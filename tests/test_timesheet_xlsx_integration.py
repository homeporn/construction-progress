"""Real temporary SQLite previews/imports; the user database is never opened."""
from decimal import Decimal
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED
import sqlite3

import pytest

from app.repositories.base import ValidationError
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.timesheet_repository import TimesheetRepository
from app.services.timesheet_xlsx import HEADERS, export_timesheet_xlsx, parse_timesheet_xlsx
from app.services.xlsx_export import pack_workbook, sheet

N = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


@pytest.fixture
def time_context(db):
    people = PersonnelRepository(db)
    contractor = people.save_contractor(dict(name='ООО Строймонтаж'))
    specialty = people.save_specialty('Штукатур', 'ТС-ШТ')
    employee_ids = [people.save_worker(dict(full_name=name, contractor_id=contractor,
                    specialty_id=specialty, effective_date='2026-10-01'))
                    for name in ['Работник Первый', 'Работник Второй']]
    repository = TimesheetRepository(db)
    sheet_id = repository.create(contractor, 2026, 10)
    return dict(repository=repository, sheet_id=sheet_id, people=people,
                contractor=contractor, specialty=specialty, employee_ids=employee_ids)


def workbook(rows):
    return pack_workbook(['Табель'], [sheet(list(HEADERS), rows, exact_column=False)])


def import_rows(context, rows):
    parsed = parse_timesheet_xlsx(workbook(rows))
    return context['repository'].preview_import(context['sheet_id'], parsed, 'табель.xlsx')


def changes(context, hours='8', employee_index=0, work_date='2026-10-01', code='Я'):
    return [[context['employee_ids'][employee_index], 'Работник Первый' if employee_index == 0 else 'Работник Второй', work_date, code, Decimal(hours)]]


def record_count(db):
    return db.execute('SELECT count(*) FROM timesheet_entries').fetchone()[0]


def selected_worker(report, employee_id):
    return next(worker for worker in report['workers'] if worker['employee_id'] == employee_id)


def test_preview_has_no_entry_writes_and_confirm_stores_exact_hours(db, time_context):
    c = time_context;r = c['repository'];s = c['sheet_id']
    before = {table: db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
              for table in ['employees', 'brigades', 'fact_events', 'confirmation_events']}
    token = import_rows(c, changes(c, '6.123456789'))
    preview = r.get_preview(s, token)
    assert not preview['errors'] and record_count(db) == 0
    assert preview['changes'][0]['hours'] == '6.123456789'
    assert r.apply_preview(s, token, operation_key='import-exact')
    row = db.execute('SELECT hours_nanos FROM timesheet_entries').fetchone()
    assert row['hours_nanos'] == 6123456789 and isinstance(row['hours_nanos'], int)
    report = r.report(s)
    assert report['total_hours'] == Decimal('6.123456789')
    assert before == {table: db.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in before}


def test_error_in_any_row_blocks_whole_import_without_changing_existing_cells(db, time_context):
    c = time_context;r = c['repository'];s = c['sheet_id']
    first = import_rows(c, changes(c, '8'));r.apply_preview(s, first)
    before = [tuple(row) for row in db.execute('SELECT * FROM timesheet_entries')]
    token = import_rows(c, changes(c, '4') + [[999999, 'Неизвестный Работник', '2026-10-02', 'Я', 8]])
    assert r.get_preview(s, token)['errors']
    with pytest.raises(ValidationError):
        r.apply_preview(s, token)
    assert before == [tuple(row) for row in db.execute('SELECT * FROM timesheet_entries')]


@pytest.mark.parametrize('code,hours', [('В', '8'), ('Я', '-1'), ('Я', '25'), ('Неизвестно', '8')])
def test_attendance_and_hours_errors_are_visible_before_commit(db, time_context, code, hours):
    c = time_context;r = c['repository'];s = c['sheet_id']
    token = import_rows(c, changes(c, hours, code=code))
    assert r.get_preview(s, token)['errors']
    assert record_count(db) == 0
    with pytest.raises(ValidationError):
        r.apply_preview(s, token)


