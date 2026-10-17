"""Offline checks. This script never installs packages or contacts a network."""
import argparse
import errno
import importlib
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import platform
import subprocess
import sys

try:
    import sqlite3
except ImportError:
    sqlite3 = None

from app.config import DEFAULT_DB_PATH, MIN_SQLITE, PROJECT_ROOT
from app.launch import LaunchArgumentParser, port_number, probe_local_port, select_database

TEST_PACKAGES = {"pytest": "8.4.2", "iniconfig": "2.1.0", "packaging": "25.0",
                 "pluggy": "1.6.0", "Pygments": "2.19.2"}
WEB_PACKAGES = {"Flask": "3.1.3", "Werkzeug": "3.1.9", "Jinja2": "3.1.6",
                "MarkupSafe": "3.0.4", "itsdangerous": "2.2.0", "click": "8.1.8", "blinker": "1.9.0"}
WEB_IMPORTS = ("flask", "werkzeug", "jinja2", "markupsafe._speedups", "itsdangerous", "click", "blinker")


def _mac_command(arguments):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def platform_info():
    process = {"amd64": "x86_64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
    info = dict(system=platform.system(), macos="", process_arch=process, hardware_arch=process,
                translated=None, warnings=[])
    if sys.platform == "darwin":
        info["macos"] = _mac_command(["/usr/bin/sw_vers", "-productVersion"]) or platform.mac_ver()[0]
        arm_capable = _mac_command(["/usr/sbin/sysctl", "-n", "hw.optional.arm64"])
        translated = _mac_command(["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"])
        if translated == "1" or arm_capable == "1" or process == "arm64":
            info["hardware_arch"] = "arm64"
            info["translated"] = process == "x86_64"
        elif arm_capable == "0" or translated == "0":
            info["hardware_arch"] = process
            info["translated"] = False
        elif process == "x86_64" and info["macos"].split(".")[:2] == ["10", "15"]:
            # Catalina is Intel-only and predates Rosetta 2; its kernel may not
            # expose the Apple Silicon sysctl keys introduced in later macOS.
            info["hardware_arch"] = "x86_64"
            info["translated"] = False
        else:
            info["hardware_arch"] = "не определена"
            info["warnings"].append("Не удалось определить аппаратную архитектуру через sysctl; проверьте её на Mac вручную.")
        if info["translated"]:
            info["warnings"].append("Python x86_64 работает через Rosetta на Apple Silicon. Для нативной приёмки используйте arm64 Python и новое окружение.")
    else:
        info["warnings"].append("Текущая система — {}. Это не испытание совместимости с macOS.".format(info["system"]))
    return info


def _platform_errors(info):
    errors = []
    if sys.platform == "darwin":
        try:
            os_version = tuple(int(x) for x in info["macos"].split(".")[:2])
        except ValueError:
            os_version = ()
        if len(os_version) < 2:
            errors.append("Не удалось определить версию macOS. Проверьте /usr/bin/sw_vers -productVersion.")
        elif os_version < (10, 15):
            errors.append("Требуется macOS 10.15 или новее.")
        if info["process_arch"] not in ("x86_64", "arm64"):
            errors.append("Неподдерживаемая архитектура Python: {}. Требуется x86_64 или arm64.".format(info["process_arch"]))
    return errors


def venv_errors(project_root=PROJECT_ROOT):
    expected = (Path(project_root) / ".venv").resolve()
    if not (Path(project_root) / ".venv" / "pyvenv.cfg").is_file():
        return ["Окружение .venv отсутствует или повреждено. Первичная подготовка: ./start.command --prepare"]
    if sys.prefix == sys.base_prefix or Path(sys.prefix).resolve() != expected:
        return ["Выбранный Python не принадлежит .venv этого проекта. Не копируйте окружение с другого компьютера; пересоздайте его по инструкции."]
    return []


def check(include_tests=False, include_web=False, *, info=None):
    errors = []
    if platform.python_implementation() != "CPython" or sys.version_info[:2] != (3, 13):
        errors.append("Требуется CPython 3.13.x; рекомендуется Python.org 3.13.12")
    if sqlite3 is None:
        errors.append("Модуль SQLite недоступен. Установите официальный CPython с Python.org и пересоздайте .venv.")
    elif sqlite3.sqlite_version_info < MIN_SQLITE:
        errors.append("Требуется SQLite >= 3.24.0")
    errors.extend(_platform_errors(info or platform_info()))
    if include_tests or include_web:
        packages = dict(TEST_PACKAGES) if include_tests else {}
        if include_web:
            packages.update(WEB_PACKAGES)
        if sys.platform == "win32":
            packages["colorama"] = "0.4.6"
        for name, expected in packages.items():
            try:
                actual = version(name)
            except PackageNotFoundError:
                actual = "missing"
            if actual != expected:
                errors.append("Требуется {}=={} (найдено {}). Подготовка: ./start.command --prepare".format(name, expected, actual))
    if include_web:
        for module in WEB_IMPORTS:
            try:
                importlib.import_module(module)
            except Exception as exc:
                errors.append("Не удалось загрузить {} ({}: {}). Проверьте архитектуру Python и пересоздайте .venv.".format(
                    module, type(exc).__name__, str(exc)[:300]))
    return errors


def database_diagnostics(path):
    """Only read the selected file: no seed, migrations, mkdir or SQLite writes."""
    path = Path(path)
    errors = []
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        errors.append("Нет доступа для записи в каталог базы: " + str(parent))
    if not path.exists():
        print("База: ещё не создана; будет создана только при запуске приложения.")
        return errors
    if sqlite3 is None:
        return errors + ["Проверка файла базы невозможна: модуль SQLite недоступен."]
    if not path.is_file():
        return errors + ["Путь базы должен указывать на файл, а не каталог."]
    if not os.access(path, os.W_OK):
        errors.append("Файл базы недоступен для записи приложением: " + str(path))
    db = None
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        db.execute("PRAGMA query_only=ON")
        results = [row[0] for row in db.execute("PRAGMA quick_check")]
        print("SQLite quick_check: " + "; ".join(results))
        if results != ["ok"]:
            errors.append("Проверка целостности SQLite обнаружила ошибки. Сохраните исходный файл и используйте резервную копию.")
        foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
        print("Нарушений внешних ключей: " + str(len(foreign_keys)))
        if foreign_keys:
            errors.append("В базе нарушены связи. Не продолжайте приёмку на этой базе до проверки резервной копии.")
        for table, label in (("buildings", "Корпусов"), ("work_catalog", "Кодов работ")):
            print(label + ": " + str(db.execute("SELECT count(*) FROM " + table).fetchone()[0]))
            if db.execute("SELECT code FROM " + table + " GROUP BY code HAVING count(*)>1").fetchone():
                errors.append("Обнаружены дубли кодов в " + table)
        applied = {row[0] for row in db.execute("SELECT name FROM schema_migrations")}
        available = {path.name for path in (PROJECT_ROOT / "migrations").glob("*.sql")}
        if applied - available:
            errors.append("База содержит неизвестные миграции. Используйте соответствующую версию исходников.")
        print("Миграции: применено {}, ожидает запуска {}. В режиме --check миграции не выполняются.".format(len(applied), len(available - applied)))
    except (sqlite3.Error, OSError) as exc:
        errors.append("Не удалось прочитать SQLite-базу: {}. Проверьте путь, закройте другой экземпляр и сохраните резервную копию.".format(exc))
    finally:
        if db is not None:
            db.close()
    return errors


def diagnose(database=DEFAULT_DB_PATH, *, port=0, test_database=False, project_root=PROJECT_ROOT):
    info = platform_info()
    print("Construction Progress — диагностика без изменения данных", flush=True)
    print("Каталог проекта: " + str(Path(project_root).resolve()))
    print("Система: " + ("macOS" if sys.platform == "darwin" else info["system"]) + (" " + info["macos"] if info["macos"] else ""))
    print("Архитектура оборудования: " + info["hardware_arch"])
    print("Архитектура Python: " + info["process_arch"])
    if sys.platform == "darwin":
        print("Rosetta: " + ("да" if info["translated"] else "нет" if info["translated"] is False else "не определено"))
    print("Python: {} ({})".format(platform.python_version(), platform.python_implementation()))
    print("Интерпретатор: " + sys.executable)
    print("Окружение: " + sys.prefix)
    print("SQLite интерпретатора: " + (sqlite3.sqlite_version if sqlite3 else "недоступна"))
    print("Режим базы: " + ("ПРИЁМКА, отдельные данные" if test_database else "рабочая база, только чтение при диагностике"))
    print("Путь к базе: " + str(database))
    packages = dict(WEB_PACKAGES, **TEST_PACKAGES)
    for name, expected in packages.items():
        try:
            actual = version(name)
        except PackageNotFoundError:
            actual = "не установлено"
        print("{}: {} (ожидается {})".format(name, actual, expected))
    errors = check(include_tests=True, include_web=True, info=info) + venv_errors(project_root)
    errors.extend(database_diagnostics(database))
    try:
        chosen = probe_local_port(port)
        print("Локальный порт: 127.0.0.1:{} доступен для привязки; проверочный сокет закрыт.".format(chosen))
        if port == 0:
            print("При запуске ОС назначит свободный порт заново; сервер привязывается атомарно.")
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            errors.append("Порт {} уже занят. Выберите другой --port или используйте --port 0.".format(port))
        else:
            errors.append("Не удалось проверить локальный порт ({}). Разрешите локальные сокеты и повторите проверку на Mac.".format(exc))
    for warning in info["warnings"]:
        print("ПРЕДУПРЕЖДЕНИЕ: " + warning)
    for error in errors:
        print("ОШИБКА: " + error)
    print("Интернет не используется. Окружение, миграции и данные в этом режиме не изменяются.")
    print("Результат диагностики: " + ("есть ошибки" if errors else "OK"))
    return 1 if errors else 0


def main():
    parser = LaunchArgumentParser(description="Проверка окружения без установки зависимостей")
    parser.add_argument("--tests", action="store_true")
    parser.add_argument("--web", action="store_true")
    parser.add_argument("--venv", action="store_true", help="Проверить принадлежность .venv проекту")
    parser.add_argument("--diagnose", action="store_true", help="Полная диагностика без изменения данных")
    parser.add_argument("--db", help="Путь к SQLite-базе для диагностики")
    parser.add_argument("--test-db", action="store_true", help="Диагностика отдельной базы приёмки")
    parser.add_argument("--port", type=port_number, default=0)
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.diagnose:
        try:
            database = select_database(args.db, test_database=args.test_db)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        raise SystemExit(diagnose(database, port=args.port, test_database=args.test_db))
    errors = check(args.tests, args.web)
    if args.venv:
        errors.extend(venv_errors())
    print("Python:", platform.python_version(), platform.system(), platform.machine())
    print("SQLite:", sqlite3.sqlite_version if sqlite3 else "недоступна")
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        sys.exit(1)
    print("Проверка зависимостей без сети: OK")


if __name__ == "__main__":
    main()
