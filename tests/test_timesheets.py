from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest

from app.database import connect
from app.repositories.base import ValidationError
from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.timesheet_repository import TimesheetRepository, decimal_hours, hours_value


@pytest.fixture
def staff(db):
    people, brigades = PersonnelRepository(db), BrigadeRepository(db)
    contractor = people.save_contractor(dict(name='Учебный подрядчик'))
    specialty = people.save_specialty('Штукатур', 'TEST-42')
    workers = [people.save_worker(dict(full_name='Учебный Работник {}'.format(number),
                    contractor_id=contractor, specialty_id=specialty, effective_date='2020-01-01'))
               for number in range(1, 4)]
    brigade = brigades.save(contractor, '01')
    for employee in workers[:2]:
        brigades.assign(employee, brigade, '2026-10-01')
    return dict(db=db, people=people, brigades=brigades, contractor=contractor,
                specialty=specialty, workers=workers, brigade=brigade, repo=TimesheetRepository(db))


def create(data, year=2026, month=10, **options):
    return data['repo'].create(data['contractor'], year, month, **options)


def change(employee, hours='8', date='2026-10-01', code='Я', **options):
    return dict(employee_id=employee, work_date=date, code=code, hours=hours, **options)


def worker_report(data, sheet, employee=None, version=None):
    employee = employee or data['workers'][0]
    return next(row for row in data['repo'].report(sheet, version)['workers'] if row['employee_id'] == employee)


def approve(data, sheet):
    data['repo'].transition(sheet, 'submit')
    data['repo'].transition(sheet, 'approve')


def other_project(data):
    other = ProjectRepository(data['db']).create_project('Учебная школа', 'OTHER-42')
    data['people'].share_contractor(data['contractor'], other)
    people = PersonnelRepository(data['db'], other)
    for employee in data['workers']:
        people.assign_to_project(employee, '2020-01-01')
    repo = TimesheetRepository(data['db'], other)
    return other, repo, repo.create(data['contractor'], 2026, 10)


def test_create_loads_roster_with_stable_ids_but_never_creates_attendance(staff):
    sheet = create(staff)
    report = staff['repo'].report(sheet)
    assert report['version']['status'] == 'draft'
    assert {row['employee_id'] for row in report['workers']} == set(staff['workers'])
    assert report['total_hours'] == Decimal('0')
    assert all(row['missing_days'] == 31 and not row['entries'] for row in report['workers'])
    assert not staff['db'].execute('SELECT 1 FROM timesheet_entries').fetchone()


@pytest.mark.parametrize('year,month,days', [(2023, 2, 28), (2024, 2, 29), (2026, 4, 30), (2026, 10, 31)])
def test_month_length_and_leap_year_are_calendar_based(staff, year, month, days):
    report = staff['repo'].report(create(staff, year, month))
    assert len(report['days']) == days
    assert report['days'][-1] == '{:04d}-{:02d}-{:02d}'.format(year, month, days)


@pytest.mark.parametrize('year,month', [(True, 10), (2026, False), (2026, 0), (2026, 13), (1899, 10), ('2026', 10)])
def test_create_rejects_invalid_calendar_values(staff, year, month):
    with pytest.raises(ValidationError):
        create(staff, year, month)
    assert not staff['db'].execute('SELECT 1 FROM timesheets').fetchone()


@pytest.mark.parametrize('hours,expected', [('8', Decimal('8')), ('4', Decimal('4')), ('0.125', Decimal('.125')),
                                          ('0.000000001', Decimal('.000000001')), ('24', Decimal('24'))])
def test_partial_fractional_and_boundary_hours_are_exact(staff, hours, expected):
    sheet = create(staff)
    staff['repo'].set_entries(sheet, [change(staff['workers'][0], hours)])
    assert worker_report(staff, sheet)['total_hours'] == expected
    assert staff['db'].execute('SELECT typeof(hours_nanos) FROM timesheet_entries').fetchone()[0] == 'integer'