def test_duplicate_import_post_and_reimport_replace_cells_without_adding_hours(db, time_context):
    c = time_context;r = c['repository'];s = c['sheet_id']
    token = import_rows(c, changes(c, '8'))
    r.apply_preview(s, token, operation_key='same-confirmation')
    count = db.execute("SELECT count(*) FROM timesheet_events WHERE operation='import_applied'").fetchone()[0]
    r.apply_preview(s, token, operation_key='same-confirmation')
    r.apply_preview(s, token, operation_key='another-confirmation')
    assert db.execute("SELECT count(*) FROM timesheet_events WHERE operation='import_applied'").fetchone()[0] == count
    second = import_rows(c, changes(c, '8'));r.apply_preview(s, second)
    assert record_count(db) == 1 and r.report(s)['total_hours'] == Decimal('8')


def test_export_roundtrip_preserves_numbers_codes_missing_days_and_zero(db, time_context):
    c = time_context;r = c['repository'];s = c['sheet_id']
    token = import_rows(c, changes(c, '6.125') + changes(c, '0', work_date='2026-10-02', code='Б'))
    r.apply_preview(s, token)
    before = r.report(s)
    exported = export_timesheet_xlsx(before)
    parsed = parse_timesheet_xlsx(exported)
    assert len(parsed) == 2 and not any(row['errors'] for row in parsed)
    assert [row['hours'] for row in parsed] == ['6.125', '0']
    assert [row['code'] for row in parsed] == ['Я', 'Б']
    preview = r.preview_import(s, parsed, 'roundtrip.xlsx');r.apply_preview(s, preview)
    after = r.report(s)
    assert after['total_hours'] == before['total_hours'] == Decimal('6.125')
    employee = c['employee_ids'][0]
    assert selected_worker(after, employee)['entries'].keys() == selected_worker(before, employee)['entries'].keys()
    with ZipFile(BytesIO(exported)) as archive:
        xml = ET.fromstring(archive.read('xl/worksheets/sheet2.xml'))
        cell = xml.find("s:sheetData/s:row[@r='2']/s:c[@r='E2']", N)
        assert cell.get('t') == 'n' and Decimal(cell.find('s:v', N).text) == before['total_hours']


def test_submitted_and_approved_import_blocked_and_old_preview_cannot_change_approved(db, time_context):
    c = time_context;r = c['repository'];s = c['sheet_id']
    token = import_rows(c, changes(c, '8'))
    r.transition(s, 'submit')
    with pytest.raises(ValidationError):
        import_rows(c, changes(c, '7'))
    r.transition(s, 'approve')
    with pytest.raises(ValidationError):
        r.apply_preview(s, token)
    with pytest.raises(ValidationError):
        import_rows(c, changes(c, '7'))
    assert record_count(db) == 0 and r.report(s)['version']['status'] == 'approved'


def test_correction_import_keeps_approved_report_and_xlsx_available(db, time_context):
    c = time_context;r = c['repository'];s = c['sheet_id']
    r.apply_preview(s, import_rows(c, changes(c, '8')))
    r.transition(s, 'submit');r.transition(s, 'approve')
    old = r.report(s)['version']['id']
    new = r.create_correction(s, 'Уточнение часов')
    parsed = parse_timesheet_xlsx(workbook(changes(c, '6')))
    token = r.preview_import(s, parsed, 'correction.xlsx', version_id=new)
    r.apply_preview(s, token)
    assert r.report(s, old)['total_hours'] == Decimal('8')
    assert r.report(s, new)['total_hours'] == Decimal('6')
    old_export = parse_timesheet_xlsx(export_timesheet_xlsx(r.report(s, old)))
    assert old_export[0]['hours'] == '8'


