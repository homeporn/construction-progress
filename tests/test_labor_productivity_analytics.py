"""Temporary databases only: measured productivity and historical conflicts."""
from decimal import Decimal, localcontext

import pytest

from app.repositories.base import ValidationError
from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.event_repository import EventRepository
from app.repositories.labor_repository import LaborRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.timesheet_repository import TimesheetRepository
from app.repositories.work_repository import WorkRepository
from app.services.labor_productivity import LaborProductivityService
from app.time_utils import instant


@pytest.fixture
def labor_school(db, monkeypatch):
    clock = {'value': '2026-11-10T10:00:00Z'}
    monkeypatch.setattr('app.repositories.timesheet_repository.instant',
                        lambda value=None: instant(value or clock['value']))
    people, brigades = PersonnelRepository(db), BrigadeRepository(db)
    contractor = people.save_contractor(dict(name='Подрядчик производительности'))
    specialty = people.save_specialty('Исполнитель', 'LP-43')
    workers = [people.save_worker(dict(full_name='Работник {}'.format(i),
                contractor_id=contractor, specialty_id=specialty, effective_date='2020-01-01'))
               for i in range(2)]
    brigade = brigades.save(contractor, 'ЛП-01')
    for worker in workers:
        brigades.assign(worker, brigade, '2026-10-01')
    work = WorkRepository(db)
    floor = work.add_floor(1, 'ЛП', 1)
    item = work.add_work_item(1, floor, 2)
    version = work.create_plan_version('CONSTRUCTION', created_at='2026-01-01T00:00:00Z')
    work.add_plan_line(version, item, '1000', '0.1')
    work.approve_plan_version(version, approved_at='2026-01-01T01:00:00Z')
    events = EventRepository(db)
    fact = events.add_fact(item, '100', '2026-10-01', event_key='lp-fact',
                           recorded_at='2026-11-10T09:00:00Z')
    timesheets = TimesheetRepository(db)
    sheet = timesheets.create(contractor, 2026, 10)
    return dict(db=db, clock=clock, people=people, work=work, events=events,
        repo=LaborRepository(db), service=LaborProductivityService(db), timesheets=timesheets,
        contractor=contractor, brigade=brigade, workers=workers, item=item, floor=floor,
        version=version, fact=fact, sheet=sheet)


def approve(s, changes):
    s['timesheets'].set_entries(s['sheet'], changes)
    s['timesheets'].transition(s['sheet'], 'submit')
    s['timesheets'].transition(s['sheet'], 'approve')


def entry(s, hours='8', stamp='2026-10-01', worker=0):
    return dict(employee_id=s['workers'][worker], work_date=stamp, code='Я', hours=hours)


def assign(s, hours='8', quantity='100', stamp='2026-10-01', item=None, fact=None, **options):
    s['repo'].allocate(item or s['item'], stamp, [dict(employee_id=s['workers'][0], hours=hours)],
                       recorded_at='2026-11-11T10:00:00Z')
    if quantity is not None:
        s['repo'].attribute(fact or s['fact'], s['contractor'], quantity, brigade_id=s['brigade'],
                            recorded_at='2026-11-11T11:00:00Z', **options)


def report(s, **options):
    args = dict(start_date='2026-10-01', end_date='2026-10-02',
                knowledge_cutoff='2026-12-31T23:59:59Z')
    args.update(options)
    return s['service'].build(**args)


def test_missing_approved_time_is_unknown_but_approved_explicit_zero_is_zero(labor_school):
    s = labor_school
    missing = report(s)
    assert missing['summary']['worked_hours'] is None
    assert missing['summary']['unique_workers'] is None
    assert missing['works'][0]['actual_output'] is None
    approve(s, [entry(s, '0')])
    zero = report(s)
    assert zero['summary']['has_approved_time'] is True
    assert zero['summary']['worked_hours'] == zero['summary']['allocated_hours'] == Decimal(0)
    assert zero['summary']['unique_workers'] == zero['summary']['average_daily_workers'] == 0
    assert zero['works'][0]['actual_output'] is None


