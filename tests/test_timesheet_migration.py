"""Upgrade an actual stage-4.1 database; never open the user's database."""
from decimal import Decimal
import shutil
import sqlite3

import pytest

import app.database as database
from app.config import PROJECT_ROOT
from app.calculation_engine.historical import HistoricalCalculator
from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.event_repository import EventRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.work_order_repository import WorkOrderRepository
from app.repositories.work_repository import WorkRepository
from app.time_utils import instant


@pytest.fixture
def stage41(tmp_path, monkeypatch):
    old = tmp_path / 'old'
    (old / 'migrations').mkdir(parents=True)
    for path in (PROJECT_ROOT / 'migrations').glob('00[1-9]_*.sql'):
        shutil.copyfile(path, old / 'migrations' / path.name)
    with monkeypatch.context() as patch:
        patch.setattr(database, 'PROJECT_ROOT', old)
        db = database.initialize(tmp_path / 'Старая база с пробелами.sqlite3')
    work, people = WorkRepository(db), PersonnelRepository(db)
    contractor = people.save_contractor({'name': 'Учебный подрядчик'})
    specialty = people.save_specialty('Учебная специальность', 'TEST-42')
    employee = people.save_worker(dict(full_name='Учебный Работник', contractor_id=contractor,
                                       specialty_id=specialty, effective_date='2026-01-01'))
    brigades = BrigadeRepository(db)
    brigade = brigades.save(contractor, '01')
    brigades.assign(employee, brigade, '2026-01-01')
    floor = work.add_floor(1, '3', 3)
    item = work.add_work_item(1, floor, 1)
    version = work.create_plan_version('CONSTRUCTION', created_at='2026-01-01T00:00:00Z')
    work.add_plan_line(version, item, '1000', '0.8')
    work.approve_plan_version(version, approved_at='2026-01-01T01:00:00Z')
    events = EventRepository(db)
    events.add_fact(item, '600', '2026-01-10', recorded_at='2026-01-10T12:00:00Z', event_key='old-fact')
    events.add_confirmation(item, '450', '2026-01-10', recorded_at='2026-01-10T13:00:00Z',
                            document_ref='Учебный акт', event_key='old-confirmation')
    orders = WorkOrderRepository(db)
    order = orders.save_draft(dict(shift_date=instant()[:10], shift_start='08:00', shift_end='17:00',
                                  contractor_id=contractor, issued_by_employee_id=employee,
                                  foreman_employee_id=employee),
                             [dict(work_item_id=item, planned_quantity='120')],
                             [dict(specialty_id=specialty, employee_ids=[employee])])
    orders.issue(order)
    columns = {row[0]: [column[1] for column in db.execute('PRAGMA table_info("{}")'.format(row[0]))]
               for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                     "AND name NOT LIKE 'sqlite_%' AND name!='schema_migrations'")}
    yield dict(db=db, columns=columns, contractor=contractor, employee=employee,
               plan_version=version, order=order, root=tmp_path)
    db.close()


