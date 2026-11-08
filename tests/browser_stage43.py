"""Optional Stage 4.3 Chromium acceptance using an isolated synthetic database.

Run with a Python that has Playwright: ``python tests/browser_stage43.py``.
The Flask application runs in .venv/bin/python over a JSON/stdin bridge to its
real test_client. Chromium intercepts every HTTP request, including POST, CSRF,
static assets and XLSX downloads. This tests browser DOM/native forms without
opening WSGI sockets; it does not claim native TCP transport coverage.
No working database is opened. Only evidence files under docs/ are written.
"""
import base64
from datetime import datetime, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlencode, urlsplit
import xml.etree.ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ORIGIN = 'http://127.0.0.1:5000'
PERIOD = {'start_date': '2026-10-01', 'end_date': '2026-10-01'}


def seed(path):
    from app.database import initialize
    from app.repositories.brigade_repository import BrigadeRepository
    from app.repositories.event_repository import EventRepository
    from app.repositories.personnel_repository import PersonnelRepository
    from app.repositories.timesheet_repository import TimesheetRepository
    from app.repositories.work_repository import WorkRepository
    from app.time_utils import instant
    db = initialize(path)
    work = WorkRepository(db)
    floor = work.add_floor(1, '1', 1)
    item = work.add_work_item(1, floor, 1)
    plan = work.create_plan_version('CONSTRUCTION', created_at='2026-01-01T00:00:00Z')
    work.add_plan_line(plan, item, '1000', '0.5')
    work.approve_plan_version(plan, approved_at='2026-01-01T01:00:00Z')
    people = PersonnelRepository(db)
    contractor = people.save_contractor({'name': 'Синтетический подрядчик 43'})
    specialty = people.save_specialty('Синтетический монтажник 43', 'BROWSER43')
    brigade_repo = BrigadeRepository(db)
    brigade = brigade_repo.save(contractor, '01')
    future_brigade = brigade_repo.save(contractor, '02')
    employees = [people.save_worker(dict(full_name='Учебный Работник {:02d}'.format(i + 1),
        contractor_id=contractor, specialty_id=specialty, effective_date='2026-01-01')) for i in range(5)]
    for employee in employees:
        brigade_repo.assign(employee, brigade, '2026-01-01')
    # The fifth worker has moved by the current date but belongs to 01 on Oct 1.
    brigade_repo.assign(employees[-1], future_brigade, '2026-10-02')
    timesheets = TimesheetRepository(db)
    sheet = timesheets.create(contractor, 2026, 10)
    timesheets.set_entries(sheet, [dict(employee_id=e, work_date='2026-10-01', code='Я', hours='8') for e in employees])
    timesheets.transition(sheet, 'submit')
    timesheets.transition(sheet, 'approve')
    event = EventRepository(db).add_fact(item, '100', '2026-10-01',
        event_key='browser43-fact', recorded_at='2026-10-02T00:00:00Z')
    assert len(brigade_repo.members(brigade, '2026-10-01')) == 5
    assert len(brigade_repo.members(brigade, '2026-10-02')) == 4
    result = dict(project=1, building=1, floor=floor, item=item, plan=plan,
        contractor=contractor, brigade=brigade, future_brigade=future_brigade,
        employees=employees, event=event, cutoff_before_allocations=instant())
    db.close()
    return result


def application(folder):
    from app.web import create_app
    path = folder / 'synthetic-stage43.sqlite3'
    fixture = seed(path)
    app = create_app({'DATABASE': str(path), 'TESTING': True,
        'SECRET_KEY': 'isolated-browser43-fixture', 'LOCAL_ORIGIN': ORIGIN})
    return app, app.test_client(), fixture, path


def url(route, **query):
    return route + '?' + urlencode(dict(PERIOD, **query))


def token(html, name='csrf_token'):
    match = re.search(r'name="' + re.escape(name) + r'" value="([^"]*)"', html)
    assert match, name
    return match.group(1)


def workbook_cells(blob):
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with ZipFile(io.BytesIO(blob)) as archive:
        workbook = ET.fromstring(archive.read('xl/workbook.xml'))
        names = [s.attrib['name'] for s in workbook.findall('m:sheets/m:sheet', ns)]
        result = {}
        for i, name in enumerate(names, 1):
            xml = ET.fromstring(archive.read('xl/worksheets/sheet%d.xml' % i))
            cells = {}
            for cell in xml.findall('.//m:c', ns):
                assert cell.find('m:f', ns) is None, 'Unexpected formula'
                value = cell.find('m:v', ns)
                if value is not None and cell.attrib.get('t', 'n') == 'n':
                    cells[cell.attrib['r']] = Decimal(value.text)
            result[name] = cells
        return result


