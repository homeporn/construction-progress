from pathlib import Path
import shutil
import struct
import zipfile

import pytest

from app.config import PROJECT_ROOT


def test_resource_manifest_includes_every_template_static_and_migration(tmp_path):
    from tools.windows.bundle import resource_entries, write_resource_manifest
    from app.portable.resources import validate_resources
    entries = resource_entries(PROJECT_ROOT)
    destinations = [entry[1] for entry in entries]
    assert len(destinations) == len(set(destinations))
    assert 'migrations/010_timesheets.sql' in destinations
    assert 'app/web/templates/timesheet_detail.html' in destinations
    assert 'app/web/static/css/work_order_print.css' in destinations
    assert len([p for p in destinations if p.endswith('.xlsx')]) == 2
    assert not any(p.endswith('.sqlite3') or p.startswith('data/') for p in destinations)
    resources = tmp_path / '_internal'
    resources.mkdir()
    for source, relative in entries:
        target = resources / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    manifest = resources / 'bundle-manifest.json'
    write_resource_manifest(PROJECT_ROOT, manifest)
    validate_resources(resources, require_manifest=True)
    (resources / 'app/web/templates/base.html').write_text('tampered')
    with pytest.raises(RuntimeError): validate_resources(resources, require_manifest=True)


def test_source_validation_reports_missing_template_without_starting_server(tmp_path):
    from app.portable.resources import validate_resources
    from app.portable.paths import PortableError
    with pytest.raises(PortableError, match='ресурс|шаблон'):
        validate_resources(tmp_path)


def test_no_linux_executable_can_be_labelled_windows(tmp_path):
    from tools.windows.bundle import check_windows_exe
    bad = tmp_path / 'ConstructionProgress.exe'
    bad.write_bytes(b'\x7fELF' + b'0'*256)
    with pytest.raises(ValueError, match='Windows|PE'): check_windows_exe(bad)


def test_buildkit_captures_working_copy_and_excludes_all_user_data(tmp_path):
    from tools.windows.bundle import create_buildkit
    result = create_buildkit(PROJECT_ROOT, tmp_path / 'BuildKit.zip')
    with zipfile.ZipFile(result) as archive:
        names = archive.namelist()
        prefix = 'ConstructionProgress-Windows-BuildKit/'
        assert prefix+'windows_launcher.py' in names
        assert prefix+'migrations/010_timesheets.sql' in names
        assert prefix+'packaging/windows/build.ps1' in names
        assert archive.read(prefix+'app/web/static/js/timesheets.js') == (PROJECT_ROOT/'app/web/static/js/timesheets.js').read_bytes()
        assert not any('.git/' in n or '/data/' in n or '/.venv/' in n or n.endswith('.sqlite3') for n in names)
        assert not any(n.endswith('.exe') for n in names)
        assert b'Build Kit' in archive.read(prefix+'README_WINDOWS.txt')
    with pytest.raises(FileExistsError): create_buildkit(PROJECT_ROOT, result)