@pytest.mark.parametrize('hours', ['-0.1', '24.000000001', 'NaN', 'Infinity', '0.0000000001', 1.5, True, None, ''])
def test_invalid_or_missing_attendance_hours_do_not_write(staff, hours):
    sheet = create(staff)
    with pytest.raises(ValidationError):
        staff['repo'].set_entries(sheet, [change(staff['workers'][0], hours)])
    assert not staff['db'].execute('SELECT 1 FROM timesheet_entries').fetchone()


@pytest.mark.parametrize('code', ['В', 'ОТ', 'Б', 'Н'])
def test_absence_codes_are_explicit_and_do_not_count_as_work(staff, code):
    sheet = create(staff)
    staff['repo'].set_entries(sheet, [change(staff['workers'][0], '0', code=code)])
    row = worker_report(staff, sheet)
    assert row['total_hours'] == 0 and row['absence_days'] == 1 and row['attendance_days'] == 0
    assert row['missing_days'] == 30 and row['entries']['2026-10-01']['code'] == code
    with pytest.raises(ValidationError):
        staff['repo'].set_entries(sheet, [change(staff['workers'][0], '8', code=code)])


def test_explicit_zero_attendance_is_distinct_from_missing_and_absence(staff):
    sheet = create(staff)
    staff['repo'].set_entries(sheet, [change(staff['workers'][0], '0')])
    row = worker_report(staff, sheet)
    assert row['total_hours'] == 0 and row['attendance_days'] == 0 and row['absence_days'] == 0
    assert row['missing_days'] == 30
    assert staff['repo'].report(sheet)['warnings']


def test_manual_example_totals_eighteen_hours_without_assumed_shift(staff):
    sheet = create(staff)
    employee = staff['workers'][0]
    staff['repo'].set_entries(sheet, [change(employee, '8'), change(employee, '6', '2026-10-02'),
        change(employee, '0', '2026-10-03', 'В'), change(employee, '4', '2026-10-04')])
    row = worker_report(staff, sheet)
    assert (row['total_hours'], row['attendance_days'], row['absence_days']) == (Decimal('18'), 3, 1)


def test_duplicate_changes_fail_atomically_and_repeated_save_replaces_cell(staff):
    sheet, employee = create(staff), staff['workers'][0]
    with pytest.raises(ValidationError):
        staff['repo'].set_entries(sheet, [change(employee), change(employee, '4')])
    assert not staff['db'].execute('SELECT 1 FROM timesheet_entries').fetchone()
    staff['repo'].set_entries(sheet, [change(employee)], operation_key='one-cell')
    staff['repo'].set_entries(sheet, [change(employee)], operation_key='one-cell')
    staff['repo'].set_entries(sheet, [change(employee, '6')])
    assert staff['db'].execute('SELECT COUNT(*) FROM timesheet_entries').fetchone()[0] == 1
    assert worker_report(staff, sheet)['total_hours'] == Decimal('6')
    with pytest.raises(ValidationError):
        staff['repo'].set_entries(sheet, [change(employee, '7')], operation_key='one-cell')


def test_creation_idempotency_and_context_uniqueness(staff):
    sheet = create(staff, operation_key='create42')
    assert create(staff, operation_key='create42') == sheet
    assert staff['db'].execute('SELECT COUNT(*) FROM timesheets').fetchone()[0] == 1
    with pytest.raises(ValidationError):
        create(staff)


def test_cross_project_daily_limit_uses_one_employee_id_and_reserves_drafts(staff):
    sheet = create(staff)
    _, other, other_sheet = other_project(staff)
    employee = staff['workers'][0]
    staff['repo'].set_entries(sheet, [change(employee, '16')])
    with pytest.raises(ValidationError):
        other.set_entries(other_sheet, [change(employee, '8.000000001')])
    other.set_entries(other_sheet, [change(employee, '8')])
    assert other.report(other_sheet)['warnings']
    assert staff['db'].execute('SELECT COUNT(*) FROM employees').fetchone()[0] == 3
    assert worker_report(staff, sheet)['total_hours'] == Decimal('16')