def test_approved_sheet_without_period_cells_has_zero_total_and_explicit_warning(labor_school):
    s = labor_school
    approve(s, [entry(s)])
    data = report(s, start_date='2026-10-02')
    assert data['summary']['worked_hours'] == 0
    assert data['available_hours'] == []
    assert any('нет заполненных ячеек' in text for text in data['warnings'])


def test_exact_125_percent_with_explicit_volume_and_frozen_plan(labor_school):
    s = labor_school
    approve(s, [entry(s)])
    assign(s)
    s['db'].execute("UPDATE work_catalog SET reference_labor_norm='99' WHERE id=2")
    data = report(s)
    assert data['summary']['worked_hours'] == data['summary']['allocated_hours'] == Decimal(8)
    assert data['summary']['unallocated_hours'] == 0
    assert data['summary']['normative_hours'] == Decimal(10)
    assert data['summary']['unique_workers'] == 1
    assert data['summary']['average_daily_workers'] == Decimal('.5')
    assert [r['workers'] for r in data['dynamics']] == [1, 0]
    for row in data['works'] + data['contractors'] + data['brigades']:
        assert row['quantity'] == 100 and row['hours'] == 8
        assert row['actual_output'] == Decimal('12.5')
        assert row['norm_output'] == Decimal(10)
        assert row['norm_ratio'] == Decimal('1.25')
        assert row['complete'] is True
    assert data['contractors'][0]['coverage'] == 1
    assert data['events'][0]['assigned'] == 100 and data['events'][0]['remaining'] == 0


def test_hours_and_volume_require_separate_explicit_assignments(labor_school):
    s = labor_school
    approve(s, [entry(s)])
    assign(s, quantity=None)
    data = report(s)
    assert data['works'][0]['quantity'] == 100
    assert data['works'][0]['actual_output'] == Decimal('12.5')
    assert data['works'][0]['complete'] is False
    assert data['contractors'][0]['quantity'] is None
    assert data['contractors'][0]['actual_output'] is None
    assert data['attributions'] == [] and data['events'][0]['remaining'] == 100
    assert data['warnings']
    brigade = report(s, filters={'brigade_id': s['brigade']})
    assert brigade['dynamics'][0]['normative_hours'] is None
    assert brigade['dynamics'][0]['norm_ratio'] is None


def test_revoked_volume_is_unassigned_instead_of_measured_executor_zero(labor_school):
    s = labor_school
    approve(s, [entry(s)])
    assign(s, employee_id=s['workers'][0])
    s['repo'].attribute(s['fact'], s['contractor'], '0', brigade_id=s['brigade'],
        employee_id=s['workers'][0], expected_revision=1, reason='Назначение отменено',
        recorded_at='2026-11-12T10:00:00Z')
    data = report(s)
    assert data['attributions'][0]['quantity'] == 0
    assert data['attributions'][0]['revision_no'] == 2
    assert data['attributions'][0]['valid'] is True
    assert data['events'][0]['assigned'] == 0
    assert data['events'][0]['remaining'] == 100
    for row in data['contractors'] + data['brigades']:
        assert row['quantity'] is row['normative_hours'] is row['actual_output'] is None
        assert row['norm_output'] is row['norm_ratio'] is None
    brigade = report(s, filters={'brigade_id': s['brigade']})
    assert brigade['works'][0]['quantity'] is None
    assert brigade['dynamics'][0]['normative_hours'] is None
    assert brigade['dynamics'][0]['norm_ratio'] is None


def test_confirmed_journal_does_not_invent_executor_confirmation(labor_school):
    s = labor_school
    approve(s, [entry(s)])
    assign(s)
    s['events'].add_confirmation(s['item'], '80', '2026-10-01', event_key='lp-confirm',
        document_ref='Акт', recorded_at='2026-11-11T12:00:00Z')
    data = report(s, mode='confirmed')
    assert data['works'][0]['quantity'] == 80
    assert data['works'][0]['actual_output'] == Decimal(10)
    for row in data['contractors'] + data['brigades']:
        assert row['quantity'] is row['normative_hours'] is row['actual_output'] is row['norm_ratio'] is None
    filtered = report(s, mode='confirmed', filters={'contractor_id': s['contractor']})
    assert filtered['works'][0]['quantity'] is None
    assert filtered['dynamics'][0]['normative_hours'] is None
    assert filtered['dynamics'][0]['norm_ratio'] is None