def backend_acceptance():
    """Real routes and rendered forms; explicitly separate from browser evidence."""
    from app.database import connect
    from app.repositories.labor_repository import LaborRepository
    from app.services.labor_productivity import LaborProductivityService
    checks = []
    with tempfile.TemporaryDirectory(prefix='construction-progress-stage43-route-') as tmp:
        app, client, f, path = application(Path(tmp))
        allocation_url = url('/labor/allocations', work_date='2026-10-01',
            work_item_id=f['item'], brigade_id=f['brigade'])

        def get(target):
            r = client.get(target, base_url=ORIGIN)
            assert r.status_code == 200, (target, r.status_code, r.get_data(as_text=True))
            return r.get_data(as_text=True)

        def post(target, data, expected=303):
            r = client.post(target, base_url=ORIGIN, headers={'Origin': ORIGIN}, data=data)
            assert r.status_code == expected, (target, r.status_code, r.get_data(as_text=True))
            return r

        def form(html, **fields):
            return dict(csrf_token=token(html), workspace_id='1', **fields)

        def report(**kwargs):
            db = connect(path)
            try:
                return LaborProductivityService(db).build(**PERIOD, **kwargs)
            finally:
                db.close()

        html = get(url('/labor'))
        assert 'data-worked-hours>40<' in html and 'data-allocated-hours>0<' in html
        assert 'data-unallocated-hours>40<' in html
        checks.append('Overview: approved 40h, allocated 0h, unallocated 40h before explicit allocation')
        html = get(allocation_url)
        assert html.count('data-labor-worker=') == 5
        assert 'Учебный Работник 05' in html
        checks.append('Historical brigade roster shows all five Oct 1 members after worker 5 moved on Oct 2')
        db = connect(path)
        source_ids = {r['employee_id']: r['entry_id'] for r in LaborRepository(db).available_hours('2026-10-01')}
        db.close()
        first = f['employees'][0]
        post(allocation_url, form(html, action='allocate', work_date='2026-10-01',
            work_item_id=str(f['item']), operation_key='browser43-individual', employee_id=str(first),
            expected_revision='0', expected_timesheet_entry_id=str(source_ids[first]), hours='8', reason=''))
        assert report()['summary']['allocated_hours'] == 8
        assert report()['attributions'] == []
        checks.append('Individual native-form route allocates 8h and does not invent volume attribution')
        html = get(allocation_url)
        payload = form(html, action='bulk_preview', work_date='2026-10-01',
            work_item_id=str(f['item']), operation_key='browser43-bulk', reason='',
            employee_id=[str(e) for e in f['employees'][1:]])
        for e in f['employees'][1:]:
            payload.update({'hours_' + str(e): '8', 'revision_' + str(e): '0', 'entry_' + str(e): str(source_ids[e])})
        preview = post(allocation_url, payload, 200).get_data(as_text=True)
        assert 'Предварительный просмотр распределения' in preview
        assert report()['summary']['allocated_hours'] == 8
        confirm = form(preview, action='bulk_confirm', preview_token=token(preview, 'preview_token'), confirm_changes='1')
        post(allocation_url, confirm)
        assert report()['summary']['allocated_hours'] == 40
        post(allocation_url, confirm)  # idempotent signed preview replay
        assert report()['summary']['allocated_hours'] == 40
        checks.append('Bulk preview writes nothing; explicit confirmation writes 32h once, replay stays 40h total')
        attr_url = url('/labor/attributions')
        html = get(attr_url)
        post(attr_url, form(html, operation_key='browser43-attribution', expected_revision='0',
            fact_event_id=str(f['event']), contractor_id=str(f['contractor']), brigade_id=str(f['brigade']),
            employee_id='', quantity='100', reason='Учебная приёмка: измеренный объём'))
        data = report()
        s = data['summary']
        assert [s[k] for k in ('worked_hours', 'allocated_hours', 'unallocated_hours', 'normative_hours', 'unique_workers', 'average_daily_workers')] == [40, 40, 0, 50, 5, 5]
        for tab in ('works', 'contractors', 'brigades'):
            metric = data[tab][0]
            assert [metric[k] for k in ('quantity', 'hours', 'normative_hours', 'actual_output', 'norm_output', 'norm_ratio')] == [100, 40, 50, Decimal('2.5'), 2, Decimal('1.25')]
            assert metric['complete']
            html = get(url('/labor/' + tab))
            assert '2,5' in html and '125,0%' in html and 'Полные данные' in html
        checks.append('Explicit 100m² attribution: 40h, norm 0.5h/m², 50 normative h, output 2.5m²/h, norm output 2m²/h, 125% across all metric screens')
        get(url('/labor', building_id=1, floor_id=f['floor'], catalog_id=1,
            contractor_id=f['contractor'], brigade_id=f['brigade'], plan_version_id=f['plan'], element='Стены'))
        historical = get(url('/labor/allocations', work_date='2026-10-01', work_item_id=f['item'],
            brigade_id=f['brigade'], knowledge_cutoff=f['cutoff_before_allocations']))
        assert 'Исторический срез доступен только для чтения' in historical
        assert 'class="labor-row-form"' not in historical and 'id="labor-bulk-form"' not in historical
        old = report(knowledge_cutoff=f['cutoff_before_allocations'])
        assert old['summary']['allocated_hours'] == 0 and old['attributions'] == []
        history = get(url('/labor/history'))
        assert history.count('Распределение часов') >= 2 and 'Назначение объёма' in history
        assert 'Учебная приёмка: измеренный объём' in history
        get(url('/labor/contractors', mode='confirmed'))
        checks.append('Combined filters and frozen approved plan work; knowledge cutoff preserves pre-allocation history and removes mutation forms; history and confirmed mode render')
        export = client.get(url('/labor/export.xlsx'), base_url=ORIGIN)
        assert export.status_code == 200 and 'attachment;' in export.headers['Content-Disposition']
        cells = workbook_cells(export.data)
        assert len(cells) == 7
        assert [cells['01. Сводка']['B%d' % i] for i in range(2, 8)] == [40, 40, 0, 50, 5, 5]
        assert [cells['03. Выработка'][c + '2'] for c in 'DEFGH'] == [100, 40, Decimal('2.5'), 2, Decimal('1.25')]
        for name in ('04. Подрядчики', '05. Бригады'):
            assert [cells[name][c + '2'] for c in 'FGHIJ'] == [100, 40, Decimal('2.5'), 2, Decimal('1.25')]
        checks.append('XLSX attachment: seven sheets; exact numeric summary and work/contractor/brigade metrics equal service and rendered screen values; no formulas')
        html = get(allocation_url)
        bad = form(html, action='allocate', work_date='2026-10-01', work_item_id=str(f['item']),
            operation_key='browser43-overcapacity', employee_id=str(first), expected_revision='1',
            expected_timesheet_entry_id=str(source_ids[first]), hours='9', reason='Негативная проверка')
        post(allocation_url, bad, 400)
        post(allocation_url, {'workspace_id': '1', 'action': 'allocate'}, 403)
        assert report()['summary']['allocated_hours'] == 40
        checks.append('Intentional negative routes: overcapacity rejected 400 and missing CSRF rejected 403; approved totals unchanged')
        db = connect(path)
        assert db.execute('SELECT COUNT(*) FROM fact_events').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM confirmation_events').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM labor_operations').fetchone()[0] == 3
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        db.close()
    return dict(status='passed', transport='Flask test_client in process; no browser', checks=checks,
        expected_http_errors=[400, 403], numeric_fixture=dict(workers=5, hours_per_worker=8,
        quantity_m2=100, labor_norm='0.5', worked_hours=40, allocated_hours=40,
        normative_hours=50, actual_output='2.5', norm_output='2', norm_ratio='1.25'), working_database_opened=False)


