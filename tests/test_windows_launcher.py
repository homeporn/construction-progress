import json
import subprocess
import sys

from app.config import PROJECT_ROOT


def test_source_diagnostic_uses_explicit_unicode_profile_without_creating_db(tmp_path):
    target = tmp_path / 'Тест User' / 'Профиль Portable'
    result = subprocess.run([sys.executable, str(PROJECT_ROOT/'windows_launcher.py'),
                             '--data-dir', str(target), '--diagnose'],
                            cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr+result.stdout
    assert json.loads(result.stdout)['database_exists'] is False
    assert not target.exists()


def test_source_launch_without_profile_cannot_touch_working_database(tmp_path):
    result = subprocess.run([sys.executable, str(PROJECT_ROOT/'windows_launcher.py'), '--no-browser'],
                            cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert 'data-dir' in result.stdout and 'Traceback' not in result.stderr


def test_backup_command_never_creates_a_missing_database(tmp_path):
    target = tmp_path / 'profile'
    result = subprocess.run([sys.executable, str(PROJECT_ROOT/'windows_launcher.py'),
                             '--data-dir', str(target), '--backup'],
                            cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert not (target/'data/construction_progress.sqlite3').exists()
    assert 'не создана' in result.stdout