def test_010_preserves_old_cells_ids_crew_events_and_progress_with_verified_backup(stage41):
    data, db = stage41, stage41['db']
    before = database._fingerprints(db, data['columns'])[1]
    cutoff = instant()
    old_document = WorkOrderRepository(db).get_order(data['order'], knowledge_cutoff=cutoff)
    report = HistoricalCalculator(db).calculate_as_of('2026-01-31', '2026-02-01T00:00:00Z', data['plan_version'])
    old_objects = {(row[0], row[1]) for row in db.execute(
        "SELECT type,name FROM sqlite_master WHERE type IN ('index','trigger') AND sql IS NOT NULL")}
    database.migrate(db)
    assert database._fingerprints(db, data['columns'])[1] == before
    upgraded_document=WorkOrderRepository(db).get_order(data['order'], knowledge_cutoff=cutoff)
    #013 adds derived control fields. Every pre-existing document field must
    #remain identical; additive analytical keys are not stored-data corruption.
    assert [{key:new[key] for key in old} for old,new in zip(old_document['lines'],upgraded_document['lines'])]==old_document['lines']
    assert len(upgraded_document['lines'])==len(old_document['lines'])
    assert {key:value for key,value in upgraded_document.items() if key!='lines'}=={key:value for key,value in old_document.items() if key!='lines'}
    assert HistoricalCalculator(db).calculate_as_of('2026-01-31', '2026-02-01T00:00:00Z', data['plan_version']) == report
    assert (report.total.actual_progress, report.total.confirmed_progress) == (Decimal('.6'), Decimal('.45'))
    assert old_objects <= {(row[0], row[1]) for row in db.execute(
        "SELECT type,name FROM sqlite_master WHERE type IN ('index','trigger') AND sql IS NOT NULL")}
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
    assert [row[0] for row in db.execute('SELECT code FROM attendance_codes ORDER BY id')] == ['Я', 'В', 'ОТ', 'Б', 'Н']
    copies = list((data['root'] / 'backups').glob('before-timesheets-*.sqlite3'))
    assert len(copies) == 1
    backup = database.connect(copies[0])
    try:
        assert database._fingerprints(backup, data['columns'])[1] == before
        assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not backup.execute('PRAGMA foreign_key_check').fetchall()
        assert not backup.execute("SELECT 1 FROM sqlite_master WHERE name='timesheets'").fetchone()
    finally:
        backup.close()
    database.migrate(db)
    assert database._fingerprints(db, data['columns'])[1] == before
    assert db.execute('SELECT COUNT(*) FROM attendance_codes').fetchone()[0] == 5
    assert len(list((data['root'] / 'backups').glob('before-timesheets-*.sqlite3'))) == 1


