from decimal import Decimal
from pathlib import Path
import shutil
import sqlite3

import pytest

import app.database as database
from app.config import PROJECT_ROOT
from app.calculation_engine.historical import HistoricalCalculator
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.work_order_repository import WorkOrderRepository
from app.repositories.work_repository import WorkRepository
from app.repositories.event_repository import EventRepository
from app.time_utils import instant


@pytest.fixture
def stage40(tmp_path,monkeypatch):
    old=tmp_path/'stage40';(old/'migrations').mkdir(parents=True)
    for file in (PROJECT_ROOT/'migrations').glob('00[1-8]_*.sql'):shutil.copyfile(file,old/'migrations'/file.name)
    with monkeypatch.context() as m:
        m.setattr(database,'PROJECT_ROOT',old);db=database.initialize(tmp_path/'исходная база.sqlite3')
    w=WorkRepository(db);e=EventRepository(db);p=PersonnelRepository(db)
    item=w.add_work_item(1,w.add_floor(1,'3',3),1)
    version=w.create_plan_version('CONSTRUCTION',created_at='2026-01-01T00:00:00Z');w.add_plan_line(version,item,'1000','0.8');w.approve_plan_version(version,approved_at='2026-01-01T01:00:00Z')
    c=p.save_contractor(dict(name='Тестовый подрядчик'));spec=p.save_specialty('Штукатур','ШТ')
    # Preserve incomplete historical personnel without inventing missing data.
    incomplete=p.save_employee(dict(full_name='Исторический прораб'))
    worker=p.save_employee(dict(full_name='Работник Тестовый',contractor_id=c,specialty_id=spec))
    orders=WorkOrderRepository(db)
    order=orders.save_draft(dict(shift_date=instant()[:10],shift_start='08:00',shift_end='17:00',contractor_id=c,issued_by_employee_id=incomplete,foreman_employee_id=worker),
          [dict(work_item_id=item,planned_quantity='120')],[dict(specialty_id=spec,employee_ids=[worker])]);orders.issue(order)
    e.add_fact(item,'600','2026-01-10',recorded_at='2026-01-10T12:00:00Z',event_key='original-fact')
    e.add_confirmation(item,'450','2026-01-10',recorded_at='2026-01-10T13:00:00Z',event_key='original-confirm',document_ref='Акт')
    columns={r[0]:[c[1] for c in db.execute('PRAGMA table_info('+r[0]+')')] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='schema_migrations'")}
    yield dict(db=db,version=version,order=order,columns=columns,root=tmp_path,worker=worker,incomplete=incomplete)
    db.close()


def test_009_preserves_every_original_cell_ids_snapshots_progress_and_checked_backup(stage40):
    s=stage40;db=s['db'];before=database._fingerprints(db,s['columns'])[1]
    order=WorkOrderRepository(db).get_order(s['order']);old_names=order['crew'][0]['names']
    report=HistoricalCalculator(db).calculate_as_of('2026-01-31','2026-02-01T00:00:00Z',s['version'])
    objects={(r[0],r[1]) for r in db.execute("SELECT type,name FROM sqlite_master WHERE type IN ('index','trigger') AND sql IS NOT NULL")}
    database.migrate(db)
    assert database._fingerprints(db,s['columns'])[1]==before
    assert HistoricalCalculator(db).calculate_as_of('2026-01-31','2026-02-01T00:00:00Z',s['version'])==report
    assert (report.total.actual_progress,report.total.confirmed_progress)==(Decimal('.6'),Decimal('.45'))
    assert WorkOrderRepository(db).get_order(s['order'])['crew'][0]['names']==old_names
    assert objects<={(r[0],r[1]) for r in db.execute("SELECT type,name FROM sqlite_master WHERE type IN ('index','trigger') AND sql IS NOT NULL")}
    assert PersonnelRepository(db).employee(s['incomplete'])['needs_details']==1
    assert PersonnelRepository(db).employee(s['worker'])['created_at'] is None
    assert not db.execute('PRAGMA foreign_key_check').fetchall() and db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    copies=list((s['root']/'backups').glob('before-personnel-import-*.sqlite3'));assert len(copies)==1
    backup=database.connect(copies[0])
    assert not backup.execute('PRAGMA foreign_key_check').fetchall() and backup.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert database._fingerprints(backup,s['columns'])[1]==before
    assert not backup.execute("SELECT 1 FROM sqlite_master WHERE name='brigades'").fetchone();backup.close()
    database.migrate(db);assert database._fingerprints(db,s['columns'])[1]==before
    assert len(list((s['root']/'backups').glob('before-personnel-import-*.sqlite3')))==1


@pytest.mark.parametrize('failure',['sql','existing-data'])
def test_009_failure_restores_schema_all_values_triggers_fk_and_keeps_backup(stage40,tmp_path,monkeypatch,failure):
    s=stage40;db=s['db'];before=database._fingerprints(db,s['columns'])[1]
    schema=[tuple(r) for r in db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]
    fake=tmp_path/'broken';shutil.copytree(PROJECT_ROOT/'migrations',fake/'migrations')
    path=fake/'migrations/009_personnel_import.sql'
    path.write_text(path.read_text()+('\nINSERT INTO absent_table VALUES (1);\n' if failure=='sql' else "\nUPDATE employees SET full_name='Ошибка переноса';\n"))
    monkeypatch.setattr(database,'PROJECT_ROOT',fake)
    with pytest.raises((sqlite3.Error,RuntimeError)):database.migrate(db)
    assert database._fingerprints(db,s['columns'])[1]==before
    assert [tuple(r) for r in db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]==schema
    assert db.execute('PRAGMA foreign_keys').fetchone()[0]==1 and db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
    assert len(list((s['root']/'backups').glob('before-personnel-import-*.sqlite3')))==1
