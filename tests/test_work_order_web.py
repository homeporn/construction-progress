import re

import pytest
from werkzeug.datastructures import MultiDict

from app.repositories.project_repository import ProjectRepository
from app.web import create_app
from test_work_orders import setup_orders

ORIGIN = 'http://127.0.0.1:5100'


@pytest.fixture
def client(db):
    return create_app(dict(DATABASE=db.execute('PRAGMA database_list').fetchone()['file'],
                           TESTING=True, LOCAL_ORIGIN=ORIGIN, SECRET_KEY='orders-test')).test_client()


def get(client, path):
    return client.get(path, base_url=ORIGIN)


def post(client, path, values):
    get(client, '/')
    with client.session_transaction(base_url=ORIGIN) as state:
        data = MultiDict(values)
        data.setdefault('csrf_token', state['csrf_token'])
        data.setdefault('workspace_id', str(state['project_id']))
    return client.post(path, data=data, base_url=ORIGIN, headers={'Origin': ORIGIN})


def order_form(s):
    values = MultiDict(s['header'])
    values.update(dict(draft_key='web-order-key', revision='1', plan_version_id=str(s['plan'])))
    for line in s['lines']:
        for key, source in [('work_item_id', 'work_item_id'), ('planned_quantity', 'planned_quantity'), ('line_remarks', 'remarks')]:
            values.add(key, str(line.get(source, '')))
    for index, crew in enumerate(s['crew']):
        values.add('crew_row', str(index))
        values.add('crew_specialty_' + str(index), str(crew['specialty_id']))
        values.add('crew_count_' + str(index), str(crew['planned_count']))
        for e in crew['employee_ids']: values.add('crew_employee_' + str(index), str(e))
    return values


@pytest.mark.parametrize('path', ['/personnel', '/personnel/contractors/new', '/personnel/employees/new',
                                 '/personnel/specialties/new', '/work-orders', '/work-orders/new'])
def test_new_module_pages_open_locally(client, path):
    response = get(client, path)
    assert response.status_code == 200
    assert '<html lang="ru">' in response.get_data(as_text=True)


def test_contractors_and_employee_creation_edit_archive_over_web(client, db):
    assert post(client, '/personnel/contractors/new', dict(name='ООО Строй')).status_code == 303
    assert post(client, '/personnel/specialties/new', dict(name='Штукатур', code='ШТ')).status_code == 303
    assert post(client, '/personnel/employees/new', dict(full_name='Иванов', contractor_id='1', specialty_id='1', position='Штукатур')).status_code == 303
    assert post(client, '/personnel/contractors/1/edit', dict(name='ООО Строй 2')).status_code == 303
    assert post(client, '/personnel/contractors/1/status', dict(action='archive')).status_code == 303
    assert 'В архиве' in get(client, '/personnel').get_data(as_text=True)
    assert post(client, '/personnel/contractors/1/status', dict(action='restore')).status_code == 303
    assert db.execute('SELECT name FROM contractors WHERE id=1').fetchone()[0] == 'ООО Строй 2'


def test_complete_document_web_flow_create_edit_issue_print_cancel(client, db, setup_orders):
    s = setup_orders
    response = post(client, '/work-orders/new', order_form(s))
    assert response.status_code == 303
    detail = response.location
    assert 'Черновик' in get(client, detail).get_data(as_text=True)
    edit = get(client, detail + '/edit')
    assert edit.status_code == 200
    form = order_form(s)
    form['notes'] = 'Уточнённое задание'
    assert post(client, detail + '/edit', form).status_code == 303
    assert post(client, detail + '/issue', {}).status_code == 303
    document = get(client, detail).get_data(as_text=True)
    assert 'НЗ-0001' in document and 'Выдан' in document and 'Уточнённое задание' in document
    assert get(client, detail + '/edit').status_code == 400
    assert post(client, detail + '/edit', form).status_code == 400
    printed = get(client, detail + '/print')
    assert printed.status_code == 200
    html = printed.get_data(as_text=True)
    assert 'НЗ-0001' in html and 'ООО Строй' in html and 'Иванов' in html and 'Зайцев' in html
    assert 'Работы проверил / принял' in html and 'не является юридически значимой' in html
    assert re.findall(r'<td[^>]+data-order-actual[^>]*>(.*?)</td>', html) == ['', '']
    assert re.findall(r'<td[^>]+data-order-confirmed[^>]*>(.*?)</td>', html) == ['', '']
    assert post(client, detail + '/cancel', dict(reason='')).status_code == 400
    assert post(client, detail + '/cancel', dict(reason='Перенос смены')).status_code == 303
    assert 'Перенос смены' in get(client, detail).get_data(as_text=True)
    assert 'НЗ-0001' in get(client, '/work-orders?status=cancelled&search=НЗ-0001').get_data(as_text=True)
    assert db.execute('SELECT count(*) FROM fact_events').fetchone()[0] == 0


