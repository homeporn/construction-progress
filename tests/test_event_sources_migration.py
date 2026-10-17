from decimal import Decimal
import shutil
import sqlite3

import pytest

from app.calculation_engine.historical import HistoricalCalculator
from app.config import PROJECT_ROOT
from app.database import _fingerprints, initialize, migrate
from app.repositories.work_repository import WorkRepository


@pytest.fixture
def stage31(tmp_path, monkeypatch):
    import app.database as database
    old = tmp_path / "stage31"
    (old / "migrations").mkdir(parents=True)
    for name in ("001_initial.sql", "002_replace_guards.sql", "003_web_workflow.sql", "004_projects.sql"):
        shutil.copyfile(PROJECT_ROOT / "migrations" / name, old / "migrations" / name)
    path = tmp_path / "прежняя база.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(database,"PROJECT_ROOT",old)
        db=initialize(path)
    work=WorkRepository(db)
    f=work.add_floor(1,"3",3)
    i=work.add_work_item(1,f,1)
    v=work.create_plan_version("CONSTRUCTION",created_at="2026-01-01T00:00:00Z")
    work.add_plan_line(v,i,"1000","0.8")
    work.approve_plan_version(v,approved_at="2026-01-01T01:00:00Z")
    db.execute("INSERT INTO fact_events(work_item_id,quantity_delta,work_date,recorded_at,event_key,unit) VALUES (?,'600','2026-01-10','2026-01-10T12:00:00.000000Z','old-f','м²')",(i,))
    db.execute("INSERT INTO confirmation_events(work_item_id,quantity_delta,confirmed_date,recorded_at,event_key,unit,document_ref) VALUES (?,'450','2026-01-10','2026-01-10T13:00:00.000000Z','old-c','м²','Акт')",(i,))
    yield db,path,v
    db.close()


def test_source_migration_preserves_ids_quantities_timestamps_audit_and_progress(stage31):
    db,path,v=stage31
    columns,before=_fingerprints(db)
    original=HistoricalCalculator(db).calculate_as_of("2026-01-31","2026-01-31T23:00:00Z",v)
    migrate(db)
    assert _fingerprints(db,columns)[1]==before
    after=HistoricalCalculator(db).calculate_as_of("2026-01-31","2026-01-31T23:00:00Z",v)
    assert after.total==original.total
    assert (after.total.actual_progress,after.total.confirmed_progress)==(Decimal("0.6"),Decimal("0.45"))
    assert db.execute("SELECT source_type FROM fact_events").fetchone()[0]=="legacy"
    assert db.execute("SELECT source_type FROM confirmation_events").fetchone()[0]=="legacy"
    assert db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
    backups=list((path.parent/"backups").glob("before-event-flow-*.sqlite3"))
    assert len(backups)==1
    with sqlite3.connect(backups[0]) as backup:
        assert "source_type" not in [r[1] for r in backup.execute("PRAGMA table_info(fact_events)")]
        assert backup.execute("SELECT quantity_delta FROM fact_events").fetchone()[0]=="600"
        assert backup.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
    migrate(db)
    assert _fingerprints(db,columns)[1]==before
    assert len(list((path.parent/"backups").glob("before-event-flow-*.sqlite3")))==1


def test_failed_source_migration_is_atomic_and_backup_is_kept(stage31,tmp_path,monkeypatch):
    import app.database as database
    db,path,_=stage31
    before=_fingerprints(db)
    root=tmp_path/"broken"
    (root/"migrations").mkdir(parents=True)
    (root/"migrations/005_event_sources.sql").write_text(
        (PROJECT_ROOT/"migrations/005_event_sources.sql").read_text()+"\nINSERT INTO missing_table VALUES (1);\n")
    monkeypatch.setattr(database,"PROJECT_ROOT",root)
    with pytest.raises(sqlite3.Error):migrate(db)
    assert _fingerprints(db)==before
    assert "source_type" not in [r[1] for r in db.execute("PRAGMA table_info(fact_events)")]
    assert db.execute("SELECT name FROM schema_migrations WHERE name='005_event_sources.sql'").fetchone() is None
    assert len(list((path.parent/"backups").glob("before-event-flow-*.sqlite3")))==1