def test_correction_reserves_maximum_of_draft_and_previous_approved_until_approval(staff):
    sheet = create(staff)
    employee = staff['workers'][0]
    staff['repo'].set_entries(sheet, [change(employee, '16')])
    approve(staff, sheet)
    original = staff['repo'].report(sheet)['version']['id']
    correction = staff['repo'].create_correction(sheet, 'Уточнение часов')
    staff['repo'].set_entries(sheet, [change(employee, '8')], version_id=correction)
    _, other, other_sheet = other_project(staff)
    with pytest.raises(ValidationError):
        other.set_entries(other_sheet, [change(employee, '9')])
    other.set_entries(other_sheet, [change(employee, '8')])
    approve(staff, sheet)
    other.set_entries(other_sheet, [change(employee, '16')])
    assert worker_report(staff, sheet, version=original)['total_hours'] == Decimal('16')
    assert worker_report(staff, sheet)['total_hours'] == Decimal('8')
    assert other.report(other_sheet)['total_hours'] == Decimal('16')


def test_cancelled_correction_restores_approved_without_releasing_reserved_hours(staff):
    sheet = create(staff)
    employee = staff['workers'][0]
    staff['repo'].set_entries(sheet, [change(employee, '16')])
    approve(staff, sheet)
    correction = staff['repo'].create_correction(sheet, 'Первое уточнение')
    staff['repo'].set_entries(sheet, [change(employee, '8')])
    staff['repo'].transition(sheet, 'cancel', reason='Не требуется', version_id=correction)
    assert staff['repo'].report(sheet)['version']['status'] == 'approved'
    assert staff['repo'].report(sheet)['total_hours'] == Decimal('16')
    assert staff['repo'].create_correction(sheet, 'Следующее уточнение') != correction


def test_status_transitions_require_review_and_keep_approved_snapshot(staff):
    sheet = create(staff)
    employee = staff['workers'][0]
    staff['repo'].set_entries(sheet, [change(employee, '8')])
    with pytest.raises(ValidationError):
        staff['repo'].transition(sheet, 'approve')
    staff['repo'].transition(sheet, 'submit')
    with pytest.raises(ValidationError):
        staff['repo'].set_entries(sheet, [change(employee, '6')])
    staff['repo'].transition(sheet, 'return', reason='Уточните')
    staff['repo'].set_entries(sheet, [change(employee, '6')])
    approve(staff, sheet)
    approved = staff['repo'].report(sheet)
    assert approved['version']['approved_at']
    for action in ['cancel', 'return', 'submit']:
        with pytest.raises(ValidationError):
            staff['repo'].transition(sheet, action, reason='Причина')
    with pytest.raises(ValidationError):
        staff['repo'].set_entries(sheet, [change(employee, '5')])
    correction = staff['repo'].create_correction(sheet, 'Ошиблись на час')
    staff['repo'].set_entries(sheet, [change(employee, '5')], version_id=correction)
    assert worker_report(staff, sheet, version=approved['version']['id'])['total_hours'] == Decimal('6')
    assert staff['repo'].report(sheet)['version']['base_version_id'] == approved['version']['id']
    assert [row['operation'] for row in approved['events']] == ['created', 'entries_changed', 'submit', 'return', 'entries_changed', 'submit', 'approve']


def test_missing_days_do_not_prevent_approval_and_archived_person_remains_in_history(staff):
    sheet = create(staff)
    staff['repo'].set_entries(sheet, [change(staff['workers'][0], '8')])
    approve(staff, sheet)
    before = worker_report(staff, sheet)
    staff['people'].set_active('employees', staff['workers'][0], False)
    assert worker_report(staff, sheet) == before


def test_worker_and_brigade_renaming_cannot_change_approved_snapshots(staff):
    sheet = create(staff)
    approve(staff, sheet)
    before = worker_report(staff, sheet)
    staff['people'].save_worker(dict(full_name='Учебный Переименованный', contractor_id=staff['contractor'],
        specialty_id=staff['specialty']), staff['workers'][0])
    staff['brigades'].save(staff['contractor'], 'Переименована', staff['brigade'])
    assert worker_report(staff, sheet) == before


