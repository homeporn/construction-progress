import sqlite3
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.config import DEFAULT_DB_PATH, MIN_SQLITE, PROJECT_ROOT


def connect(path=DEFAULT_DB_PATH):
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RuntimeError("SQLite >= 3.24.0 required")
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA recursive_triggers = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def backup_database(db):
    """Consistent, checked pre-upgrade copy; never overwrite an earlier backup."""
    filename = db.execute("PRAGMA database_list").fetchone()[2]
    if not filename:  # An in-memory test database has no persistent original.
        return None
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("База повреждена: обновление отменено")
    folder = Path(filename).parent / "backups"
    folder.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = folder / ("before-projects-{}-{}.sqlite3".format(stamp, uuid4().hex[:8]))
    target = sqlite3.connect(str(path))
    try:
        db.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or target.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Резервная копия не прошла проверку; обновление отменено")
    finally:
        target.close()
    return path


def _fingerprints(db, columns=None):
    """Compare original columns, IDs and values without loading a database into RAM."""
    if columns is None:
        columns = {table: [r[1] for r in db.execute('PRAGMA table_info("{}")'.format(table))]
                   for table in ("buildings", "floors", "work_catalog", "work_items", "plan_versions",
                                 "plan_lines", "fact_events", "confirmation_events", "audit_log")}
    result = {}
    for table, names in columns.items():
        digest, count = hashlib.sha256(), 0
        fields = ",".join('"{}"'.format(name) for name in names)
        for row in db.execute('SELECT {} FROM "{}" ORDER BY id'.format(fields, table)):
            digest.update(json.dumps(tuple(row), ensure_ascii=False).encode("utf-8"))
            count += 1
        result[table] = (count, digest.hexdigest())
    return columns, result


def migrate(db):
    """Each SQL migration, its DDL and version marker commit atomically."""
    if db.in_transaction:
        raise RuntimeError("Run migrations outside a transaction")
    existing = db.execute("SELECT 1 FROM sqlite_master WHERE name='buildings'").fetchone()
    markers = db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
    upgraded = markers and db.execute("SELECT 1 FROM schema_migrations WHERE name='004_projects.sql'").fetchone()
    if existing and not upgraded and (PROJECT_ROOT / "migrations/004_projects.sql").exists():
        backup_database(db)
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
               "(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    for path in sorted((PROJECT_ROOT / "migrations").glob("*.sql")):
        # executescript commits an existing transaction, so never accept one.
        if db.in_transaction:
            raise RuntimeError("Run migrations outside a transaction")
        rebuild = path.name == "004_projects.sql"
        foreign_keys = db.execute("PRAGMA foreign_keys").fetchone()[0]
        legacy_row = db.execute("PRAGMA legacy_alter_table").fetchone()
        # SQLite 3.24 already uses the old rename behavior and has no such pragma.
        legacy = legacy_row[0] if legacy_row else 0
        if rebuild:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("PRAGMA legacy_alter_table=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM schema_migrations WHERE name=?", (path.name,)).fetchone():
                db.commit()
                continue
            preserved = _fingerprints(db) if rebuild else None
            # Execute complete SQL statements without executescript's implicit COMMIT.
            statement = ""
            for line in path.read_text(encoding="utf-8").splitlines(True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            if statement.strip():
                raise RuntimeError("Incomplete migration: " + path.name)
            if rebuild:
                if _fingerprints(db, preserved[0])[1] != preserved[1]:
                    raise RuntimeError("Миграция изменила существующие данные; обновление отменено")
                if db.execute("PRAGMA foreign_key_check").fetchall() or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Миграция нарушила целостность базы; обновление отменено")
                # Only a new empty installation receives the traditional buildings.
                if preserved[1]["buildings"][0] == 0:
                    db.executemany("INSERT INTO buildings(project_id,code,name,sort_order) VALUES (1,?,?,?)",
                                   [("A", "Корпус А", 1), ("B", "Корпус B", 2), ("C", "Корпус C", 3)])
            db.execute("INSERT INTO schema_migrations(name) VALUES (?)", (path.name,))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            if rebuild:
                db.execute("PRAGMA legacy_alter_table={}".format(int(legacy)))
                db.execute("PRAGMA foreign_keys={}".format(int(foreign_keys)))


def initialize(path=DEFAULT_DB_PATH):
    from app.catalog import seed_catalog
    db = connect(path)
    try:
        migrate(db)
        seed_catalog(db)
    except Exception:
        db.close()
        raise
    return db