def test_attendance_code_snapshot_remains_correct_in_old_export(db, time_context):
    c = time_context;r = c['repository'];s = c['sheet_id']
    r.apply_preview(s, import_rows(c, changes(c, '8')))
    r.transition(s, 'submit');r.transition(s, 'approve')
    db.execute("UPDATE attendance_codes SET code='Я2',name='Новая явка' WHERE code='Я'")
    report = r.report(s)
    assert report['total_hours'] == Decimal('8') and selected_worker(report, c['employee_ids'][0])['entries']['2026-10-01']['code'] == 'Я'
    assert parse_timesheet_xlsx(export_timesheet_xlsx(report))[0]['code'] == 'Я'


def test_foreign_employee_and_wrong_contractor_never_imported(db, time_context):
    c = time_context;r = c['repository'];s = c['sheet_id']
    other = ProjectRepository(db).create_project('Другой объект', 'FOREIGN')
    other_people = PersonnelRepository(db, other)
    contractor = other_people.save_contractor(dict(name='Чужой подрядчик'))
    foreign = other_people.save_worker(dict(full_name='Работник Чужой', contractor_id=contractor,
                specialty_id=c['specialty'], effective_date='2026-10-01'))
    token = import_rows(c, [[foreign, 'Работник Чужой', '2026-10-01', 'Я', 8]])
    assert r.get_preview(s, token)['errors']
    with pytest.raises(ValidationError):
        r.apply_preview(s, token)
    assert record_count(db) == 0
    with pytest.raises(ValidationError):
        TimesheetRepository(db, other).get_preview(s, token)


def test_other_project_reservation_is_rechecked_after_preview(db, time_context):
    c = time_context;r = c['repository'];s = c['sheet_id']
    other = ProjectRepository(db).create_project('Второй объект', 'SHARED')
    c['people'].share_contractor(c['contractor'], other)
    shared_people = PersonnelRepository(db, other)
    shared_people.assign_to_project(c['employee_ids'][0], '2026-10-01')
    shared = TimesheetRepository(db, other)
    other_sheet = shared.create(c['contractor'], 2026, 10)
    token = import_rows(c, changes(c, '16'))
    shared.set_entries(other_sheet, [dict(employee_id=c['employee_ids'][0], work_date='2026-10-01', code='Я', hours='16')])
    with pytest.raises(ValidationError, match='24'):
        r.apply_preview(s, token)
    assert r.report(s)['total_hours'] == 0 and shared.report(other_sheet)['total_hours'] == 16


def test_storage_failure_rolls_back_all_import_rows_and_preview_can_retry(db, time_context, monkeypatch):
    c = time_context;r = c['repository'];s = c['sheet_id']
    token = import_rows(c, changes(c, '8') + changes(c, '7', employee_index=1))
    original = r._write_changes
    def fail(vid, rows):
        original(vid, rows[:1])
        raise sqlite3.OperationalError('Artificial disk failure')
    monkeypatch.setattr(r, '_write_changes', fail)
    with pytest.raises(sqlite3.OperationalError):
        r.apply_preview(s, token)
    assert record_count(db) == 0 and r.get_preview(s, token)['status'] == 'preview'
    monkeypatch.setattr(r, '_write_changes', original)
    r.apply_preview(s, token)
    assert record_count(db) == 2 and r.report(s)['total_hours'] == Decimal('15')


def test_formula_row_never_reaches_saved_cells(db, time_context):
    c = time_context;r = c['repository'];s = c['sheet_id']
    payload = workbook(changes(c, '8'))
    output = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(output, 'w', ZIP_DEFLATED) as target:
        for item in source.infolist():
            body = source.read(item.filename)
            if item.filename == 'xl/worksheets/sheet1.xml':
                body = body.replace(b'<c r="E2" t="n" s="3">', b'<c r="E2" t="n" s="3"><f>1+7</f>')
            target.writestr(item.filename, body)
    token = r.preview_import(s, parse_timesheet_xlsx(output.getvalue()), 'formulas.xlsx')
    assert any('формулы' in error for error in r.get_preview(s, token)['errors'])
    with pytest.raises(ValidationError):
        r.apply_preview(s, token)
    assert record_count(db) == 0


