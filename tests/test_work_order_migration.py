from decimal import Decimal
from pathlib import Path
import shutil
import sqlite3

import pytest

import app.database as database
from app.calculation_engine.historical import HistoricalCalculator
from app.config import PROJECT_ROOT
from app.repositories.event_repository import EventRepository
from app.repositories.work_repository import WorkRepository


@pytest.fixture
def old_database(tmp_path, monkeypatch):
    root = tmp_path / 'stage311'
    (root / 'migrations').mkdir(parents=True)
    for file in (PROJECT_ROOT / 'migrations').glob('00[1-5]_*.sql'):
        shutil.copyfile(file, root / 'migrations' / file.name)
    with monkeypatch.context() as patch:
        patch.setattr(database, 'PROJECT_ROOT', root)
        db = database.initialize(tmp_path / 'до обновления.sqlite3')
    work, events = WorkRepository(db), EventRepository(db)
    floor = work.add_floor(1, '3', 3)
    item = work.add_work_item(1, floor, 1)
    plan = work.create_plan_version('CONSTRUCTION', created_at='2026-01-01T00:00:00Z')
    work.add_plan_line(plan, item, '1000', '0.8')
    work.approve_plan_version(plan, approved_at='2026-01-01T01:00:00Z')
    events.add_fact(item, '600', '2026-01-10', event_key='fact', recorded_at='2026-01-10T12:00:00Z')
    events.add_confirmation(item, '450', '2026-01-10', event_key='confirmation', document_ref='Акт', recorded_at='2026-01-10T13:00:00Z')
    yield db, plan, tmp_path
    db.close()


def test_migration_backup_preserves_all_ids_events_and_progress_and_is_repeatable(old_database):
    db, plan, root = old_database
    columns, before = database._fingerprints(db)
    calc = HistoricalCalculator(db)
    report = calc.calculate_as_of('2026-01-31', '2026-02-01T00:00:00Z', plan)
    database.migrate(db)
    assert database._fingerprints(db, columns)[1] == before
    assert calc.calculate_as_of('2026-01-31', '2026-02-01T00:00:00Z', plan) == report
    assert (report.total.actual_progress, report.total.confirmed_progress) == (Decimal('0.6'), Decimal('0.45'))
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
    backups = list((root / 'backups').glob('before-work-orders-*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert backup.execute("SELECT 1 FROM sqlite_master WHERE name='work_orders'").fetchone() is None
        assert backup.execute('SELECT quantity_delta FROM fact_events').fetchone()[0] == '600'
    database.migrate(db)
    assert database._fingerprints(db, columns)[1] == before
    assert len(list((root / 'backups').glob('before-work-orders-*.sqlite3'))) == 1
    assert db.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == len(list((PROJECT_ROOT / 'migrations').glob('*.sql')))


def test_failed_migration_rolls_back_schema_and_keeps_backup(old_database, tmp_path, monkeypatch):
    db, _, root = old_database
    columns, before = database._fingerprints(db)
    broken = tmp_path / 'broken'
    shutil.copytree(PROJECT_ROOT / 'migrations', broken / 'migrations')
    migration = broken / 'migrations/006_work_orders.sql'
    migration.write_text(migration.read_text() + '\nINSERT INTO nonexistent_table VALUES (1);\n')
    monkeypatch.setattr(database, 'PROJECT_ROOT', broken)
    with pytest.raises(sqlite3.OperationalError): database.migrate(db)
    assert database._fingerprints(db, columns)[1] == before
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='work_orders'").fetchone() is None
    assert db.execute("SELECT 1 FROM schema_migrations WHERE name='006_work_orders.sql'").fetchone() is None
    assert len(list((root / 'backups').glob('before-work-orders-*.sqlite3'))) == 1
    assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
