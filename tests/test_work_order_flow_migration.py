from decimal import Decimal
from pathlib import Path
import shutil
import sqlite3

import pytest

import app.database as database
from app.config import PROJECT_ROOT
from app.calculation_engine.historical import HistoricalCalculator
from app.repositories.event_repository import EventRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.work_repository import WorkRepository
from app.repositories.work_order_repository import WorkOrderRepository
from app.time_utils import instant


@pytest.fixture
def stage32(tmp_path,monkeypatch):
    root=tmp_path/'stage32';(root/'migrations').mkdir(parents=True)
    for migration in (PROJECT_ROOT/'migrations').glob('00[1-6]_*.sql'):
        shutil.copyfile(migration,root/'migrations'/migration.name)
    with monkeypatch.context() as patch:
        patch.setattr(database,'PROJECT_ROOT',root)
        db=database.initialize(tmp_path/'существующая.sqlite3')
    w,p=WorkRepository(db),PersonnelRepository(db)
    f=w.add_floor(1,'3',3);i=w.add_work_item(1,f,1)
    v=w.create_plan_version('CONSTRUCTION',created_at='2026-01-01T00:00:00Z')
    w.add_plan_line(v,i,'1000','0.8');w.approve_plan_version(v,approved_at='2026-01-01T01:00:00Z')
    c=p.save_contractor(dict(name='ООО Строй'));sp=p.save_specialty('Штукатур','ШТ')
    e=p.save_employee(dict(full_name='Бригадир',specialty_id=sp,contractor_id=c))
    orders=WorkOrderRepository(db)
    o=orders.save_draft(dict(shift_date=instant()[:10],shift_start='08:00',shift_end='17:00',contractor_id=c,issued_by_employee_id=e,foreman_employee_id=e),
                        [dict(work_item_id=i,planned_quantity='120')],[dict(specialty_id=sp,employee_ids=[e])])
    orders.issue(o)
    events=EventRepository(db)
    events.add_fact(i,'600','2026-01-10',event_key='old-fact',recorded_at='2026-01-10T12:00:00Z')
    events.add_confirmation(i,'450','2026-01-10',event_key='old-confirmation',document_ref='Акт',recorded_at='2026-01-10T13:00:00Z')
    tables={row[0]:[c[1] for c in db.execute('PRAGMA table_info('+row[0]+')')]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='schema_migrations'")}
    yield db,v,o,tables,tmp_path
    db.close()


def test_007_preserves_all_original_tables_and_checked_backup_and_is_repeatable(stage32):
    db,v,o,columns,root=stage32
    before=database._fingerprints(db,columns)[1]
    original=HistoricalCalculator(db).calculate_as_of('2026-01-31','2026-02-01T00:00:00Z',v)
    database.migrate(db)
    assert database._fingerprints(db,columns)[1]==before
    assert HistoricalCalculator(db).calculate_as_of('2026-01-31','2026-02-01T00:00:00Z',v)==original
    assert (original.total.actual_progress,original.total.confirmed_progress)==(Decimal('0.6'),Decimal('0.45'))
    assert db.execute('SELECT work_order_line_id FROM fact_events').fetchone()[0] is None
    assert db.execute('SELECT work_order_line_id FROM confirmation_events').fetchone()[0] is None
    assert db.execute('SELECT source_type FROM fact_events').fetchone()[0]=='manual'
    assert db.execute('SELECT order_number FROM work_orders WHERE id=?',(o,)).fetchone()[0]=='НЗ-0001'
    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok' and not db.execute('PRAGMA foreign_key_check').fetchall()
    backups=list((root/'backups').glob('before-work-order-events-*.sqlite3'))
    assert len(backups)==1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not backup.execute('PRAGMA foreign_key_check').fetchall()
        assert 'work_order_line_id' not in [r[1] for r in backup.execute('PRAGMA table_info(fact_events)')]
        assert backup.execute('SELECT quantity_delta FROM fact_events').fetchone()[0]=='600'
    database.migrate(db)
    assert database._fingerprints(db,columns)[1]==before
    assert len(list((root/'backups').glob('before-work-order-events-*.sqlite3')))==1


@pytest.mark.parametrize('failure',['sql','modify_existing_data'])
def test_failed_007_keeps_original_schema_data_and_backup(stage32,tmp_path,monkeypatch,failure):
    db,_,_,columns,root=stage32
    before=database._fingerprints(db,columns)[1]
    fake=tmp_path/'broken';shutil.copytree(PROJECT_ROOT/'migrations',fake/'migrations')
    file=fake/'migrations/007_work_order_events.sql'
    file.write_text(file.read_text()+('\nINSERT INTO missing_table VALUES(1);\n' if failure=='sql' else "\nUPDATE employees SET full_name='Подмена';\n"))
    monkeypatch.setattr(database,'PROJECT_ROOT',fake)
    with pytest.raises((sqlite3.Error,RuntimeError)):database.migrate(db)
    assert database._fingerprints(db,columns)[1]==before
    assert 'work_order_line_id' not in [r[1] for r in db.execute('PRAGMA table_info(fact_events)')]
    assert db.execute("SELECT 1 FROM schema_migrations WHERE name='007_work_order_events.sql'").fetchone() is None
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='work_order_workflow_actions'").fetchone() is None
    assert len(list((root/'backups').glob('before-work-order-events-*.sqlite3')))==1
    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok' and not db.execute('PRAGMA foreign_key_check').fetchall()
