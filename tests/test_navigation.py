"""The menu changes presentation without replacing legacy business routes."""
import re

import pytest

from app.repositories.project_repository import ProjectRepository
from app.web.navigation import GROUPS, active_page
from test_web import ORIGIN, client, get, post, webapp


def main_menu(response):
    page = response.get_data(as_text=True)
    return re.search(r'<nav class="application-navigation".*?</nav>', page, re.S).group()


def test_navigation_sections_and_unique_canonical_links(client):
    menu = main_menu(get(client, '/'))
    assert set(re.findall(r'data-nav-group="([^"]+)"', menu)) == {group[0] for group in GROUPS}
    assert 'Объекты и объёмы' in menu and 'Рабочее время' in menu
    assert 'Персонал и бригады' in menu and 'Справочники и настройки' in menu
    links = re.findall(r'href="([^"]+)"', menu)
    assert len(links) == len(set(links))
    assert '/timesheets?history=1' in links and '/timesheets/new' in links
    assert 'Зарплата' not in menu and 'Производительность' in menu


def test_menu_opened_groups_and_collapsed_sidebar_persist_in_session(client):
    response = post(client, '/navigation/state', dict(opened=['production', 'personnel'], collapsed='1'))
    assert response.status_code == 200 and response.json == dict(saved=True)
    with client.session_transaction(base_url=ORIGIN) as state:
        assert state['navigation'] == dict(opened=['personnel', 'production'], collapsed=True)
    page = get(client, '/volumes').get_data(as_text=True)
    assert '<body class="nav-collapsed">' in page
    assert re.search(r'data-nav-group="production"\s+open', page)
    assert re.search(r'data-nav-group="personnel"\s+open', page)
    assert not re.search(r'data-nav-group="objects"\s+open', page)
    assert post(client, '/navigation/state', dict(collapsed='0')).status_code == 200
    assert '<body class="">' in get(client, '/').get_data(as_text=True)


@pytest.mark.parametrize('data', [dict(opened=['foreign'], collapsed='1'), dict(collapsed='yes'),
                                  dict(opened=['objects'] * 8, collapsed='0')])
def test_invalid_navigation_preferences_are_rejected(client, data):
    assert post(client, '/navigation/state', data).status_code == 400


@pytest.mark.parametrize('violation', ['csrf', 'origin', 'workspace'])
def test_navigation_state_keeps_csrf_origin_and_workspace_guards(client, violation):
    data = dict(collapsed='1')
    options = {}
    if violation == 'csrf':
        options['token'] = False
    elif violation == 'origin':
        options['origin'] = 'https://example.invalid'
    else:
        data['workspace_id'] = '999'
    assert post(client, '/navigation/state', data, **options).status_code == 403


@pytest.mark.parametrize('path,item', [
    ('/facts', 'facts'), ('/confirmations', 'confirmations'), ('/volumes', 'volumes'),
    ('/personnel/workers', 'workers'), ('/personnel/brigades', 'brigades'),
    ('/personnel/import', 'import'), ('/personnel/import-history', 'imports'),
    ('/personnel?section=contractors', 'contractors'), ('/personnel?section=specialties', 'specialties'),
    ('/analytics/volumes', 'natural'), ('/analytics/groups', 'groups'),
    ('/analytics/dynamics', 'dynamics'), ('/progress', 'summary'),
    ('/timesheets', 'timesheets'), ('/timesheets/new', 'timesheet_create'),
    ('/timesheets?history=1', 'timesheet_history')])
def test_active_navigation_item_and_breadcrumbs(client, path, item):
    response = get(client, path)
    assert response.status_code == 200
    menu = main_menu(response)
    assert re.search(r'class="nav-link active"\s+aria-current="page"\s+data-nav-item="' + item + '"', menu)
    breadcrumbs = re.search(r'<nav class="breadcrumbs".*?</nav>', response.get_data(as_text=True), re.S).group()
    assert 'Главная' in breadcrumbs and 'aria-current="page"' in breadcrumbs


@pytest.mark.parametrize('path', ['/', '/projects', '/buildings', '/catalog', '/volumes', '/facts',
    '/confirmations', '/progress', '/work-orders', '/personnel', '/personnel/workers',
    '/personnel/brigades', '/personnel/import', '/personnel/import-history', '/analytics', '/analytics/export'])
def test_legacy_urls_remain_available(client, path):
    assert get(client, path).status_code == 200


def test_legacy_combined_directory_and_distinct_settings_sections(client):
    legacy = get(client, '/personnel').get_data(as_text=True)
    assert '<h2>Подрядные организации</h2>' in legacy and '<h2>Специальности</h2>' in legacy
    contractors = get(client, '/personnel?section=contractors').get_data(as_text=True)
    specialties = get(client, '/personnel?section=specialties').get_data(as_text=True)
    assert '<h2>Подрядные организации</h2>' in contractors and '<h2>Специальности</h2>' not in contractors
    assert '<h2>Специальности</h2>' in specialties and '<h2>Подрядные организации</h2>' not in specialties
    assert get(client, '/personnel?section=foreign').status_code == 400


def test_project_switch_uses_one_global_selector_and_resets_old_filters(client, db):
    school = ProjectRepository(db).create_project('Школа', 'NAV-SCHOOL')
    page = get(client, '/volumes?building_id=1').get_data(as_text=True)
    assert page.count('aria-label="Текущий объект"') == 1
    response = post(client, '/projects/select', dict(project_id=school))
    assert response.status_code == 303 and response.location == '/'
    current = get(client, '/').get_data(as_text=True)
    assert re.search(r'<span class="topbar-project"[^>]*>Школа</span>', current)
    assert get(client, '/projects/1').status_code == 403


def test_settings_read_only_local_environment_page(client, db):
    before = db.total_changes
    response = get(client, '/settings')
    assert response.status_code == 200
    assert 'Настройки приложения' in response.get_data(as_text=True)
    assert 'Python / SQLite / Flask' in response.get_data(as_text=True)
    assert db.total_changes == before
    assert post(client, '/settings', {}).status_code == 405


@pytest.mark.parametrize('path,expected', [
    ('/personnel/employees/new', ('personnel', 'workers')),
    ('/personnel/contractors/new', ('settings', 'contractors')),
    ('/personnel/specialties/new', ('settings', 'specialties')),
    ('/work-orders/new', ('production', 'orders')),
    ('/analytics/groups/new', ('analytics', 'groups'))])
def test_nested_forms_belong_to_correct_menu_group(webapp, path, expected):
    with webapp.test_request_context(path):
        assert active_page() == expected


def test_navigation_assets_are_local_and_use_older_safari_fallback(client):
    page = get(client, '/').get_data(as_text=True)
    assert '/static/css/navigation.css' in page and '/static/js/navigation.js' in page
    script = get(client, '/static/js/navigation.js').get_data(as_text=True)
    assert 'media.addListener' in script and 'credentials: "same-origin"' in script
    assert 'https://' not in script and 'http://' not in script
