"""Brand changes must never change the user's database or local security identity."""
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zipfile import ZipFile

import pytest

from app.config import DEFAULT_DB_PATH, PROJECT_ROOT
from app.web import create_app

NAME='ПЛАНКТОН. Учёт рабочего времени'
FAMILY='ПЛАНКТОН'
ORIGIN='http://127.0.0.1:5100'


@pytest.fixture
def branded_client(db):
    app=create_app(dict(DATABASE=db.execute('PRAGMA database_list').fetchone()[2],
        TESTING=True,LOCAL_ORIGIN=ORIGIN,SECRET_KEY='synthetic-brand-test'))
    return app,app.test_client()


def title(response):
    return re.search(r'<title>(.*?)</title>',response.get_data(as_text=True),re.S).group(1).strip()


def test_home_brand_is_official_and_title_not_duplicated(branded_client):
    _,client=branded_client
    response=client.get('/',base_url=ORIGIN)
    assert response.status_code==200
    assert title(response)==NAME
    html=response.get_data(as_text=True)
    assert FAMILY in html and 'УЧЁТ РАБОЧЕГО ВРЕМЕНИ' in html
    assert 'Construction Progress' not in html and 'CONSTRUCTION PROGRESS' not in html
    assert '>CP<' not in html and 'PLANKTON Time' not in html
    assert 'aria-label="'+NAME+'"' in html


@pytest.mark.parametrize('path',[
    '/projects','/projects/1','/buildings','/volumes','/catalog','/facts','/confirmations',
    '/work-orders','/work-orders/control','/personnel','/personnel/workers','/personnel/brigades',
    '/personnel/import','/personnel/import-history','/timesheets','/labor','/payroll',
    '/payroll/terms','/payroll/rates','/payroll/periods','/analytics','/settings',
])
def test_sections_keep_routes_and_use_short_brand_title(branded_client,path):
    _,client=branded_client
    response=client.get(path,base_url=ORIGIN,follow_redirects=True)
    assert response.status_code==200,path
    assert title(response).endswith(' | '+FAMILY)
    assert 'Construction Progress' not in response.get_data(as_text=True)
    assert FAMILY+' | '+FAMILY not in title(response)


def test_brand_configuration_is_presentation_metadata_only():
    from app import branding
    assert (branding.PRODUCT_FAMILY_NAME,branding.PRODUCT_NAME,branding.PRODUCT_SUBTITLE)==(FAMILY,NAME,'Учёт рабочего времени')
    assert (branding.PRODUCT_ID,branding.PRODUCT_SLUG)==('plankton_time','PlanktonTime')
    assert branding.PRODUCT_FAMILY_LATIN=='PLANKTON'
    assert DEFAULT_DB_PATH==PROJECT_ROOT/'data/construction_progress.sqlite3'


def test_project_named_like_brand_does_not_duplicate_page_title(db,branded_client):
    from app.repositories.project_repository import ProjectRepository
    ProjectRepository(db).update_project(1,FAMILY,'BRAND')
    _,client=branded_client
    assert title(client.get('/projects/1',base_url=ORIGIN))==NAME


def test_favicon_keeps_url_and_uses_neutral_geometry(branded_client):
    _,client=branded_client
    response=client.get('/static/favicon.svg',base_url=ORIGIN)
    assert response.status_code==200
    assert b'<svg' in response.data and b'CP' not in response.data
    assert b'<path' in response.data
    assert not any(token in response.data for token in (b'<image',b'http://fonts',b'https://fonts'))


def test_shutdown_and_error_titles_keep_brand_and_csrf(branded_client):
    app,client=branded_client
    client.get('/',base_url=ORIGIN)
    assert app.config['SESSION_COOKIE_NAME']=='construction_progress'
    assert app.config['SESSION_COOKIE_HTTPONLY'] is True
    assert client.post('/shutdown',base_url=ORIGIN,headers={'Origin':ORIGIN}).status_code==403
    with client.session_transaction(base_url=ORIGIN) as state:
        values=dict(csrf_token=state['csrf_token'],workspace_id=str(state['project_id']))
    assert client.post('/shutdown',base_url=ORIGIN,data=values,headers={'Origin':'https://example.invalid'}).status_code==403
    assert client.get('/',base_url='http://evil.invalid').status_code==403
    stopped=client.post('/shutdown',base_url=ORIGIN,data=values,headers={'Origin':ORIGIN})
    assert stopped.status_code==200 and title(stopped)=='Работа завершена | '+FAMILY
    missing=client.get('/missing-brand-page',base_url=ORIGIN)
    assert missing.status_code==404 and title(missing).endswith(' | '+FAMILY)


