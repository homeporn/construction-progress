from decimal import Decimal
from pathlib import Path
import shutil
import sqlite3

import pytest

from app.calculation_engine.historical import HistoricalCalculator
from app.calculation_engine.models import WorkInput
from app.calculation_engine.progress import calculate_item
from app.config import PROJECT_ROOT
from app.database import _fingerprints, initialize, migrate


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    import app.database as database
    old = tmp_path / "old"
    (old / "migrations").mkdir(parents=True)
    for name in ("001_initial.sql", "002_replace_guards.sql", "003_web_workflow.sql"):
        shutil.copyfile(PROJECT_ROOT / "migrations" / name, old / "migrations" / name)
    path = tmp_path / "каталог с пробелами" / "рабочая.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(database, "PROJECT_ROOT", old)
        db = initialize(path)
    db.execute("INSERT INTO floors(id,building_id,floor_code,sort_order) VALUES (11,1,'Тех',100)")
    db.execute("INSERT INTO work_items(id,building_id,floor_id,catalog_id,discipline) VALUES (21,1,11,1,'CONSTRUCTION')")
    db.execute("INSERT INTO plan_versions(id,discipline,version_no,created_at) VALUES (31,'CONSTRUCTION',1,'2026-01-01T00:00:00.000000Z')")
    db.execute("INSERT INTO plan_lines(id,plan_version_id,work_item_id,planned_quantity,labor_norm,unit) VALUES (41,31,21,'1000','0.8','м²')")
    db.execute("UPDATE plan_versions SET status='APPROVED',approved_at='2026-01-01T01:00:00.000000Z' WHERE id=31")
    db.execute("INSERT INTO plan_versions(id,discipline,version_no,created_at) VALUES (32,'CONSTRUCTION',2,'2026-01-02T00:00:00.000000Z')")
    db.execute("INSERT INTO plan_lines(id,plan_version_id,work_item_id,planned_quantity,labor_norm,unit) VALUES (42,32,21,'1200','0.8','м²')")
    db.execute("INSERT INTO fact_events(id,work_item_id,quantity_delta,work_date,recorded_at,event_key,unit) VALUES (51,21,'600','2026-01-10','2026-01-11T12:00:00.000000Z','legacy-f','м²')")
    db.execute("INSERT INTO fact_events(id,work_item_id,quantity_delta,work_date,recorded_at,event_key,unit,correction_of_id) VALUES (52,21,'-20','2026-01-12','2026-01-12T12:00:00.000000Z','legacy-f-c','м²',51)")
    db.execute("INSERT INTO confirmation_events(id,work_item_id,quantity_delta,confirmed_date,recorded_at,event_key,unit,document_ref,comment) VALUES (61,21,'450','2026-01-11','2026-01-11T12:00:00.000000Z','legacy-c','м²','Акт 1','Комментарий')")
    yield db, path
    db.close()


