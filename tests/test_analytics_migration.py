from decimal import Decimal
from pathlib import Path
import shutil
import sqlite3

import pytest

import app.database as database
from app.config import PROJECT_ROOT
from app.calculation_engine.historical import HistoricalCalculator
from app.repositories.work_repository import WorkRepository
from app.repositories.event_repository import EventRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.work_order_repository import WorkOrderRepository
from app.time_utils import instant


@pytest.fixture
def stage33(tmp_path,monkeypatch):
    old=tmp_path/'old';(old/'migrations').mkdir(parents=True)
    for file in (PROJECT_ROOT/'migrations').glob('00[1-7]_*.sql'):shutil.copyfile(file,old/'migrations'/file.name)
    with monkeypatch.context() as m:
        m.setattr(database,'PROJECT_ROOT',old);db=database.initialize(tmp_path/'старые данные.sqlite3')
    w=WorkRepository(db);e=EventRepository(db);p=PersonnelRepository(db)
    f=w.add_floor(1,'1',1);i=w.add_work_item(1,f,1)
    v=w.create_plan_version('CONSTRUCTION',created_at='2026-01-01T00:00:00Z');w.add_plan_line(v,i,'1000','0.8');w.approve_plan_version(v,approved_at='2026-01-01T01:00:00Z')
    c=p.save_contractor(dict(name='ООО Строй'));sp=p.save_specialty('Штукатур','ШТ');person=p.save_employee(dict(full_name='Прораб',contractor_id=c,specialty_id=sp))
    o=WorkOrderRepository(db);order=o.save_draft(dict(shift_date=instant()[:10],shift_start='08:00',shift_end='17:00',contractor_id=c,issued_by_employee_id=person,foreman_employee_id=person),[dict(work_item_id=i,planned_quantity='120')],[dict(specialty_id=sp,employee_ids=[person])]);o.issue(order)
    e.add_fact(i,'600','2026-01-10',event_key='old-fact',recorded_at='2026-01-10T12:00:00Z');e.add_confirmation(i,'450','2026-01-10',event_key='old-confirm',document_ref='Акт',recorded_at='2026-01-10T13:00:00Z')
    columns={r[0]:[c[1] for c in db.execute('PRAGMA table_info('+r[0]+')')] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='schema_migrations'")}
    yield db,v,order,columns,tmp_path
    db.close()


def test_008_preserves_every_table_calculation_order_and_backup(stage33):
    db,v,o,cols,root=stage33;before=database._fingerprints(db,cols)[1]
    report=HistoricalCalculator(db).calculate_as_of('2026-01-31','2026-02-01T00:00:00Z',v)
    database.migrate(db)
    assert database._fingerprints(db,cols)[1]==before
    after=HistoricalCalculator(db).calculate_as_of('2026-01-31','2026-02-01T00:00:00Z',v)
    assert after==report and (after.total.actual_progress,after.total.confirmed_progress)==(Decimal('.6'),Decimal('.45'))
    assert db.execute('SELECT order_number,status FROM work_orders WHERE id=?',(o,)).fetchone()[:]==('НЗ-0001','issued')
    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok' and not db.execute('PRAGMA foreign_key_check').fetchall()
    backups=list((root/'backups').glob('before-analytics-*.sqlite3'));assert len(backups)==1
    with sqlite3.connect(backups[0]) as b:
        assert b.execute('PRAGMA integrity_check').fetchone()[0]=='ok' and not b.execute('PRAGMA foreign_key_check').fetchall()
        assert 'reference_labor_norm' not in [r[1] for r in b.execute('PRAGMA table_info(work_catalog)')]
    database.migrate(db);assert database._fingerprints(db,cols)[1]==before
    assert len(list((root/'backups').glob('before-analytics-*.sqlite3')))==1


@pytest.mark.parametrize('failure',['sql','modify_existing'])
def test_008_failure_rolls_back_schema_and_data_keeps_checked_backup(stage33,tmp_path,monkeypatch,failure):
    db,_,_,cols,root=stage33;before=database._fingerprints(db,cols)[1]
    fake=tmp_path/'broken';shutil.copytree(PROJECT_ROOT/'migrations',fake/'migrations')
    file=fake/'migrations/008_analytics.sql'
    file.write_text(file.read_text()+('\nINSERT INTO missing_table VALUES(1);\n' if failure=='sql' else "\nUPDATE employees SET full_name='Повреждение';\n"))
    monkeypatch.setattr(database,'PROJECT_ROOT',fake)
    with pytest.raises((sqlite3.Error,RuntimeError)):database.migrate(db)
    assert database._fingerprints(db,cols)[1]==before
    assert 'reference_labor_norm' not in [r[1] for r in db.execute('PRAGMA table_info(work_catalog)')]
    assert not db.execute("SELECT 1 FROM schema_migrations WHERE name='008_analytics.sql'").fetchone()
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='analytical_groups'").fetchone()
    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert len(list((root/'backups').glob('before-analytics-*.sqlite3')))==1