@pytest.mark.parametrize('path',[
    '/analytics/export.xlsx','/personnel/import/template.xlsx','/timesheets/template.xlsx',
])
def test_new_download_labels_keep_valid_workbook_structure(branded_client,path):
    _,client=branded_client
    response=client.get(path,base_url=ORIGIN)
    assert response.status_code==200
    assert 'PlanktonTime' in response.headers['Content-Disposition']
    assert 'Construction' not in response.headers['Content-Disposition']
    with ZipFile(BytesIO(response.data)) as book:
        assert 'xl/workbook.xml' in book.namelist()
        assert b'<f>' not in b''.join(book.read(n) for n in book.namelist() if n.startswith('xl/worksheets/'))


def test_existing_portable_database_is_reused_under_new_executable_name(tmp_path,monkeypatch):
    from app.portable.paths import PortablePaths
    from app.portable.storage import prepare_database,read_only
    folder=tmp_path/'Тест User'/'Construction Progress'  # Existing installation stays named as it was.
    old=PortablePaths.discover(executable=folder/'ConstructionProgress.exe',resources=PROJECT_ROOT,frozen=True)
    old.ensure_writable();prepare_database(old)
    from app.database import connect
    db=connect(old.database)
    db.execute("INSERT INTO floors(building_id,floor_code,sort_order) VALUES (1,'Сохранённый этаж',99)")
    db.close()
    digest=hashlib.sha256(old.database.read_bytes()).hexdigest()
    monkeypatch.chdir(tmp_path)
    new=PortablePaths.discover(executable=folder/'PlanktonTime.exe',resources=PROJECT_ROOT,frozen=True)
    assert new.profile==old.profile and new.database==old.database
    prepare_database(new)
    assert hashlib.sha256(new.database.read_bytes()).hexdigest()==digest
    with read_only(new.database) as db:
        assert db.execute("SELECT count(*) FROM floors WHERE floor_code='Сохранённый этаж'").fetchone()[0]==1
    assert not list(folder.rglob('plankton_time.sqlite3'))


@pytest.mark.skipif(sys.platform=='win32',reason='macOS Bash start.command; covered by Linux/macOS regression')
def test_mac_entry_shows_brand_without_creating_data(tmp_path):
    env=dict(os.environ,CP_PYTHON=sys.executable)
    result=subprocess.run(['bash',str(PROJECT_ROOT/'start.command'),'--help'],cwd=tmp_path,env=env,text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert NAME in result.stdout
    assert 'data/construction_progress.sqlite3' in result.stdout
    assert not list(tmp_path.rglob('*.sqlite3'))


def test_portable_diagnostics_show_brand_without_creating_data(tmp_path):
    target=tmp_path/'Профиль ПЛАНКТОН'
    diagnostic=subprocess.run([sys.executable,str(PROJECT_ROOT/'windows_launcher.py'),'--data-dir',str(target),'--diagnose'],cwd=tmp_path,text=True,encoding='utf-8',capture_output=True,timeout=20)
    assert diagnostic.returncode==0,diagnostic.stderr
    data=json.loads(diagnostic.stdout)
    assert data['product_name']==NAME and data['product_id']=='plankton_time'
    assert data['database'].endswith('construction_progress.sqlite3')
    assert not target.exists()


def test_future_build_kit_uses_new_slug_and_contains_brand_module(tmp_path):
    from tools.windows.bundle import create_buildkit
    archive=create_buildkit(PROJECT_ROOT,tmp_path/'PlanktonTime-BuildKit.zip')
    with ZipFile(archive) as book:
        prefix='PlanktonTime-Windows-BuildKit/'
        assert prefix+'app/branding.py' in book.namelist()
        assert prefix+'packaging/windows/ConstructionProgress.spec' in book.namelist()
        assert NAME in book.read(prefix+'README_WINDOWS.txt').decode('utf-8')
        assert not any(n.endswith(('.exe','.sqlite3','.db')) for n in book.namelist())


def test_future_windows_output_names_agree_with_brand_configuration():
    from app.branding import PRODUCT_SLUG
    folder=PROJECT_ROOT/'packaging/windows'
    spec=(folder/'ConstructionProgress.spec').read_text(encoding='utf-8')
    build=(folder/'build.ps1').read_text(encoding='utf-8')
    assert 'name=PRODUCT_SLUG' in spec and "contents_directory='_internal'" in spec
    assert 'from app.branding import PRODUCT_SLUG' in spec and 'from app.branding import PRODUCT_SLUG' in build
    assert 'ConstructionProgress.spec' in build and 'CP_WINDOWS_RESOURCE_MANIFEST' in build
    for filename in ('version_info.txt','BUILD_WINDOWS.cmd','BUILD_KIT_README.txt','PORTABLE_README.txt'):
        assert PRODUCT_SLUG in (folder/filename).read_text(encoding='utf-8')
    info=(folder/'version_info.txt').read_text(encoding='utf-8')
    assert "'OriginalFilename', 'PlanktonTime.exe'" in info and "'ProductName', '"+NAME+"'" in info