def worker():
    """Single real Flask test_client with persistent cookies, driven through pipes."""
    with tempfile.TemporaryDirectory(prefix='construction-progress-stage43-browser-') as tmp:
        app, client, fixture, path = application(Path(tmp))
        print(json.dumps({'fixture': fixture}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            split = urlsplit(request['url'])
            headers = {k: v for k, v in request['headers'].items() if k.lower() not in ('host', 'content-length', 'cookie')}
            response = client.open(split.path + ('?' + split.query if split.query else ''),
                method=request['method'], base_url=ORIGIN, headers=headers,
                data=base64.b64decode(request.get('body', '')),
                environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
            print(json.dumps({'status': response.status_code, 'headers': dict(response.headers),
                'body': base64.b64encode(response.data).decode('ascii')}), flush=True)


def browser_acceptance(results):
    from playwright.sync_api import sync_playwright
    args = ['--no-sandbox', '--disable-dev-shm-usage']
    with sync_playwright() as pw:
        browser = None
        for extra in ([], ['--disable-crash-reporter', '--disable-crashpad', '--noerrdialogs', '--no-zygote', '--single-process']):
            try:
                browser = pw.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=args + extra, timeout=10000)
                break
            except Exception as error:
                results['launch_attempts'].append(dict(args=args + extra, error=str(error)))
        if browser is None:
            results['status'] = 'blocked'
            results['blocker'] = 'Chromium exits before page creation: crashpad setsockopt returns EPERM (Operation not permitted), SIGTRAP in default execution sandbox. Both bounded launch variants failed. No screenshots or browser interaction claims are made.'
            return
        process = subprocess.Popen([str(ROOT / '.venv/bin/python'), str(Path(__file__)), '--worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT)
        try:
            fixture = json.loads(process.stdout.readline())['fixture']
            context = browser.new_context(viewport={'width': 1366, 'height': 768}, accept_downloads=True)
            page = context.new_page()
            results['browser_page_created'] = True
            results['browser_observation_status'] = 'collecting'

            def bridge(route):
                request = route.request
                if not request.url.startswith(ORIGIN + '/'):
                    results['external_requests'].append(request.url)
                    route.abort()
                    return
                payload = dict(url=request.url, method=request.method, headers=request.all_headers(),
                    body=base64.b64encode(request.post_data_buffer or b'').decode('ascii'))
                process.stdin.write(json.dumps(payload) + '\n')
                process.stdin.flush()
                response = json.loads(process.stdout.readline())
                route.fulfill(status=response['status'], headers=response['headers'],
                    body=base64.b64decode(response['body']))

            context.route('**/*', bridge)
            page.on('pageerror', lambda e: results['javascript_errors'].append(str(e)))
            page.on('console', lambda m: results['console_errors'].append(m.text) if m.type == 'error' else None)
            page.on('response', lambda r: results['unexpected_http_errors'].append([r.status, r.url]) if r.status >= 400 else None)

            def goto(target):
                response = page.goto(ORIGIN + target, wait_until='networkidle')
                assert response.status == 200, (target, response.status)

            def submit(form, name):
                with page.expect_navigation(wait_until='networkidle'):
                    form.get_by_role('button', name=name, exact=True).click()

            def screenshot(name):
                target = ROOT / 'docs/screenshots' / ('stage43_' + name + '.png')
                target.parent.mkdir(exist_ok=True)
                page.screenshot(path=str(target), full_page=False, animations='disabled')
                results['screenshots'].append(str(target.relative_to(ROOT)))

            goto(url('/labor'))
            assert page.locator('[data-worked-hours]').inner_text() == '40'
            assert page.locator('[data-unallocated-hours]').inner_text() == '40'
            allocation = url('/labor/allocations', work_date='2026-10-01', work_item_id=fixture['item'], brigade_id=fixture['brigade'])
            goto(allocation)
            assert page.locator('[data-labor-worker]').count() == 5
            first = fixture['employees'][0]
            row_form = page.locator('[data-labor-worker="%d"] .labor-row-form' % first)
            row_form.locator('[name="hours"]').fill('8')
            submit(row_form, 'Сохранить')
            for employee in fixture['employees'][1:]:
                page.locator('[form="labor-bulk-form"][name="employee_id"][value="%d"]' % employee).check()
                page.locator('[name="hours_%d"]' % employee).fill('8')
            with page.expect_navigation(wait_until='networkidle'):
                page.get_by_role('button', name='Предварительный просмотр выбранных работников', exact=True).click()
            screenshot('bulk_preview')
            form = page.locator('form').filter(has=page.locator('[name="preview_token"]'))
            form.locator('[name="confirm_changes"]').check()
            submit(form, 'Подтвердить распределение')
            goto(url('/labor/attributions'))
            form = page.locator('form.form-grid')
            form.locator('[name="fact_event_id"]').select_option(str(fixture['event']))
            form.locator('[name="contractor_id"]').select_option(str(fixture['contractor']))
            form.locator('[name="brigade_id"]').select_option(str(fixture['brigade']))
            form.locator('[name="quantity"]').fill('100')
            form.locator('[name="reason"]').fill('Учебная браузерная приёмка')
            submit(form, 'Назначить объём')
            goto(url('/labor'))
            assert page.locator('[data-allocated-hours]').inner_text() == '40'
            assert page.locator('[data-unallocated-hours]').inner_text() == '0'
            screenshot('overview')
            for tab in ('works', 'contractors', 'brigades'):
                goto(url('/labor/' + tab))
                row = page.locator('[data-labor-code]').first
                assert '2,5' in row.inner_text() and '125,0%' in row.inner_text()
            screenshot('brigades')
            filters = page.locator('form.filter-form').first
            filters.locator('[name="contractor_id"]').select_option(str(fixture['contractor']))
            filters.locator('[name="brigade_id"]').select_option(str(fixture['brigade']))
            submit(filters, 'Применить')
            assert '125,0%' in page.locator('[data-labor-code]').first.inner_text()
            with page.expect_download() as download:
                page.get_by_role('link', name='Скачать Excel', exact=True).click()
            blob = Path(download.value.path()).read_bytes()
            cells = workbook_cells(blob)
            assert cells['01. Сводка']['B2'] == 40
            assert [cells['03. Выработка'][c + '2'] for c in 'DEFGH'] == [100, 40, Decimal('2.5'), 2, Decimal('1.25')]
            goto(url('/labor/history'))
            assert 'Учебная браузерная приёмка' in page.locator('main').inner_text()
            goto(url('/labor/allocations', work_date='2026-10-01', work_item_id=fixture['item'],
                brigade_id=fixture['brigade'], knowledge_cutoff=fixture['cutoff_before_allocations']))
            assert page.locator('.labor-row-form').count() == 0
            assert page.locator('#labor-bulk-form').count() == 0
            assert not results['javascript_errors'] and not results['console_errors']
            assert not results['unexpected_http_errors'] and not results['external_requests']
            results.update(status='passed', chromium_version=browser.version,
                browser_observation_status='completed',
                checks=['Overview, individual allocation, historical brigade selection, bulk preview/confirmation, explicit volume attribution, three metric dashboards, filters, historical read-only forms, history, native XLSX download with identical numeric metrics'])
        finally:
            process.terminate()
            process.wait(timeout=5)
            browser.close()


def main():
    evidence = dict(stage='4.3', captured_at=datetime.now(timezone.utc).isoformat(),
        status='pending', environment='Linux default execution sandbox', viewport=[1366, 768],
        transport='Chromium request interception to real Flask test_client over subprocess stdio; native TCP/WSGI transport not tested',
        launch_attempts=[], checks=[], screenshots=[], javascript_errors=[], console_errors=[],
        unexpected_http_errors=[], external_requests=[], browser_page_created=False,
        browser_observation_status='not_observed: no Chromium page created; empty error/request arrays do not establish browser acceptance',
        safari_tested=False, working_database_opened=False)
    backend = subprocess.run([str(ROOT / '.venv/bin/python'), str(Path(__file__)), '--backend'],
        cwd=ROOT, text=True, capture_output=True, timeout=60)
    if backend.returncode:
        evidence['backend_acceptance'] = dict(status='failed', error=backend.stderr)
    else:
        evidence['backend_acceptance'] = json.loads(backend.stdout)
    try:
        browser_acceptance(evidence)
    except Exception as error:
        evidence.update(status='failed', error=str(error))
    target = ROOT / 'docs/stage43/browser_results.json'
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': evidence['status'], 'backend': evidence['backend_acceptance']['status'],
        'evidence': str(target.relative_to(ROOT)), 'screenshots': evidence['screenshots']}, ensure_ascii=False))
    return 0 if evidence['status'] == 'passed' else 2


if __name__ == '__main__':
    if '--worker' in sys.argv:
        worker()
    elif '--backend' in sys.argv:
        print(json.dumps(backend_acceptance(), ensure_ascii=False))
    else:
        raise SystemExit(main())