def test_roster_uses_month_intersection_and_each_entry_checks_date_availability(staff):
    employee = staff['workers'][0]
    staff['people'].end_project_assignment(employee, '2026-10-10')
    sheet = create(staff)
    assert employee in {row['employee_id'] for row in staff['repo'].report(sheet)['workers']}
    staff['repo'].set_entries(sheet, [change(employee, '8', '2026-10-09')])
    with pytest.raises(ValidationError):
        staff['repo'].set_entries(sheet, [change(employee, '8', '2026-10-10')])


def test_worker_starting_later_in_month_cannot_receive_earlier_attendance(staff):
    employee = staff['people'].save_worker(dict(full_name='Поздний Работник', contractor_id=staff['contractor'],
        specialty_id=staff['specialty'], effective_date='2026-10-20'))
    sheet = create(staff)
    with pytest.raises(ValidationError):
        staff['repo'].set_entries(sheet, [change(employee, '8', '2026-10-19')])
    staff['repo'].set_entries(sheet, [change(employee, '8', '2026-10-20')])


def test_bulk_preview_uses_selected_historical_members_and_never_marks_others(staff):
    sheet = create(staff)
    first, second, _ = staff['workers']
    two = staff['brigades'].save(staff['contractor'], '2')
    staff['brigades'].assign(first, two, '2026-10-16')
    preview = staff['repo'].preview_bulk(sheet, [first], '2026-10-15', '2026-10-15', 'Я', '8', brigade_id=staff['brigade'])
    assert not staff['repo'].get_preview(sheet, preview)['errors']
    assert not staff['db'].execute('SELECT 1 FROM timesheet_entries').fetchone()
    staff['repo'].apply_preview(sheet, preview)
    assert worker_report(staff, sheet, first)['total_hours'] == Decimal('8')
    assert worker_report(staff, sheet, second)['total_hours'] == 0
    with pytest.raises(ValidationError):
        staff['repo'].preview_bulk(sheet, [first], '2026-10-16', '2026-10-16', 'Я', '8', brigade_id=staff['brigade'])
    with pytest.raises(ValidationError):
        staff['repo'].preview_bulk(sheet, [first], '2026-10-15', '2026-10-16', 'Я', '8', brigade_id=staff['brigade'])


def test_bulk_preview_rechecks_membership_when_applied(staff):
    sheet, employee = create(staff), staff['workers'][0]
    preview = staff['repo'].preview_bulk(sheet, [employee], '2026-10-10', '2026-10-10', 'Я', '8', brigade_id=staff['brigade'])
    other = staff['brigades'].save(staff['contractor'], '2')
    staff['brigades'].assign(employee, other, '2026-10-10')
    with pytest.raises(ValidationError):
        staff['repo'].apply_preview(sheet, preview)
    assert not staff['db'].execute('SELECT 1 FROM timesheet_entries').fetchone()


def test_copy_clear_and_stale_preview_preserve_explicit_history(staff):
    sheet, employee = create(staff), staff['workers'][0]
    staff['repo'].set_entries(sheet, [change(employee, '7.5')])
    token = staff['repo'].preview_copy(sheet, '2026-10-01', ['2026-10-02', '2026-10-03'], [employee])
    staff['repo'].apply_preview(sheet, token, operation_key='copy-three')
    staff['repo'].apply_preview(sheet, token, operation_key='copy-three')
    assert worker_report(staff, sheet)['total_hours'] == Decimal('22.5')
    stale = staff['repo'].preview_bulk(sheet, [employee], '2026-10-04', '2026-10-04', 'Я', '8')
    staff['repo'].set_entries(sheet, [dict(employee_id=employee, work_date='2026-10-02', clear=True)])
    assert worker_report(staff, sheet)['missing_days'] == 29
    with pytest.raises(ValidationError):
        staff['repo'].apply_preview(sheet, stale)


