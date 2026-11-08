"""Checked Backup API and preflight upgrades using the existing migrations."""
from datetime import datetime, timezone
from contextlib import closing
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import platform
import sqlite3
import tempfile
from uuid import uuid4

import app.database as database
from .paths import PortableError

VERSION = 'W1.0'


def read_only(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def check_integrity(db):
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchall():
        raise PortableError('Проверка целостности базы не пройдена. Запуск отменён; восстановите проверенную резервную копию.')


def schema_names(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone():
        raise PortableError('Неизвестная структура базы: отсутствует история миграций. Файл не изменён.')
    return [row[0] for row in db.execute('SELECT name FROM schema_migrations ORDER BY name')]


def checked_backup(source, folder, label='manual'):
    """Never copy an open SQLite file; never overwrite an older backup."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target = folder / '{}-{}-{}.sqlite3'.format(label, stamp, uuid4().hex)
    descriptor = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    src = dst = None
    try:
        src = read_only(source)
        check_integrity(src)
        dst = sqlite3.connect(str(target))
        src.backup(dst)
        check_integrity(dst)
        return target
    except Exception:
        if dst is not None:
            dst.close(); dst = None
        target.unlink(missing_ok=True)
        raise
    finally:
        if dst is not None: dst.close()
        if src is not None: src.close()


def prepare_database(paths):
    """Must run under the profile OS lock, before constructing the WSGI app.

    Existing SQL migrations are transactional individually. Preflighting the
    complete pending chain on a separate Backup API copy catches SQL/DDL faults
    before the live file is opened for writes. An I/O failure in the real pass
    still stops startup and retains the verified pre-upgrade backup.
    """
    backup = None
    try:
        if not paths.database.exists():
            db = database.initialize(paths.database)
            db.close()
            with closing(read_only(paths.database)) as check:
                check_integrity(check)
            return
        with closing(read_only(paths.database)) as db:
            check_integrity(db)
            applied = schema_names(db)
        available = sorted(p.name for p in (database.PROJECT_ROOT / 'migrations').glob('*.sql'))
        if not available or applied != available[:len(applied)]:
            raise PortableError('Версия схемы неизвестна или новее приложения. Используйте совместимую версию; база не изменена.')
        if applied == available:
            return
        backup = checked_backup(paths.database, paths.backups, 'before-upgrade')
        with tempfile.TemporaryDirectory(prefix='.upgrade-check-', dir=paths.data) as staging:
            probe = checked_backup(backup, Path(staging), 'probe')
            db = database.initialize(probe)
            db.close()
            with closing(read_only(probe)) as db:
                check_integrity(db)
                if schema_names(db) != available:
                    raise PortableError('Пробное обновление не применило все миграции.')
        db = database.initialize(paths.database)
        db.close()
        with closing(read_only(paths.database)) as db:
            check_integrity(db)
    except PortableError:
        if backup:
            raise PortableError('Обновление базы остановлено. Проверенная резервная копия: {}. Сервер не запущен.'.format(backup)) from None
        raise
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        message = ('Обновление базы остановлено. Проверенная резервная копия: {}. Сервер не запущен.'.format(backup)
                   if backup else 'Не удалось открыть, создать или проверить базу {}. Проверьте права записи и целостность файла. Сервер не запущен.'.format(paths.database))
        raise PortableError(message) from exc


def diagnostic_report(paths):
    """Read-only: no migrations, no seeding, no employee records, no secrets."""
    report = dict(application_version=VERSION, python=platform.python_version(),
                  sqlite=sqlite3.sqlite_version, platform=platform.platform(),
                  architecture=platform.machine(), database=str(paths.database),
                  resources=str(paths.resources), database_exists=paths.database.is_file(),
                  directories={name: dict(path=str(getattr(paths, name)), exists=getattr(paths, name).is_dir(),
                                         writable=os.access(getattr(paths, name), os.W_OK))
                               for name in ('profile', 'data', 'backups', 'exports', 'logs')})
    for package in ('Flask', 'waitress', 'pyinstaller'):
        try: report[package] = version(package)
        except PackageNotFoundError: report[package] = 'not installed'
    if paths.database.is_file():
        try:
            with closing(read_only(paths.database)) as db:
                check_integrity(db)
                report.update(integrity_check='ok', foreign_key_errors=0, schema=schema_names(db))
        except (sqlite3.Error, PortableError):
            report.update(integrity_check='failed', error='База недоступна, повреждена или её схема неизвестна.')
    return report