def test_migration_preserves_every_original_id_value_event_and_version(legacy):
    db, path = legacy
    columns, before = _fingerprints(db)
    old_triggers = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    migrate(db)
    assert _fingerprints(db, columns)[1] == before
    assert db.execute("SELECT name FROM projects WHERE id=1").fetchone()[0] == "Строительный проект"
    assert {r[0] for r in db.execute("SELECT project_id FROM buildings")} == {1}
    assert {r[0] for r in db.execute("SELECT project_id FROM plan_versions")} == {1}
    assert old_triggers <= {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    backups = list((path.parent / "backups").glob("before-projects-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("PRAGMA foreign_key_check").fetchall() == []
        assert copy.execute("SELECT count(*) FROM sqlite_master WHERE name='projects'").fetchone()[0] == 0
        assert copy.execute("SELECT quantity_delta FROM fact_events WHERE id=52").fetchone()[0] == "-20"


def test_migration_does_not_change_progress_or_knowledge_cutoff(legacy):
    db, _ = legacy
    # Capture expected progress through the original pure Decimal engine before DDL.
    before = calculate_item(WorkInput(work_item_id=21, discipline="CONSTRUCTION",
        planned_quantity=Decimal("1000"), labor_norm=Decimal("0.8"),
        actual=Decimal("580"), confirmed=Decimal("450")))
    migrate(db)
    after = HistoricalCalculator(db).calculate_as_of("2026-01-31", "2026-01-31T23:00:00Z", 31).total
    assert (after.planned_labor, after.actual_progress, after.confirmed_progress, after.remaining_labor) == (
        before.planned_labor, before.actual_progress, before.confirmed_progress, before.remaining_labor)
    assert after.actual_progress == Decimal("0.58")
    early = HistoricalCalculator(db).calculate_as_of("2026-01-31", "2026-01-10T23:00:00Z", 31).total
    assert early.actual_progress == 0


def test_repeated_migration_and_reopening_do_not_seed_duplicates(legacy):
    db, path = legacy
    migrate(db)
    counts = _fingerprints(db)[1]
    for _ in range(2):
        migrate(db)
        other = initialize(path)
        other.close()
    assert counts == _fingerprints(db)[1]
    assert db.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
    assert len(list((path.parent / "backups").glob("*.sqlite3"))) == 1


@pytest.mark.parametrize("failure", ["sql", "alter_data", "foreign_key"])
def test_failed_rebuild_rolls_back_data_schema_and_restores_pragmas(legacy, tmp_path, monkeypatch, failure):
    import app.database as database
    db, path = legacy
    before = _fingerprints(db)
    old_schema = [tuple(r) for r in db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
    bad = tmp_path / "bad"
    (bad / "migrations").mkdir(parents=True)
    script = (PROJECT_ROOT / "migrations/004_projects.sql").read_text()
    script += {"sql": "\nINSERT INTO missing_table VALUES(1);\n",
               "alter_data": "\nDROP TRIGGER fact_events_no_update;\nUPDATE fact_events SET comment='changed' WHERE id=51;\n",
               "foreign_key": "\nDROP TRIGGER buildings_stable;\nDROP TRIGGER audit_buildings_update;\nUPDATE buildings SET project_id=999;\n"}[failure]
    (bad / "migrations/004_projects.sql").write_text(script)
    monkeypatch.setattr(database, "PROJECT_ROOT", bad)
    with pytest.raises((sqlite3.Error, RuntimeError)):
        migrate(db)
    assert before == _fingerprints(db)
    assert old_schema == [tuple(r) for r in db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert len(list((path.parent / "backups").glob("*.sqlite3"))) == 1


def test_backup_failure_prevents_any_migration(legacy, monkeypatch):
    import app.database as database
    db, _ = legacy
    before = _fingerprints(db)
    def fail(_):
        raise OSError("Нельзя сохранить резервную копию")
    monkeypatch.setattr(database, "backup_database", fail)
    with pytest.raises(OSError):
        migrate(db)
    assert before == _fingerprints(db)
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='projects'").fetchone() is None


def test_backup_includes_committed_rows_in_uncheckpointed_wal(legacy):
    db, path = legacy
    assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    db.execute("UPDATE plan_lines SET planned_quantity='1300' WHERE id=42")
    assert Path(str(path) + "-wal").stat().st_size > 0
    migrate(db)
    backup = next((path.parent / "backups").glob("before-projects-*.sqlite3"))
    with sqlite3.connect(backup) as copy:
        assert copy.execute("SELECT planned_quantity FROM plan_lines WHERE id=42").fetchone()[0] == "1300"
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_startup_migration_failure_has_readable_console_message(tmp_path, monkeypatch, capsys):
    import main
    from app.web import server
    def fail(*args, **kwargs):
        raise RuntimeError("Резервная копия не прошла проверку; обновление отменено")
    monkeypatch.setattr(server, "run", fail)
    monkeypatch.setattr("sys.argv", ["main.py", "--db", str(tmp_path / "not-created.sqlite3"), "--no-browser"])
    assert main.main() == 2
    captured = capsys.readouterr()
    assert "обновление отменено" in captured.out
    assert "Traceback" not in captured.out + captured.err
    assert not (tmp_path / "not-created.sqlite3").exists()