def test_approved_time_correction_invalidates_old_eight_hours_and_preserves_history(labor_school):
    s = labor_school
    approve(s, [entry(s)])
    assign(s)
    s['clock']['value'] = '2026-11-12T10:00:00Z'
    s['timesheets'].create_correction(s['sheet'], 'Восемь часов исправлены на шесть')
    approve(s, [entry(s, '6')])
    data = report(s)
    assert data['summary']['worked_hours'] == 6
    assert data['summary']['allocated_hours'] == 8
    assert data['summary']['unallocated_hours'] == -2
    assert data['summary']['conflict_count'] == 1
    assert data['allocations'][0]['valid'] is False
    assert data['dynamics'][0]['normative_hours'] is None
    assert data['dynamics'][0]['norm_ratio'] is None
    for row in data['works'] + data['contractors'] + data['brigades']:
        assert row['actual_output'] is row['hours'] is row['norm_ratio'] is None
        assert row['complete'] is False
    old = report(s, knowledge_cutoff='2026-11-11T23:59:59Z')
    assert old['summary']['worked_hours'] == 8 and old['summary']['conflict_count'] == 0
    assert old['contractors'][0]['norm_ratio'] == Decimal('1.25')


def test_revoked_zero_allocation_is_valid_when_latest_approved_cell_was_removed(labor_school):
    s = labor_school
    approve(s, [entry(s)])
    assign(s)
    s['clock']['value'] = '2026-11-12T10:00:00Z'
    s['timesheets'].create_correction(s['sheet'], 'Запись удалена после проверки')
    approve(s, [dict(employee_id=s['workers'][0], work_date='2026-10-01', clear=True)])
    s['repo'].allocate(s['item'], '2026-10-01',
        [dict(employee_id=s['workers'][0], hours='0', expected_revision=1)],
        reason='Снятие старого распределения', recorded_at='2026-11-13T10:00:00Z')
    data = report(s)
    assert data['allocations'][0]['valid'] is True
    assert data['summary']['conflict_count'] == 0
    assert data['summary']['allocated_hours'] == 0
    assert data['works'][0]['actual_output'] is None


def test_stored_brigade_of_valid_allocation_survives_later_membership_correction(labor_school, monkeypatch):
    s = labor_school
    approve(s, [entry(s)])
    assign(s)
    monkeypatch.setattr('app.repositories.brigade_repository.instant',
                        lambda value=None: instant(value or '2026-11-12T10:00:00Z'))
    brigades = BrigadeRepository(s['db'])
    new_brigade = brigades.save(s['contractor'], 'ЛП-02')
    brigades.assign(s['workers'][0], new_brigade, '2026-10-01')
    data = report(s, filters={'brigade_id': s['brigade']})
    assert data['allocations'][0]['brigade_id'] == s['brigade']
    assert data['allocations'][0]['valid'] is True
    assert data['brigades'][0]['hours'] == 8
    assert data['brigades'][0]['norm_ratio'] == Decimal('1.25')
    assert data['brigades'][0]['complete'] is False


def test_root_volume_reduction_invalidates_every_attribution_without_capping(labor_school):
    s = labor_school
    approve(s, [entry(s)])
    assign(s)
    s['events'].add_fact(s['item'], '-10', '2026-10-01', correction_of_id=s['fact'],
        event_key='lp-volume-correction', recorded_at='2026-11-12T10:00:00Z')
    data = report(s)
    assert data['events'][0]['available'] == 90
    assert data['events'][0]['assigned'] == 100 and data['events'][0]['remaining'] == -10
    assert data['attributions'][0]['quantity'] == 100 and data['attributions'][0]['valid'] is False
    assert data['summary']['conflict_count'] == 1
    assert data['dynamics'][0]['normative_hours'] is None
    assert data['dynamics'][0]['norm_ratio'] is None
    assert data['works'][0]['quantity'] == 90
    for row in data['works'] + data['contractors'] + data['brigades']:
        assert row['actual_output'] is row['norm_ratio'] is None
    old = report(s, knowledge_cutoff='2026-11-11T23:59:59Z')
    assert old['events'][0]['available'] == old['contractors'][0]['quantity'] == 100
    assert old['summary']['conflict_count'] == 0


