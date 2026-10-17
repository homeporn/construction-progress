import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from app.config import PROJECT_ROOT
from app.database import initialize
from app import launch
from scripts import check_environment as diagnostics


@pytest.mark.parametrize("macos,process,arm,translated,hardware,rosetta", [
    ("10.15.7", "x86_64", "0", None, "x86_64", False),
    ("10.15.7", "x86_64", None, None, "x86_64", False),
    ("13.7.8", "x86_64", "0", None, "x86_64", False),
    ("15.6.1", "arm64", "1", "0", "arm64", False),
    ("13.7.8", "x86_64", "1", "1", "arm64", True),
])
def test_mac_architecture_detection_including_rosetta(monkeypatch, macos, process, arm, translated, hardware, rosetta):
    monkeypatch.setattr(diagnostics.sys, "platform", "darwin")
    monkeypatch.setattr(diagnostics.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(diagnostics.platform, "machine", lambda: process)
    values = {"-productVersion": macos, "hw.optional.arm64": arm, "sysctl.proc_translated": translated}
    monkeypatch.setattr(diagnostics, "_mac_command", lambda args: values[args[-1]])
    info = diagnostics.platform_info()
    assert info["hardware_arch"] == hardware and info["process_arch"] == process
    assert info["translated"] is rosetta
    assert info["macos"] == macos
    assert diagnostics._platform_errors(info) == []
    assert any("Rosetta" in warning for warning in info["warnings"]) is rosetta


@pytest.mark.parametrize("macos,arch", [("10.14.6", "x86_64"), ("", "x86_64"), ("wrong", "arm64"), ("13.7", "i386")])
def test_unsupported_or_unknown_mac_environment_is_reported(monkeypatch, macos, arch):
    monkeypatch.setattr(diagnostics.sys, "platform", "darwin")
    assert diagnostics._platform_errors(dict(macos=macos, process_arch=arch))


def test_platform_fallback_does_not_invent_hardware_identity(monkeypatch):
    monkeypatch.setattr(diagnostics.sys, "platform", "darwin")
    monkeypatch.setattr(diagnostics.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(diagnostics.platform, "mac_ver", lambda: ("13.7.8", (), ""))
    monkeypatch.setattr(diagnostics, "_mac_command", lambda args: None)
    info = diagnostics.platform_info()
    assert info["macos"] == "13.7.8"
    assert info["hardware_arch"] == "не определена"
    assert info["translated"] is None


def test_binary_dependency_import_failure_is_reported_without_traceback(monkeypatch):
    original = diagnostics.importlib.import_module
    def broken(name):
        if name == "markupsafe._speedups":
            raise ImportError("Mach-O incompatible architecture")
        return original(name)
    monkeypatch.setattr(diagnostics.importlib, "import_module", broken)
    errors = diagnostics.check(include_web=True)
    assert any("markupsafe._speedups" in error and "архитектуру" in error for error in errors)


def test_missing_dependency_and_sqlite_are_reported(monkeypatch):
    original = diagnostics.version
    def missing(name):
        if name == "Flask":
            raise diagnostics.PackageNotFoundError(name)
        return original(name)
    monkeypatch.setattr(diagnostics, "version", missing)
    monkeypatch.setattr(diagnostics, "sqlite3", None)
    errors = diagnostics.check(include_web=True)
    assert any("Flask" in error and "--prepare" in error for error in errors)
    assert any("SQLite недоступен" in error for error in errors)


def test_wrong_venv_is_not_accepted(tmp_path, monkeypatch):
    assert diagnostics.venv_errors(tmp_path)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /example\n")
    assert diagnostics.venv_errors(tmp_path)
    monkeypatch.setattr(diagnostics.sys, "prefix", str(tmp_path / ".venv"))
    assert diagnostics.venv_errors(tmp_path) == []


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def diagnostic_ports(monkeypatch):
    monkeypatch.setattr(diagnostics, "probe_local_port", lambda port: port or 54321)
    monkeypatch.setattr(diagnostics, "venv_errors", lambda root: [])


def test_diagnose_is_read_only_and_does_not_apply_pending_migration(db, capsys, diagnostic_ports):
    path = Path(db.execute("PRAGMA database_list").fetchone()["file"])
    db.execute("DELETE FROM schema_migrations WHERE name='003_web_workflow.sql'")
    before = (file_hash(path), path.stat().st_mtime_ns)
    assert diagnostics.diagnose(path) == 0
    assert before == (file_hash(path), path.stat().st_mtime_ns)
    assert db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 2
    output = capsys.readouterr().out
    assert "ожидает запуска 1" in output
    assert "Проверка" in output or "диагностика" in output
    assert "Интернет не используется" in output


def test_check_missing_database_does_not_create_its_parent(tmp_path, diagnostic_ports):
    path = tmp_path / "новый каталог с пробелами" / "данные.sqlite3"
    assert diagnostics.diagnose(path) == 0
    assert not path.parent.exists()


def test_corrupt_database_is_not_repaired_or_replaced(tmp_path, diagnostic_ports, capsys):
    path = tmp_path / "damaged.sqlite3"
    path.write_bytes(b"not a SQLite database")
    before = file_hash(path)
    assert diagnostics.diagnose(path) == 1
    assert file_hash(path) == before
    assert "Не удалось прочитать" in capsys.readouterr().out


def test_busy_port_is_a_russian_diagnostic_error(tmp_path, diagnostic_ports, monkeypatch, capsys):
    def occupied(port):
        raise OSError(errno.EADDRINUSE, "Address already in use")
    monkeypatch.setattr(diagnostics, "probe_local_port", occupied)
    assert diagnostics.diagnose(tmp_path / "missing.sqlite3", port=5100) == 1
    assert "Порт 5100 уже занят" in capsys.readouterr().out


def test_port_probe_binds_only_loopback_and_always_closes(monkeypatch):
    calls = []
    class Socket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            calls.append("closed")
        def setsockopt(self, level, option, value):
            assert (level, option, value) == (launch.socket.SOL_SOCKET, launch.socket.SO_REUSEADDR, 1)
        def bind(self, address):
            calls.append(address)
        def getsockname(self):
            return ("127.0.0.1", 54321)
    monkeypatch.setattr(launch.socket, "socket", lambda *args: Socket())
    assert launch.probe_local_port() == 54321
    assert calls == [("127.0.0.1", 0), "closed"]


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_acceptance_database_cannot_alias_working_database(tmp_path, monkeypatch, kind):
    working, target = tmp_path / "working.sqlite3", tmp_path / "acceptance.sqlite3"
    working.write_bytes(b"production must remain unchanged")
    (target.symlink_to if kind == "symlink" else target.hardlink_to)(working)
    monkeypatch.setattr(launch, "DEFAULT_DB_PATH", working)
    monkeypatch.setattr(launch, "TEST_DB_PATH", target)
    with pytest.raises(ValueError, match="ссылается на рабочую"):
        launch.select_database(test_database=True)
    assert working.read_bytes() == b"production must remain unchanged"


def test_test_db_and_explicit_db_are_mutually_exclusive(tmp_path):
    with pytest.raises(ValueError, match="нельзя использовать вместе"):
        launch.select_database(tmp_path / "custom.sqlite3", test_database=True)


@pytest.fixture
def copied_project(tmp_path):
    root = tmp_path / "Construction Progress Приёмка с пробелами"
    root.mkdir()
    for name in ("app", "scripts", "migrations", "requirements"):
        shutil.copytree(PROJECT_ROOT / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("start.command", "main.py"):
        shutil.copy2(PROJECT_ROOT / name, root / name)
    (root / ".venv").symlink_to(PROJECT_ROOT / ".venv", target_is_directory=True)
    return root


def test_real_launcher_preserves_working_data_and_repeated_test_database(copied_project, tmp_path):
    root = copied_project
    working = root / "data" / "construction_progress.sqlite3"
    db = initialize(working)
    db.execute("INSERT INTO floors(building_id,floor_code,sort_order) VALUES (1,'Рабочий этаж',1)")
    db.close()
    before = file_hash(working)
    for _ in range(2):
        result = subprocess.run([str(root / "start.command"), "--test-db", "--cli"], cwd=tmp_path,
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "РЕЖИМ ПРИЁМКИ" in result.stdout
        assert "Рабочий этаж" not in result.stdout
        assert file_hash(working) == before
    test_db = root / "data" / "acceptance" / "acceptance.sqlite3"
    db = initialize(test_db)
    try:
        assert [db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                for table in ("buildings", "work_catalog", "floors", "plan_lines")] == [3, 15, 0, 0]
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


@pytest.fixture
def launcher_spy(tmp_path):
    """Record subprocess arguments without installing packages or opening a port."""
    root = tmp_path / "проект с пробелами"
    root.mkdir()
    shutil.copy2(PROJECT_ROOT / "start.command", root / "start.command")
    log = tmp_path / "arguments.jsonl"
    spy = tmp_path / "Python с пробелами"
    code = "import json,sys; open({!r},'a').write(json.dumps(sys.argv[1:],ensure_ascii=False)+'\\n')".format(str(log))
    spy.write_text("#!/bin/bash\nexec " + shlex.quote(sys.executable) + " -c " + shlex.quote(code) + ' "$@"\n')
    spy.chmod(0o700)
    def run(*arguments, venv=False):
        if venv:
            (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
            (root / ".venv" / "bin" / "python").symlink_to(spy)
        env = dict(os.environ, CP_PYTHON=str(spy))
        result = subprocess.run([str(root / "start.command"), *arguments], cwd=tmp_path, env=env,
                                capture_output=True, text=True, timeout=10)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls, root
    return run


def test_check_mode_never_creates_venv_or_installs_packages(launcher_spy):
    result, calls, root = launcher_spy("--test-db", "--check", "--port", "5100")
    assert result.returncode == 0
    assert calls == [["-B", "-m", "scripts.check_environment", "--diagnose", "--test-db", "--port", "5100"]]
    assert not (root / ".venv").exists() and not (root / "data").exists()


def test_check_prepare_conflict_is_rejected_before_any_python_or_network(launcher_spy):
    result, calls, root = launcher_spy("--check", "--prepare")
    assert result.returncode != 0 and not calls
    assert "нельзя совмещать" in result.stderr
    assert not (root / ".venv").exists()


def test_default_launch_without_arguments_and_without_automatic_pip(launcher_spy):
    result, calls, _ = launcher_spy(venv=True)
    assert result.returncode == 0
    assert calls[-1] == ["main.py"]
    assert not any("pip" in call for call in calls)


def test_launcher_keeps_unicode_path_as_one_argument(launcher_spy):
    value = "данные с пробелами/моя база.sqlite3"
    result, calls, _ = launcher_spy("--db", value, "--no-browser", venv=True)
    assert result.returncode == 0
    assert calls[-1] == ["main.py", "--db", value, "--no-browser"]


def test_only_explicit_prepare_can_invoke_package_installation(launcher_spy):
    result, calls, _ = launcher_spy("--test-db", "--prepare", "--cli", venv=True)
    assert result.returncode == 0
    installations = [call for call in calls if "pip" in call]
    assert len(installations) == 1 and "--only-binary=:all:" in installations[0]
    assert calls[-1] == ["main.py", "--test-db", "--cli"]


def test_unprepared_launch_does_not_silently_create_environment(launcher_spy):
    result, calls, root = launcher_spy()
    assert result.returncode != 0
    assert "--prepare" in result.stderr
    assert not (root / ".venv").exists()
    assert not any("pip" in call or "venv" in call for call in calls)


@pytest.mark.parametrize("value", ["-1", "65536", "not-a-port"])
def test_invalid_port_fails_before_any_database_is_created(tmp_path, value):
    db = tmp_path / "must-not-exist.sqlite3"
    result = subprocess.run([sys.executable, "main.py", "--port", value, "--db", str(db)],
                            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and "Ошибка параметров" in result.stderr
    assert "Traceback" not in result.stderr and not db.exists()


def test_bind_failure_is_localized_without_werkzeug_fatal_output(tmp_path, monkeypatch, capsys):
    from app.web import server
    def fail(*args, **kwargs):
        print("Address already in use", file=sys.stderr)
        raise SystemExit(1)
    monkeypatch.setattr(server, "make_server", fail)
    with pytest.raises(OSError, match="Не удалось открыть 127.0.0.1:5100"):
        server.LocalServer(tmp_path / "test.sqlite3", port=5100)
    assert "Address already in use" not in capsys.readouterr().err
