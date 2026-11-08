import hashlib
from pathlib import Path
import shutil
import sqlite3
import pytest
import app.database as database
from app.config import PROJECT_ROOT
from app.calculation_engine.historical import HistoricalCalculator
from app.repositories.work_repository import WorkRepository
from app.repositories.event_repository import EventRepository

@pytest.fixture
def pre43(tmp_path,monkeypatch):
 old=tmp_path/'old';(old/'migrations').mkdir(parents=True)
 for p in (PROJECT_ROOT/'migrations').glob('*.sql'):
  if p.name<'011':shutil.copyfile(p,old/'migrations'/p.name)
 with monkeypatch.context() as m:
  m.setattr(database,'PROJECT_ROOT',old);db=database.initialize(tmp_path/'old.sqlite3')
 w=WorkRepository(db);f=w.add_floor(1,'3',3);i=w.add_work_item(1,f,1)
 p=w.create_plan_version('CONSTRUCTION',created_at='2026-01-01T00:00:00Z');w.add_plan_line(p,i,'1000','.5');w.approve_plan_version(p,approved_at='2026-01-01T01:00:00Z')
 e=EventRepository(db);e.add_fact(i,'600','2026-01-10',event_key='old',recorded_at='2026-01-10T12:00:00Z')
 e.add_confirmation(i,'450','2026-01-10',event_key='oldc',document_ref='Тест',recorded_at='2026-01-10T13:00:00Z')
 columns={r[0]:[c[1] for c in db.execute('pragma table_info("'+r[0]+'")')] for r in db.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' and name!='schema_migrations'")}
 yield db,columns,p,tmp_path
 db.close()

def test_migration_backup_preserves_old_ids_and_60_45_and_repeats(pre43):
 db,columns,p,root=pre43;before=database._fingerprints(db,columns)[1]
 report=HistoricalCalculator(db).calculate_as_of('2026-01-31','2026-02-01T00:00:00Z',p)
 database.migrate(db);database.migrate(db)
 assert database._fingerprints(db,columns)[1]==before
 assert HistoricalCalculator(db).calculate_as_of('2026-01-31','2026-02-01T00:00:00Z',p)==report
 assert str(report.total.actual_progress)=='0.6' and str(report.total.confirmed_progress)=='0.45'
 copies=list((root/'backups').glob('before-labor-productivity*.sqlite3'));assert len(copies)==1
 copy=sqlite3.connect(copies[0]);assert copy.execute('pragma integrity_check').fetchone()[0]=='ok';assert not copy.execute('pragma foreign_key_check').fetchall();assert copy.execute('select count(*) from schema_migrations').fetchone()[0]==10;copy.close()
 assert db.execute('pragma integrity_check').fetchone()[0]=='ok' and not db.execute('pragma foreign_key_check').fetchall()

@pytest.mark.parametrize('failure',['sql','data'])
def test_011_failure_rolls_back_and_keeps_backup(pre43,tmp_path,monkeypatch,failure):
 db,columns,p,root=pre43;before=database._fingerprints(db,columns)[1]
 broken=tmp_path/'broken';shutil.copytree(PROJECT_ROOT/'migrations',broken/'migrations')
 path=broken/'migrations/011_labor_productivity.sql';path.write_text(path.read_text()+('\nINSERT INTO no_such_table VALUES (1);\n' if failure=='sql' else "\nUPDATE work_catalog SET name='corrupt' WHERE id=1;\n"))
 monkeypatch.setattr(database,'PROJECT_ROOT',broken)
 with pytest.raises((sqlite3.Error,RuntimeError)):database.migrate(db)
 assert database._fingerprints(db,columns)[1]==before
 assert not db.execute("select 1 from sqlite_master where name='labor_operations'").fetchone()
 assert len(list((root/'backups').glob('before-labor-productivity*.sqlite3')))==1