def test_capacity_is_checked_across_other_work_before_work_filter(labor_school):
    s = labor_school
    second = s['work'].add_work_item(1, s['floor'], 3)
    version = s['work'].create_plan_version('CONSTRUCTION', base_version_id=s['version'],
                                           created_at='2026-01-02T00:00:00Z')
    s['work'].add_plan_line(version, second, '1000', '1')
    s['work'].approve_plan_version(version, approved_at='2026-01-02T01:00:00Z')
    approve(s, [entry(s)])
    assign(s, hours='4')
    s['repo'].allocate(second, '2026-10-01', [dict(employee_id=s['workers'][0], hours='4')],
                       recorded_at='2026-11-11T12:00:00Z')
    s['clock']['value'] = '2026-11-12T10:00:00Z'
    s['timesheets'].create_correction(s['sheet'], 'Уточнение общей продолжительности')
    approve(s, [entry(s, '6')])
    filtered = report(s, filters={'catalog_id': 2})
    assert len(filtered['allocations']) == 1
    assert filtered['allocations'][0]['hours'] == 4
    assert filtered['allocations'][0]['valid'] is False
    assert filtered['works'][0]['actual_output'] is None
    assert filtered['summary']['conflict_count'] == 1


def test_two_employers_do_not_pool_approved_capacity_for_same_person(labor_school):
    s = labor_school
    approve(s, [entry(s)])
    assign(s)
    s['clock']['value'] = '2026-11-12T10:00:00Z'
    s['timesheets'].create_correction(s['sheet'], 'Уточнение первого подрядчика')
    approve(s, [entry(s, '6')])
    second = s['people'].save_contractor(dict(name='Второй работодатель'))
    BrigadeRepository(s['db']).remove(s['workers'][0], s['brigade'], '2026-10-02')
    s['people'].save_worker(dict(contractor_id=second), s['workers'][0])
    s['clock']['value'] = '2026-11-13T10:00:00Z'
    sheet = s['timesheets'].create(second, 2026, 10)
    s['timesheets'].set_entries(sheet, [entry(s)])
    s['timesheets'].transition(sheet, 'submit')
    s['timesheets'].transition(sheet, 'approve')
    data = report(s)
    assert data['summary']['worked_hours'] == 14
    assert data['summary']['unique_workers'] == 1
    assert data['summary']['conflict_count'] == 1
    assert data['allocations'][0]['valid'] is False
    available = {r['contractor_id']: r for r in data['available_hours']}
    assert available[s['contractor']]['remaining'] == -2
    assert available[second]['remaining'] == 8
    assert data['contractors'][0]['actual_output'] is None


def test_calendar_average_deduplicates_workers_each_day(labor_school):
    s = labor_school
    approve(s, [entry(s), entry(s, '4', '2026-10-02'), entry(s, '6', worker=1)])
    data = report(s, end_date='2026-10-04')
    assert data['summary']['worked_hours'] == 18
    assert data['summary']['unique_workers'] == 2
    assert data['summary']['average_daily_workers'] == Decimal('.75')
    assert [r['workers'] for r in data['dynamics']] == [2, 1, 0, 0]
    assert data['contractors'][0]['quantity'] is None


