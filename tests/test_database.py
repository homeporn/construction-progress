from decimal import Decimal
import sqlite3
import pytest

from app.database import initialize, migrate
from app.repositories.base import ValidationError
from app.repositories.event_repository import EventRepository
from app.repositories.work_repository import WorkRepository


def test_reopening_and_seeding_is_idempotent(tmp_path):
    path = tmp_path / "переносимая база.sqlite3"
    for _ in range(2):
        db = initialize(path)
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM buildings").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM work_catalog").fetchone()[0] == 15
        assert db.execute("SELECT count(*) FROM floors").fetchone()[0] == 0
        from app.config import PROJECT_ROOT
        assert db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == len(list((PROJECT_ROOT / "migrations").glob("*.sql")))
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        db.close()


def test_custom_floors_units_and_uniqueness(db, work):
    underground = work.add_floor(1, "B2", -2)
    technical = work.add_floor(1, "Технический", 100)
    assert underground != technical
    catalog = work.add_catalog_entry("ПР.01", "Выпуск документации", "Документация", "Раздел АР", "комплект")
    design = work.add_work_item(1, technical, catalog, "DESIGN")
    assert design > 0
    with pytest.raises(ValidationError):
        work.add_floor(1, "B2", -2)
    with pytest.raises(ValidationError):
        work.add_work_item(1, technical, catalog, "DESIGN")
    with pytest.raises(ValidationError):
        work.add_catalog_entry("ПР.01", "Выпуск документации", "Документация", "Другой раздел", "лист")


def test_foreign_keys_and_floor_building_consistency(work):
    floor = work.add_floor(1, "1", 1)
    with pytest.raises(ValidationError):
        work.add_work_item(2, floor, 1)
    with pytest.raises(ValidationError):
        work.add_work_item(1, floor, 999)


@pytest.mark.parametrize("value", ["01", "1.0", "+1", "-0", "1e3", "1.", ".1", "-1", "0.0000000001", "1000000000000000000", "1\x00junk"])
def test_sql_decimal_format_constraints(db, work, item, value):
    version = work.create_plan_version("CONSTRUCTION")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO plan_lines(plan_version_id,work_item_id,planned_quantity,labor_norm,unit) "
                   "VALUES (?,?,?,'1','м²')", (version, item, value))


def test_database_uses_exact_text_not_real(db, work, item, events):
    version = work.create_plan_version("CONSTRUCTION")
    work.add_plan_line(version, item, Decimal("123456789012345678.123456789"), Decimal("0.123456789"))
    line = db.execute("SELECT planned_quantity,labor_norm,typeof(planned_quantity),typeof(labor_norm) FROM plan_lines").fetchone()
    assert tuple(line) == ("123456789012345678.123456789", "0.123456789", "text", "text")
    events.add_fact(item, "0.1", "2026-01-10", event_key="fraction", recorded_at="2026-01-10T12:00:00Z")
    assert db.execute("SELECT quantity_delta,typeof(quantity_delta) FROM fact_events").fetchone()[:] == ("0.1", "text")


def test_referenced_classification_cannot_change_history(db, item, plan):
    for sql in ("UPDATE work_items SET catalog_id=2 WHERE id=?",
                "UPDATE work_catalog SET unit='шт' WHERE id=1",
                "UPDATE floors SET floor_code='новый' WHERE id=1",
                "UPDATE buildings SET name='новый' WHERE id=1"):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, (item,) if "?" in sql else ())


def test_audit_records_changes_and_is_immutable(db, work, item):
    version = work.create_plan_version("CONSTRUCTION")
    work.add_plan_line(version, item, "100", "1")
    work.update_plan_line(version, item, "120", "2")
    audit = db.execute("SELECT details FROM audit_log WHERE entity_type='plan_lines' AND operation='UPDATE'").fetchone()[0]
    assert "OLD.planned_quantity='100'" in audit
    assert "NEW.planned_quantity='120'" in audit
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM audit_log")


@pytest.mark.parametrize("date", ["2026-02-30", "2026-13-01", "2026-00-01", "2026-1-01"])
def test_sql_date_constraints(db, item, date):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO fact_events(work_item_id,quantity_delta,work_date,recorded_at,event_key,unit) "
                   "VALUES (?,'1',?,'2026-12-31T12:00:00.000000Z','bad-date','м²')", (item, date))


def test_migration_failure_rolls_back_ddl(db, tmp_path, monkeypatch):
    import app.database as database
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    (migration_dir / "003_broken.sql").write_text(
        "CREATE TABLE should_not_survive(id INTEGER);\nINSERT INTO nonexistent VALUES (1);\n", encoding="utf-8")
    monkeypatch.setattr(database, "PROJECT_ROOT", tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        migrate(db)
    assert db.execute("SELECT name FROM sqlite_master WHERE name='should_not_survive'").fetchone() is None
    assert db.execute("SELECT name FROM schema_migrations WHERE name='003_broken.sql'").fetchone() is None


def test_replace_cannot_bypass_immutability_even_without_recursive_triggers(db, events, item, plan):
    db.execute("PRAGMA recursive_triggers=OFF")
    original = events.add_fact(item, "100", "2026-01-10", event_key="f", recorded_at="2026-01-10T12:00:00Z")
    statements = [
        ("INSERT OR REPLACE INTO fact_events(id,work_item_id,quantity_delta,work_date,recorded_at,event_key,unit) "
         "VALUES (?,?,'999','2026-01-10','2026-01-10T12:00:00.000000Z','f','м²')", (original, item)),
        ("INSERT OR REPLACE INTO plan_versions(id,discipline,version_no,created_at) "
         "VALUES (?,'CONSTRUCTION',1,'2026-01-01T00:00:00.000000Z')", (plan,)),
        ("INSERT OR REPLACE INTO plan_lines(plan_version_id,work_item_id,planned_quantity,labor_norm,unit) "
         "VALUES (?,?,'999','1','м²')", (plan, item)),
        ("INSERT OR REPLACE INTO work_catalog(id,code,work_type,element,name,unit) "
         "VALUES (1,'ОТД.СТ.01','изменено','изменено','изменено','шт')", ()),
    ]
    for sql, args in statements:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, args)
    assert db.execute("SELECT quantity_delta FROM fact_events WHERE id=?", (original,)).fetchone()[0] == "100"
    assert db.execute("SELECT status FROM plan_versions WHERE id=?", (plan,)).fetchone()[0] == "APPROVED"