def test_import_previews_are_atomic_idempotent_and_do_not_create_workers(staff):
    sheet, employee = create(staff), staff['workers'][0]
    rows = [dict(row_number=2, full_name='Учебный Работник 1', errors=[], **change(employee, '8'))]
    token = staff['repo'].preview_import(sheet, rows, 'табель.xlsx')
    assert staff['repo'].get_preview(sheet, token)['filename'] == 'табель.xlsx'
    assert staff['db'].execute('SELECT COUNT(*) FROM timesheet_entries').fetchone()[0] == 0
    staff['repo'].apply_preview(sheet, token)
    staff['repo'].apply_preview(sheet, token)
    assert staff['db'].execute('SELECT COUNT(*) FROM timesheet_entries').fetchone()[0] == 1
    assert staff['db'].execute('SELECT COUNT(*) FROM employees').fetchone()[0] == 3
    assert worker_report(staff, sheet)['total_hours'] == Decimal('8')


def test_import_preview_reports_row_errors_invalid_ids_and_duplicates_without_writing(staff):
    sheet, employee = create(staff), staff['workers'][0]
    rows = [dict(row_number=2, errors=[], **change(employee)),
            dict(row_number=3, errors=[], **change(employee, '4')),
            dict(row_number=4, errors=['Ошибка Excel'], **change(99999, '4'))]
    token = staff['repo'].preview_import(sheet, rows, 'ошибки.xlsx')
    preview = staff['repo'].get_preview(sheet, token)
    assert len(preview['errors']) >= 3
    with pytest.raises(ValidationError):
        staff['repo'].apply_preview(sheet, token)
    assert not staff['db'].execute('SELECT 1 FROM timesheet_entries').fetchone()
    staff['repo'].cancel_preview(sheet, token)
    assert staff['repo'].get_preview(sheet, token)['status'] == 'cancelled'


def test_transaction_failure_rolls_back_entries_revision_operations_and_audit(staff, monkeypatch):
    sheet, employee = create(staff), staff['workers'][0]
    before = staff['repo'].report(sheet)
    def fail(*args, **kwargs):
        raise RuntimeError('Искусственная ошибка записи журнала')
    monkeypatch.setattr(staff['repo'], '_event', fail)
    with pytest.raises(RuntimeError):
        staff['repo'].set_entries(sheet, [change(employee)], operation_key='will-rollback')
    after = staff['repo'].report(sheet)
    assert after['version'] == before['version'] and after['events'] == before['events']
    assert not staff['db'].execute('SELECT 1 FROM timesheet_entries').fetchone()
    assert not staff['db'].execute("SELECT 1 FROM timesheet_operations WHERE id='will-rollback'").fetchone()


def test_cross_project_context_prevents_reads_mutations_and_preview_access(staff):
    sheet = create(staff)
    _, other, other_sheet = other_project(staff)
    token = staff['repo'].preview_bulk(sheet, [staff['workers'][0]], '2026-10-01', '2026-10-01', 'Я', '8')
    for operation in [lambda: other.report(sheet),
                      lambda: other.set_entries(sheet, [change(staff['workers'][0])]),
                      lambda: other.transition(sheet, 'submit'),
                      lambda: other.get_preview(other_sheet, token),
                      lambda: other.apply_preview(other_sheet, token)]:
        with pytest.raises(ValidationError):
            operation()


def test_foreign_contractor_cannot_receive_another_employers_employee(staff):
    other = staff['people'].save_contractor(dict(name='Другой подрядчик'))
    sheet = staff['repo'].create(other, 2026, 10)
    assert not staff['repo'].report(sheet)['workers']
    with pytest.raises(ValidationError):
        staff['repo'].add_workers(sheet, [staff['workers'][0]])
    with pytest.raises(ValidationError):
        staff['repo'].set_entries(sheet, [change(staff['workers'][0])])


def test_concurrent_cross_project_writes_cannot_allocate_more_than_twenty_four_hours(staff):
    sheet = create(staff)
    other_id, _, other_sheet = other_project(staff)
    filename = staff['db'].execute('PRAGMA database_list').fetchone()[2]
    barrier = Barrier(2)
    def attempt(project_id, sheet_id):
        db = connect(filename)
        try:
            repo = TimesheetRepository(db, project_id)
            barrier.wait(timeout=10)
            try:
                repo.set_entries(sheet_id, [change(staff['workers'][0], '13')])
                return 'saved'
            except ValidationError:
                return 'rejected'
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(attempt, 1, sheet)
        two = pool.submit(attempt, other_id, other_sheet)
        assert sorted([one.result(timeout=15), two.result(timeout=15)]) == ['rejected', 'saved']
    assert staff['db'].execute('SELECT COUNT(*) FROM timesheet_entries').fetchone()[0] == 1


