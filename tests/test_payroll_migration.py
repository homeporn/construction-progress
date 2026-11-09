"""Upgrade a populated 4.3 database, including failure injection, off user paths."""
import shutil
import sqlite3

import pytest

import app.database as database
from app.config import PROJECT_ROOT
from app.calculation_engine.historical import HistoricalCalculator
from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.event_repository import EventRepository
from app.repositories.labor_repository import LaborRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.timesheet_repository import TimesheetRepository
from app.repositories.work_order_repository import WorkOrderRepository
from app.repositories.work_repository import WorkRepository
from app.time_utils import instant


@pytest.fixture
def pre44(tmp_path, monkeypatch):
    previous = tmp_path / 'stage43'
    (previous / 'migrations').mkdir(parents=True)
    for path in (PROJECT_ROOT / 'migrations').glob('*.sql'):
        if path.name < '012':
            shutil.copyfile(path, previous / 'migrations' / path.name)
    with monkeypatch.context() as patch:
        patch.setattr(database, 'PROJECT_ROOT', previous)
        db = database.initialize(tmp_path / 'прежние данные.sqlite3')
    work = WorkRepository(db)
    floor = work.add_floor(1, '3', 3)
    item = work.add_work_item(1, floor, 1)
    plan = work.create_plan_version('CONSTRUCTION', created_at='2026-01-01T00:00:00Z')
    work.add_plan_line(plan, item, '1000', '.5')
    work.approve_plan_version(plan, approved_at='2026-01-01T01:00:00Z')
    events = EventRepository(db)
    fact = events.add_fact(item, '600', '2026-01-10', event_key='old44-fact', recorded_at='2026-01-10T12:00:00Z')
    events.add_confirmation(item, '450', '2026-01-10', event_key='old44-confirm', document_ref='Синтетический акт', recorded_at='2026-01-10T13:00:00Z')
    people = PersonnelRepository(db)
    contractor = people.save_contractor({'name': 'Синтетический подрядчик миграции'})
    specialty = people.save_specialty('Монтажник', 'MIG44')
    employee = people.save_worker(dict(full_name='Синтетический Работник миграции', contractor_id=contractor,
        specialty_id=specialty, effective_date='2020-01-01'))
    brigade = BrigadeRepository(db).save(contractor, '01')
    BrigadeRepository(db).assign(employee, brigade, '2020-01-01')
    sheets = TimesheetRepository(db)
    sheet = sheets.create(contractor, 2026, 1)
    sheets.set_entries(sheet, [dict(employee_id=employee, work_date='2026-01-10', code='Я', hours='8')])
    sheets.transition(sheet, 'submit')
    sheets.transition(sheet, 'approve')
    LaborRepository(db).allocate(item, '2026-01-10', [dict(employee_id=employee, hours='8')])
    LaborRepository(db).attribute(fact, contractor, '600', employee_id=employee)
    orders = WorkOrderRepository(db)
    order = orders.save_draft(dict(shift_date=instant()[:10], shift_start='08:00', shift_end='17:00', contractor_id=contractor,
        issued_by_employee_id=employee, foreman_employee_id=employee),
        [dict(work_item_id=item, planned_quantity='120')], [dict(specialty_id=specialty, employee_ids=[employee])])
    orders.issue(order)
    columns = {r[0]: [c[1] for c in db.execute('PRAGMA table_info("' + r[0] + '")')]
        for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='schema_migrations'")}
    yield db, columns, plan, tmp_path
    db.close()


def test_012_checked_backup_preserves_every_old_row_and_reapplication(pre44):
    db, columns, plan, root = pre44
    before = database._fingerprints(db, columns)[1]
    report = HistoricalCalculator(db).calculate_as_of('2026-01-31', '2026-02-01T00:00:00Z', plan)
    database.migrate(db)
    database.migrate(db)
    assert database._fingerprints(db, columns)[1] == before
    assert HistoricalCalculator(db).calculate_as_of('2026-01-31', '2026-02-01T00:00:00Z', plan) == report
    assert str(report.total.actual_progress) == '0.6' and str(report.total.confirmed_progress) == '0.45'
    copies = list((root / 'backups').glob('before-payroll-*.sqlite3'))
    assert len(copies) == 1
    with sqlite3.connect(copies[0]) as copy:
        copy.row_factory = sqlite3.Row
        assert copy.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not copy.execute('PRAGMA foreign_key_check').fetchall()
        assert copy.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == 11
        assert database._fingerprints(copy, columns)[1] == before
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('failure', ['sql', 'data'])
def test_012_failure_rolls_back_new_schema_and_keeps_verified_backup(pre44, tmp_path, monkeypatch, failure):
    db, columns, _, root = pre44
    before = database._fingerprints(db, columns)[1]
    broken = tmp_path / 'broken'
    shutil.copytree(PROJECT_ROOT / 'migrations', broken / 'migrations')
    path = broken / 'migrations/012_payroll.sql'
    path.write_text(path.read_text() + ('\nINSERT INTO no_such_table VALUES (1);\n' if failure == 'sql' else "\nUPDATE work_catalog SET name='corrupt' WHERE id=1;\n"))
    monkeypatch.setattr(database, 'PROJECT_ROOT', broken)
    with pytest.raises((sqlite3.Error, RuntimeError)):
        database.migrate(db)
    assert database._fingerprints(db, columns)[1] == before
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='payroll_periods'").fetchone()
    assert not db.execute("SELECT 1 FROM schema_migrations WHERE name='012_payroll.sql'").fetchone()
    assert len(list((root / 'backups').glob('before-payroll-*.sqlite3'))) == 1
    assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
