"""Portable storage tests use private temporary profiles, never the working DB."""
import hashlib
from pathlib import Path
import shutil
import sqlite3

import pytest

from app.config import PROJECT_ROOT


def portable(tmp_path):
    from app.portable.paths import PortablePaths
    return PortablePaths.discover(executable=tmp_path / 'Тест User' / 'Construction Progress' / 'ConstructionProgress.exe',
                                  resources=PROJECT_ROOT, frozen=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_frozen_paths_are_beside_executable_and_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = portable(tmp_path)
    assert p.database == tmp_path / 'Тест User' / 'Construction Progress' / 'data' / 'construction_progress.sqlite3'
    assert p.backups.parent == p.profile and p.logs.parent == p.profile
    assert p.resources == PROJECT_ROOT


def test_external_profile_is_explicit_and_resources_are_never_writable_storage(tmp_path):
    from app.portable.paths import PortablePaths, PortableError
    p = PortablePaths.discover(executable=tmp_path / 'program' / 'app.exe', resources=PROJECT_ROOT,
                               data_dir=tmp_path / 'Личный профиль 01', frozen=True)
    assert p.profile == tmp_path / 'Личный профиль 01'
    with pytest.raises(PortableError):
        PortablePaths.discover(executable=tmp_path / 'app.exe', resources=PROJECT_ROOT,
                               data_dir=PROJECT_ROOT / '_internal' / 'data', frozen=True)


def test_source_launcher_requires_an_explicit_test_profile():
    from app.portable.paths import PortablePaths, PortableError
    with pytest.raises(PortableError, match='data-dir'):
        PortablePaths.discover(frozen=False)


def test_first_run_and_repeat_have_no_demo_data(tmp_path):
    from app.portable.storage import prepare_database
    p = portable(tmp_path)
    p.ensure_writable()
    prepare_database(p)
    db = sqlite3.connect(p.database)
    assert db.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == len(list((PROJECT_ROOT/'migrations').glob('*.sql')))
    assert db.execute('SELECT count(*) FROM employees').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM fact_events').fetchone()[0] == 0
    db.execute("INSERT INTO floors(building_id,floor_code,sort_order) VALUES (1,'Сохранить',1)")
    db.commit(); db.close()
    before = digest(p.database)
    prepare_database(p)
    assert digest(p.database) == before


def test_write_failure_is_localized_and_does_not_fall_back_elsewhere(tmp_path, monkeypatch):
    from app.portable.paths import PortableError
    p = portable(tmp_path)
    def denied(*a, **kw):
        raise PermissionError('denied')
    monkeypatch.setattr('app.portable.paths.tempfile.NamedTemporaryFile', denied)
    with pytest.raises(PortableError, match='запис'):
        p.ensure_writable()
    assert not p.database.exists()


def old_database(p, tmp_path, monkeypatch):
    import app.database as database
    old = tmp_path / 'stage41'
    (old / 'migrations').mkdir(parents=True)
    for sql in (PROJECT_ROOT / 'migrations').glob('00[1-9]_*.sql'):
        shutil.copyfile(sql, old / 'migrations' / sql.name)
    p.ensure_writable()
    with monkeypatch.context() as patch:
        patch.setattr(database, 'PROJECT_ROOT', old)
        db = database.initialize(p.database)
    db.execute("INSERT INTO floors(building_id,floor_code,sort_order) VALUES (1,'История',1)")
    db.close()


def test_upgrade_has_verified_backup_and_preserves_existing_ids(tmp_path, monkeypatch):
    from app.portable.storage import prepare_database
    p = portable(tmp_path)
    old_database(p, tmp_path, monkeypatch)
    before = digest(p.database)
    prepare_database(p)
    copies = list(p.backups.glob('*.sqlite3'))
    assert len(copies) == 1
    copy = sqlite3.connect(copies[0])
    assert copy.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not copy.execute('PRAGMA foreign_key_check').fetchall()
    assert copy.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == 9
    assert copy.execute('SELECT id,floor_code FROM floors').fetchall() == [(1,'История')]
    copy.close()
    db = sqlite3.connect(p.database)
    assert db.execute('SELECT id,floor_code FROM floors').fetchall() == [(1,'История')]
    assert db.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == len(list((PROJECT_ROOT/'migrations').glob('*.sql')))
    db.close()
    assert digest(p.database) != before
    prepare_database(p)
    assert len(list(p.backups.glob('*.sqlite3'))) == 1


def test_failed_migration_preserves_original_and_backup(tmp_path, monkeypatch):
    from app.portable.storage import prepare_database
    from app.portable.paths import PortableError
    import app.database as database
    p = portable(tmp_path)
    old_database(p, tmp_path, monkeypatch)
    broken = tmp_path / 'broken'
    shutil.copytree(PROJECT_ROOT / 'migrations', broken / 'migrations')
    migration = broken / 'migrations' / '010_timesheets.sql'
    migration.write_text(migration.read_text() + '\nINSERT INTO deliberately_missing VALUES (1);\n')
    monkeypatch.setattr(database, 'PROJECT_ROOT', broken)
    before = digest(p.database)
    with pytest.raises(PortableError, match='[Кк]опи'):
        prepare_database(p)
    assert digest(p.database) == before
    assert len(list(p.backups.glob('*.sqlite3'))) == 1
    db = sqlite3.connect(p.database)
    assert db.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == 9
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='timesheets'").fetchone()
    db.close()


def test_corrupt_and_newer_database_are_never_replaced(tmp_path):
    from app.portable.storage import prepare_database
    from app.portable.paths import PortableError
    p = portable(tmp_path); p.ensure_writable()
    p.database.write_bytes(b'not sqlite')
    before = digest(p.database)
    with pytest.raises(PortableError): prepare_database(p)
    assert digest(p.database) == before
    p.database.unlink()
    prepare_database(p)
    db = sqlite3.connect(p.database)
    db.execute("INSERT INTO schema_migrations(name) VALUES ('999_future.sql')")
    db.commit(); db.close()
    before = digest(p.database)
    with pytest.raises(PortableError, match='[Нн]ов|схем'): prepare_database(p)
    assert digest(p.database) == before


def test_diagnostic_is_read_only_and_has_no_personnel(tmp_path):
    from app.portable.storage import diagnostic_report, prepare_database
    p = portable(tmp_path)
    report = diagnostic_report(p)
    assert report['database_exists'] is False and not p.profile.exists()
    p.ensure_writable(); prepare_database(p)
    before = digest(p.database)
    report = diagnostic_report(p)
    assert report['integrity_check'] == 'ok' and report['foreign_key_errors'] == 0
    assert report['schema'][-1] == sorted(p.name for p in (PROJECT_ROOT/'migrations').glob('*.sql'))[-1]
    assert digest(p.database) == before
    assert 'employees' not in report and 'full_name' not in report


def test_windows_smoke_fixture_preserves_shared_calculation_and_fact_without_order(tmp_path):
    from tools.windows.smoke import seed_fixture
    (tmp_path/'.windows-smoke-fixture').touch()
    assert seed_fixture(tmp_path) == dict(order=1,timesheet=1,actual='0.6',confirmed='0.45',hours='7.5')