def test_timesheet_has_no_effect_on_volume_events_or_physical_progress(staff, work, item, plan, events):
    from app.calculation_engine.historical import HistoricalCalculator
    events.add_fact(item, '600', '2026-01-10', recorded_at='2026-01-10T12:00:00Z', event_key='time-independent-fact')
    events.add_confirmation(item, '450', '2026-01-10', recorded_at='2026-01-10T13:00:00Z', document_ref='Акт', event_key='time-independent-confirm')
    before = HistoricalCalculator(staff['db']).calculate_as_of('2026-01-31', '2026-02-01T00:00:00Z', plan)
    sheet = create(staff)
    staff['repo'].set_entries(sheet, [change(staff['workers'][0])])
    approve(staff, sheet)
    after = HistoricalCalculator(staff['db']).calculate_as_of('2026-01-31', '2026-02-01T00:00:00Z', plan)
    assert before == after
    assert (after.total.actual_progress, after.total.confirmed_progress) == (Decimal('.6'), Decimal('.45'))
    assert staff['db'].execute('SELECT COUNT(*) FROM fact_events').fetchone()[0] == 1
    assert staff['db'].execute('SELECT COUNT(*) FROM confirmation_events').fetchone()[0] == 1


def test_low_decimal_context_does_not_round_stored_nine_decimal_hours():
    from decimal import localcontext
    with localcontext() as context:
        context.prec = 2
        assert hours_value('23.123456789') == 23123456789
        assert decimal_hours(23123456789) == Decimal('23.123456789')


@pytest.mark.parametrize('value', ['1E-999999999', '1E999999999', Decimal('1E-999999999'), Decimal('1E999999999')])
def test_extreme_nonzero_exponents_fail_without_expanding_decimal_strings(value):
    with pytest.raises(ValidationError):
        hours_value(value)


@pytest.mark.parametrize('value', ['0E-999999999', Decimal('0E-999999999'), '-0E999999999'])
def test_extreme_zero_exponents_are_safe_and_exactly_zero(value):
    assert hours_value(value) == 0


def test_cancelled_month_can_be_reopened_as_new_version_without_new_header(staff):
    sheet = create(staff)
    first = staff['repo'].report(sheet)['version']['id']
    staff['repo'].set_entries(sheet, [change(staff['workers'][0], '8')])
    staff['repo'].transition(sheet, 'cancel', reason='Создан ошибочно')
    reopened = create(staff)
    report = staff['repo'].report(reopened)
    assert reopened == sheet and report['version']['version_no'] == 2
    assert report['version']['status'] == 'draft' and report['version']['base_version_id'] is None
    assert report['total_hours'] == 0
    assert staff['repo'].report(sheet, first)['version']['status'] == 'cancelled'
    assert staff['db'].execute('SELECT COUNT(*) FROM timesheets').fetchone()[0] == 1


def test_worker_note_has_audit_history_and_is_carried_into_correction(staff):
    sheet, employee = create(staff), staff['workers'][0]
    staff['repo'].set_worker_note(sheet, employee, 'Неполная занятость', operation_key='note42')
    staff['repo'].set_worker_note(sheet, employee, 'Неполная занятость', operation_key='note42')
    approve(staff, sheet)
    original = staff['repo'].report(sheet)['version']['id']
    assert worker_report(staff, sheet)['note'] == 'Неполная занятость'
    with pytest.raises(ValidationError):
        staff['repo'].set_worker_note(sheet, employee, 'Нельзя менять утверждённое')
    correction = staff['repo'].create_correction(sheet, 'Уточнение примечания')
    assert worker_report(staff, sheet)['note'] == 'Неполная занятость'
    staff['repo'].set_worker_note(sheet, employee, 'Новое примечание', version_id=correction)
    assert worker_report(staff, sheet, version=original)['note'] == 'Неполная занятость'
    assert worker_report(staff, sheet)['note'] == 'Новое примечание'