def test_brigade_daily_productivity_uses_explicit_volume_and_normative_labor(labor_school):
    s = labor_school
    new = s['work'].create_plan_version('CONSTRUCTION', base_version_id=s['version'],
                                       created_at='2026-09-01T00:00:00Z')
    s['work'].update_plan_line(new, s['item'], '1000', '0.5')
    s['work'].approve_plan_version(new, approved_at='2026-09-01T01:00:00Z')
    approve(s, [entry(s, '20'), entry(s, '20', worker=1)])
    assign(s, hours='20')
    s['repo'].allocate(s['item'], '2026-10-01',
        [dict(employee_id=s['workers'][1], hours='20')], recorded_at='2026-11-11T12:00:00Z')
    data = report(s, filters={'brigade_id': s['brigade']})
    assert data['dynamics'][0] == dict(date='2026-10-01', hours=Decimal(40),
        allocated_hours=Decimal(40), workers=2, normative_hours=Decimal(50),
        norm_ratio=Decimal('1.25'))
    assert data['dynamics'][1]['normative_hours'] is None
    assert data['dynamics'][1]['norm_ratio'] is None
    assert report(s)['dynamics'][0]['normative_hours'] == Decimal(50)


def test_business_date_norms_and_explicit_approved_baseline_are_separate(labor_school):
    s = labor_school
    approve(s, [entry(s)])
    assign(s)
    new = s['work'].create_plan_version('CONSTRUCTION', base_version_id=s['version'],
                                       created_at='2026-10-02T00:00:00Z')
    s['work'].update_plan_line(new, s['item'], '1000', '0.2')
    s['work'].approve_plan_version(new, approved_at='2026-10-02T01:00:00Z')
    s['events'].add_fact(s['item'], '50', '2026-10-02', event_key='lp-second-day',
                         recorded_at='2026-11-12T10:00:00Z')
    data = report(s)
    assert data['works'][0]['quantity'] == 150
    assert data['works'][0]['normative_hours'] == 20
    assert data['contractors'][0]['normative_hours'] == 10
    chosen = report(s, plan_version_id=new)
    assert chosen['works'][0]['normative_hours'] == 30
    assert chosen['contractors'][0]['normative_hours'] == 20
    for row in chosen['allocations'] + chosen['attributions']:
        assert row['plan_version_id'] == s['version']
        assert row['labor_norm'] == Decimal('0.1')
    with pytest.raises(ValidationError):
        report(s, plan_version_id=new, knowledge_cutoff='2026-10-01T23:59:59Z')


def test_later_correction_reverses_original_business_date_norm(labor_school):
    s = labor_school
    new = s['work'].create_plan_version('CONSTRUCTION', base_version_id=s['version'],
                                       created_at='2026-10-02T00:00:00Z')
    s['work'].update_plan_line(new, s['item'], '1000', '0.2')
    s['work'].approve_plan_version(new, approved_at='2026-10-02T01:00:00Z')
    s['events'].add_fact(s['item'], '-10', '2026-10-02', correction_of_id=s['fact'],
        event_key='lp-norm-correction', recorded_at='2026-11-12T10:00:00Z')
    assert report(s)['works'][0]['normative_hours'] == Decimal('9')
    assert report(s, plan_version_id=new)['works'][0]['normative_hours'] == Decimal('18')


def test_known_normative_output_survives_measured_zero_volume(labor_school):
    s = labor_school
    new = s['work'].create_plan_version('CONSTRUCTION', base_version_id=s['version'],
                                       created_at='2026-09-01T00:00:00Z')
    s['work'].update_plan_line(new, s['item'], '1000', '0.5')
    s['work'].approve_plan_version(new, approved_at='2026-09-01T01:00:00Z')
    approve(s, [entry(s)])
    assign(s, quantity=None)
    s['events'].add_fact(s['item'], '-100', '2026-10-01', correction_of_id=s['fact'],
        event_key='lp-zero-volume', recorded_at='2026-11-12T10:00:00Z')
    data = report(s)
    assert data['works'][0]['quantity'] == data['works'][0]['actual_output'] == 0
    assert data['works'][0]['norm_output'] == Decimal(2)
    assert data['works'][0]['norm_ratio'] == 0