def test_bad_form_keeps_submitted_rows_and_creates_nothing(client, db, setup_orders):
    values = order_form(setup_orders)
    values.setlist('planned_quantity', ['-1', '60'])
    values['notes'] = 'Сохраните введённый текст'
    response = post(client, '/work-orders/new', values)
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert 'Сохраните введённый текст' in html and 'value="-1"' in html
    assert 'crew_employee_0' in html and 'crew_employee_1' in html
    assert db.execute('SELECT count(*) FROM work_orders').fetchone()[0] == 0


def test_archived_building_not_in_form_but_kept_in_issued_document(client, setup_orders):
    s = setup_orders
    order = s['orders'].save_draft(s['header'], s['lines'], s['crew'])
    s['orders'].issue(order)
    s['work'].archive_building(1)
    html = get(client, '/work-orders/new').get_data(as_text=True)
    assert 'data-building="1"' not in html
    assert 'Корпус А' in get(client, '/work-orders/{}/print'.format(order)).get_data(as_text=True)
    assert 'НЗ-0001' in get(client, '/work-orders?building_id=1').get_data(as_text=True)


@pytest.mark.parametrize('suffix, method', [('', 'get'), ('/edit', 'get'), ('/edit', 'post'), ('/issue', 'post'), ('/cancel', 'post'), ('/print', 'get')])
def test_direct_urls_and_posts_cannot_access_other_project_orders(client, db, setup_orders, suffix, method):
    s = setup_orders
    order = s['orders'].save_draft(s['header'], s['lines'], s['crew'])
    project = ProjectRepository(db).create_project('Другой объект', 'OTHER')
    assert post(client, '/projects/select', dict(project_id=project)).status_code == 303
    path = '/work-orders/{}{}'.format(order, suffix)
    response = get(client, path) if method == 'get' else post(client, path, dict(reason='Подмена'))
    assert response.status_code == 400
    assert s['orders'].get_order(order)['status'] == 'draft'
    assert 'ООО Строй' not in get(client, '/work-orders').get_data(as_text=True)


@pytest.mark.parametrize('kind', ['contractors', 'employees'])
def test_foreign_personnel_cannot_be_edited_or_archived(client, db, setup_orders, kind):
    s = setup_orders
    project = ProjectRepository(db).create_project('Другой объект', 'OTHER')
    post(client, '/projects/select', dict(project_id=project))
    record = s['contractor'] if kind == 'contractors' else s['issuer']
    assert get(client, '/personnel/{}/{}/edit'.format(kind, record)).status_code == 400
    assert post(client, '/personnel/{}/{}/status'.format(kind, record), dict(action='archive')).status_code == 400


@pytest.mark.parametrize('guard', ['csrf', 'origin', 'workspace'])
def test_issue_protected_against_csrf_origin_and_stale_workspace(client, setup_orders, guard):
    s = setup_orders
    order = s['orders'].save_draft(s['header'], s['lines'], s['crew'])
    get(client, '/')
    with client.session_transaction(base_url=ORIGIN) as state:
        data = dict(csrf_token=state['csrf_token'], workspace_id='1')
    if guard == 'csrf': data['csrf_token'] = 'invalid'
    if guard == 'workspace': data['workspace_id'] = '2'
    response = client.post('/work-orders/{}/issue'.format(order), base_url=ORIGIN, data=data,
                           headers={'Origin': 'http://foreign.invalid' if guard == 'origin' else ORIGIN})
    assert response.status_code == 403
    assert s['orders'].get_order(order)['order_number'] is None


def test_print_styles_are_local_and_landscape(client):
    css = get(client, '/static/css/work_order_print.css')
    assert css.status_code == 200
    assert 'A4 landscape' in css.get_data(as_text=True) and '@media print' in css.get_data(as_text=True)