def test_stale_approved_version_cannot_silently_choose_another_correction_base(staff):
    sheet = create(staff)
    approve(staff, sheet)
    first = staff['repo'].report(sheet)['version']
    second_id = staff['repo'].create_correction(sheet, 'Первое уточнение', version_id=first['id'],
        expected_revision=first['revision'])
    approve(staff, sheet)
    second = staff['repo'].report(sheet)['version']
    assert second['id'] == second_id and second['version_no'] == 2
    with pytest.raises(ValidationError):
        staff['repo'].create_correction(sheet, 'Открыта устаревшая форма', version_id=first['id'],
            expected_revision=first['revision'], operation_key='stale-base')
    assert staff['db'].execute('SELECT COUNT(*) FROM timesheet_versions').fetchone()[0] == 2
    third = staff['repo'].create_correction(sheet, 'Просмотрено новое основание', version_id=second['id'],
        expected_revision=second['revision'], operation_key='correct-base')
    assert staff['repo'].report(sheet)['version']['base_version_id'] == second_id
    assert staff['repo'].create_correction(sheet, 'Просмотрено новое основание', version_id=second['id'],
        expected_revision=second['revision'], operation_key='correct-base') == third
    assert staff['db'].execute('SELECT COUNT(*) FROM timesheet_versions').fetchone()[0] == 3


def test_zero_duration_project_availability_does_not_break_month_creation(staff):
    employee = staff['people'].save_worker(dict(full_name='Учебный Нулевой Период',
        contractor_id=staff['contractor'], specialty_id=staff['specialty'], effective_date='2026-10-10'))
    staff['people'].end_project_assignment(employee, '2026-10-10')
    sheet = create(staff)
    report = staff['repo'].report(sheet)
    assert employee not in {worker['employee_id'] for worker in report['workers']}
    assert {worker['employee_id'] for worker in report['workers']} == set(staff['workers'])
    assert report['total_hours'] == 0


def test_obsolete_approved_time_on_other_project_does_not_cause_overlap_warning(staff):
    sheet, employee = create(staff), staff['workers'][0]
    _, other, other_sheet = other_project(staff)
    other.set_entries(other_sheet, [change(employee, '8')])
    other.transition(other_sheet, 'submit')
    other.transition(other_sheet, 'approve')
    other.create_correction(other_sheet, 'Явка занесена ошибочно')
    other.set_entries(other_sheet, [dict(employee_id=employee, work_date='2026-10-01', clear=True)])
    other.transition(other_sheet, 'submit')
    other.transition(other_sheet, 'approve')
    staff['repo'].set_entries(sheet, [change(employee, '8')])
    assert other.report(other_sheet)['total_hours'] == 0
    assert not staff['repo'].report(sheet)['warnings']
    # Only the old immutable historical version still contains the eight hours.
    assert staff['db'].execute('SELECT COUNT(*) FROM timesheet_entries').fetchone()[0] == 2


def test_report_keeps_one_snapshot_when_another_connection_commits_mid_read(staff):
    db = staff['db']
    assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    sheet, employee = create(staff), staff['workers'][0]
    before = staff['repo'].report(sheet)
    writer = connect(db.execute('PRAGMA database_list').fetchone()[2])
    committed = []
    def concurrent_commit(sql):
        if not committed and sql.startswith('SELECT * FROM timesheet_entries WHERE version_id='):
            committed.append(True)
            TimesheetRepository(writer).set_entries(sheet, [change(employee, '8')])
    db.set_trace_callback(concurrent_commit)
    try:
        snapshot = staff['repo'].report(sheet)
    finally:
        db.set_trace_callback(None)
        writer.close()
    assert committed == [True]
    assert snapshot['version'] == before['version']
    assert snapshot['total_hours'] == 0 and not snapshot['workers'][0]['entries']
    assert len(snapshot['events']) == 1
    current = staff['repo'].report(sheet)
    assert current['total_hours'] == Decimal('8')
    assert current['version']['revision'] == before['version']['revision'] + 1
    assert len(current['events']) == 2
    assert not db.in_transaction