def test_partial_assignment_reports_coverage_without_total_work_as_executor_volume(labor_school):
    s = labor_school
    approve(s, [entry(s)])
    assign(s, quantity='60')
    data = report(s)
    assert data['works'][0]['quantity'] == 100
    assert data['contractors'][0]['quantity'] == 60
    assert data['contractors'][0]['coverage'] == Decimal('.6')
    assert data['contractors'][0]['actual_output'] == Decimal('7.5')
    assert data['contractors'][0]['complete'] is False
    scoped = report(s, filters={'contractor_id': s['contractor']})
    assert scoped['works'][0]['quantity'] == 60


def test_correction_against_older_root_has_period_delta_but_no_executor_ratio(labor_school):
    s = labor_school
    approve(s, [entry(s, stamp='2026-10-02')])
    assign(s, stamp='2026-10-02')
    s['events'].add_fact(s['item'], '-10', '2026-10-02', correction_of_id=s['fact'],
        event_key='lp-next-period-correction', recorded_at='2026-11-12T10:00:00Z')
    data = report(s, start_date='2026-10-02')
    assert data['works'][0]['quantity'] == -10
    assert all(r['actual_output'] is None for r in data['works'] + data['contractors'])
    assert any('более раннего периода' in text for text in data['warnings'])


def test_direction_and_project_isolation_and_no_natural_mixed_total(labor_school):
    s = labor_school
    design = s['work'].add_work_item(1, s['floor'], 2, discipline='DESIGN')
    version = s['work'].create_plan_version('DESIGN', created_at='2026-01-01T00:00:00Z')
    s['work'].add_plan_line(version, design, '1000', '2')
    s['work'].approve_plan_version(version, approved_at='2026-01-01T01:00:00Z')
    s['events'].add_fact(design, '20', '2026-10-01', event_key='lp-design',
                         recorded_at='2026-11-10T09:00:00Z')
    other = ProjectRepository(s['db']).create_project('Другой объект', 'LP-OTHER')
    assert report(s)['works'][0]['quantity'] == 100
    assert report(s, discipline='DESIGN')['works'][0]['quantity'] == 20
    assert LaborProductivityService(s['db'], other).build('2026-10-01', '2026-10-02')['works'] == []
    assert 'quantity' not in report(s)['summary']


def test_projection_is_read_only_and_uses_bounded_query_count(labor_school):
    s = labor_school
    approve(s, [entry(s)])
    assign(s)
    version = s['work'].create_plan_version('CONSTRUCTION', base_version_id=s['version'],
                                           created_at='2026-09-01T00:00:00Z')
    for number in range(20):
        floor = s['work'].add_floor(1, 'П{}'.format(number), number + 2)
        item = s['work'].add_work_item(1, floor, 2)
        s['work'].add_plan_line(version, item, '1000', '0.1')
        s['events'].add_fact(item, '0.123456789', '2026-10-01',
            event_key='lp-batch-{}'.format(number), recorded_at='2026-11-10T09:00:00Z')
    s['work'].approve_plan_version(version, approved_at='2026-09-01T01:00:00Z')
    before = s['db'].total_changes
    statements = []
    s['db'].set_trace_callback(statements.append)
    try:
        with localcontext() as context:
            context.prec = 6
            data = report(s)
    finally:
        s['db'].set_trace_callback(None)
    assert s['db'].total_changes == before
    assert data['works'][0]['quantity'] == Decimal('102.469135780')
    assert data['works'][0]['normative_hours'] == Decimal('10.2469135780')
    assert data['works'][0]['norm_ratio'] == Decimal('1.28086419725')
    selects = [q for q in statements if q.lstrip().upper().startswith('SELECT')]
    assert len(selects) <= 18
    assert any(q == 'BEGIN' for q in statements) and any(q == 'ROLLBACK' for q in statements)


@pytest.mark.parametrize('options', [dict(start_date='2026-10-03'), dict(mode='bad'),
    dict(discipline='ALL'), dict(filters={'bad': 1}), dict(filters={'contractor_id': True}),
    dict(plan_version_id=True)])
def test_invalid_report_inputs_are_rejected(labor_school, options):
    with pytest.raises((ValidationError, ValueError)):
        report(labor_school, **options)