def test_stale_preview_cannot_overwrite_later_edit(db, time_context):
    c = time_context;r = c['repository'];s = c['sheet_id']
    token = import_rows(c, changes(c, '8'))
    r.set_entries(s, [dict(employee_id=c['employee_ids'][0], work_date='2026-10-01', code='Я', hours='4')])
    with pytest.raises(ValidationError, match='изменился'):
        r.apply_preview(s, token)
    assert r.report(s)['total_hours'] == Decimal('4')


def test_absence_with_blank_hours_preview_displays_and_stores_explicit_zero(db, time_context):
    c = time_context;r = c['repository'];s = c['sheet_id'];employee = c['employee_ids'][0]
    rows = parse_timesheet_xlsx(workbook([[employee, 'Работник Первый', '2026-10-01', 'В', None]]))
    assert rows[0]['hours'] is None
    token = r.preview_import(s, rows, 'выходной.xlsx')
    preview = r.get_preview(s, token)
    assert not preview['errors']
    assert preview['changes'][0]['hours'] == '0' and preview['changes'][0]['code'] == 'В'
    assert record_count(db) == 0
    r.apply_preview(s, token)
    entry = selected_worker(r.report(s), employee)['entries']['2026-10-01']
    assert entry['hours'] == Decimal('0') and entry['code'] == 'В'
    assert not entry['counts_as_work'] and record_count(db) == 1


def test_mismatched_name_warns_displays_roster_snapshot_and_never_creates_employee(db, time_context):
    c = time_context;r = c['repository'];s = c['sheet_id'];employee = c['employee_ids'][0]
    # Even a later edit to the master must not replace this version's snapshot.
    c['people'].save_employee(dict(full_name='Работник Переименованный'), employee)
    before = db.execute('SELECT count(*) FROM employees').fetchone()[0]
    token = import_rows(c, [[employee, 'Совсем Другой Человек', '2026-10-01', 'Я', 8]])
    preview = r.get_preview(s, token)
    assert not preview['errors'] and any('ФИО отличается' in warning for warning in preview['warnings'])
    assert preview['changes'][0]['full_name'] == 'Работник Первый'
    assert preview['changes'][0]['employee_id'] == employee
    r.apply_preview(s, token)
    assert db.execute('SELECT count(*) FROM employees').fetchone()[0] == before
    assert c['people'].employee(employee)['full_name'] == 'Работник Переименованный'
    assert selected_worker(r.report(s), employee)['full_name'] == 'Работник Первый'


def test_changed_code_flags_after_preview_block_apply_without_partial_cell_writes(db, time_context):
    c = time_context;r = c['repository'];s = c['sheet_id']
    r.apply_preview(s, import_rows(c, changes(c, '6', work_date='2026-10-03')))
    token = import_rows(c, changes(c, '8') + changes(c, '0', employee_index=1, code='В'))
    assert not r.get_preview(s, token)['errors']
    before = [tuple(row) for row in db.execute('SELECT * FROM timesheet_entries ORDER BY id')]
    event_count = db.execute('SELECT count(*) FROM timesheet_events').fetchone()[0]
    # Both old/new code flags are valid with zero hours. The prepared digest
    # must detect this semantic change even though row validation still passes.
    db.execute("UPDATE attendance_codes SET counts_as_work=1,requires_hours=0 WHERE code='В'")
    with pytest.raises(ValidationError, match='Справочник кодов изменился'):
        r.apply_preview(s, token)
    assert [tuple(row) for row in db.execute('SELECT * FROM timesheet_entries ORDER BY id')] == before
    assert db.execute('SELECT count(*) FROM timesheet_events').fetchone()[0] == event_count
    assert r.get_preview(s, token)['status'] == 'preview'
    assert r.report(s)['total_hours'] == Decimal('6')