@pytest.mark.parametrize('failure', ['sql', 'old-data'])
def test_010_failed_upgrade_rolls_back_all_schema_and_data_and_retains_backup(stage41, tmp_path, monkeypatch, failure):
    data, db = stage41, stage41['db']
    before = database._fingerprints(db, data['columns'])[1]
    schema = [tuple(row) for row in db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]
    broken = tmp_path / 'broken'
    shutil.copytree(PROJECT_ROOT / 'migrations', broken / 'migrations')
    migration = broken / 'migrations' / '010_timesheets.sql'
    with migration.open('a', encoding='utf-8') as stream:
        stream.write('\nINSERT INTO intentionally_missing_table VALUES (1);\n' if failure == 'sql'
                     else "\nUPDATE work_catalog SET reference_labor_norm='0.75' WHERE id=1;\n")
    monkeypatch.setattr(database, 'PROJECT_ROOT', broken)
    with pytest.raises((sqlite3.Error, RuntimeError)):
        database.migrate(db)
    assert database._fingerprints(db, data['columns'])[1] == before
    assert [tuple(row) for row in db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')] == schema
    assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
    assert len(list((data['root'] / 'backups').glob('before-timesheets-*.sqlite3'))) == 1


def _empty_version(data):
    db, now = data['db'], instant()
    sheet = db.execute('INSERT INTO timesheets(project_id,contractor_id,year,month,created_at,updated_at) '
                       'VALUES (1,?,2026,10,?,?)', (data['contractor'], now, now)).lastrowid
    version = db.execute('INSERT INTO timesheet_versions(timesheet_id,version_no,created_at,updated_at) '
                         'VALUES (?,1,?,?)', (sheet, now, now)).lastrowid
    db.execute('INSERT INTO timesheet_workers(version_id,employee_id,full_name,specialty_name,brigade_number) '
               "VALUES (?,?,'Учебный Работник','Учебная специальность','01')", (version, data['employee']))
    return sheet, version


def _entry(db, version, employee, hours=8000000000, work_date='2026-10-01'):
    now = instant()
    return db.execute('INSERT INTO timesheet_entries(version_id,employee_id,work_date,attendance_code_id,code_snapshot,'
                      'counts_as_work,requires_hours,hours_nanos,created_at,updated_at) '
                      "VALUES (?,?,?,1,'Я',1,1,?,?,?)", (version, employee, work_date, hours, now, now)).lastrowid


@pytest.mark.parametrize('hours', [-1, 24000000001, '8.5'])
def test_010_nanohours_require_bounded_integers(stage41, hours):
    database.migrate(stage41['db'])
    _, version = _empty_version(stage41)
    with pytest.raises(sqlite3.IntegrityError):
        _entry(stage41['db'], version, stage41['employee'], hours=hours)


@pytest.mark.parametrize('date', ['2026-11-01', '2026-10-32', '2026-02-30'])
def test_010_rejects_invalid_dates_and_dates_outside_month(stage41, date):
    database.migrate(stage41['db'])
    _, version = _empty_version(stage41)
    with pytest.raises(sqlite3.IntegrityError):
        _entry(stage41['db'], version, stage41['employee'], work_date=date)


def test_010_approved_rows_snapshots_and_events_are_immutable(stage41):
    db = stage41['db']
    database.migrate(db)
    _, version = _empty_version(stage41)
    entry = _entry(db, version, stage41['employee'], hours=8125000000)
    now = instant()
    db.execute("INSERT INTO timesheet_events(version_id,operation,recorded_at,details) VALUES (?,'created',?,'{}')", (version, now))
    db.execute("UPDATE timesheet_versions SET status='submitted',submitted_at=?,updated_at=? WHERE id=?", (now, now, version))
    db.execute("UPDATE timesheet_versions SET status='approved',approved_at=?,updated_at=? WHERE id=?", (now, now, version))
    for sql, args in [
        ('UPDATE timesheet_entries SET hours_nanos=1 WHERE id=?', (entry,)),
        ('DELETE FROM timesheet_entries WHERE id=?', (entry,)),
        ("UPDATE timesheet_workers SET full_name='Изменён' WHERE version_id=?", (version,)),
        ('DELETE FROM timesheet_workers WHERE version_id=?', (version,)),
        ("UPDATE timesheet_versions SET comment='Изменён' WHERE id=?", (version,)),
        ('DELETE FROM timesheet_versions WHERE id=?', (version,)),
        ("UPDATE timesheet_events SET details='{}' WHERE version_id=?", (version,)),
        ('DELETE FROM timesheet_events WHERE version_id=?', (version,)),
    ]:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, args)
    db.execute("UPDATE attendance_codes SET code='ЯВ',name='Новое название' WHERE id=1")
    frozen = db.execute('SELECT code_snapshot,hours_nanos FROM timesheet_entries WHERE id=?', (entry,)).fetchone()
    assert tuple(frozen) == ('Я', 8125000000)


def test_010_correction_preserves_original_code_snapshot_and_single_open_version(stage41):
    db = stage41['db']
    database.migrate(db)
    sheet, first = _empty_version(stage41)
    _entry(db, first, stage41['employee'])
    now = instant()
    db.execute("UPDATE timesheet_versions SET status='submitted',submitted_at=? WHERE id=?", (now, first))
    db.execute("UPDATE timesheet_versions SET status='approved',approved_at=? WHERE id=?", (now, first))
    db.execute("UPDATE attendance_codes SET code='ЯВ' WHERE id=1")
    correction = db.execute('INSERT INTO timesheet_versions(timesheet_id,version_no,base_version_id,correction_reason,created_at,updated_at) '
                            "VALUES (?,2,?,'Исправление',?,?)", (sheet, first, now, now)).lastrowid
    db.execute('INSERT INTO timesheet_workers(version_id,employee_id,full_name,specialty_name,brigade_number) SELECT ?,employee_id,full_name,specialty_name,brigade_number '
               'FROM timesheet_workers WHERE version_id=?', (correction, first))
    _entry(db, correction, stage41['employee'], hours=7000000000)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute('INSERT INTO timesheet_versions(timesheet_id,version_no,base_version_id,correction_reason,created_at,updated_at) '
                   "VALUES (?,3,?,'Ещё исправление',?,?)", (sheet, first, now, now))
    assert db.execute('SELECT hours_nanos FROM timesheet_entries WHERE version_id=?', (first,)).fetchone()[0] == 8000000000
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
