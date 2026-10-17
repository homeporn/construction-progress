import sqlite3
from pathlib import Path

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


def migrate(db):
    """Each SQL migration, its DDL and version marker commit atomically."""
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
               "(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    for path in sorted((PROJECT_ROOT / "migrations").glob("*.sql")):
        # executescript commits an existing transaction, so never accept one.
        if db.in_transaction:
            raise RuntimeError("Run migrations outside a transaction")
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT 1 FROM schema_migrations WHERE name=?", (path.name,)).fetchone():
                db.commit()
                continue
            # Execute complete SQL statements without executescript's implicit COMMIT.
            statement = ""
            for line in path.read_text(encoding="utf-8").splitlines(True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            if statement.strip():
                raise RuntimeError("Incomplete migration: " + path.name)
            db.execute("INSERT INTO schema_migrations(name) VALUES (?)", (path.name,))
            db.commit()
        except Exception:
            db.rollback()
            raise


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
