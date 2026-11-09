"""Stage 4.4 acceptance on disposable, explicitly synthetic payroll sources.

Run ``python tests/browser_stage44.py`` using the Python with Playwright.
Real Flask routes run in .venv/bin/python over the Stage 4.3 stdio pattern.
Only the evidence JSON and actual Chromium screenshots are retained. Native
TCP/WSGI transport and Safari are outside this harness's coverage.
``python tests/browser_stage44.py --refresh-backend`` preserves an existing
browser launch observation and refreshes only isolated backend acceptance.
"""
import base64
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4
from zipfile import ZipFile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ORIGIN = 'http://127.0.0.1:5000'
N = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def token(html, name='csrf_token'):
    match = re.search(r'name="' + re.escape(name) + r'" value="([^"]*)"', html)
    assert match, name
    return match.group(1)


def application(folder):
    from app.database import initialize
    from app.repositories.brigade_repository import BrigadeRepository
    from app.repositories.event_repository import EventRepository
    from app.repositories.personnel_repository import PersonnelRepository
    from app.repositories.timesheet_repository import TimesheetRepository
    from app.repositories.work_repository import WorkRepository
    from app.web import create_app
    path = folder / 'synthetic-stage44.sqlite3'
    db = initialize(path)
    people = PersonnelRepository(db)
    specialty = people.save_specialty('Учебная специальность 44', 'BROWSER44')
    fixture = {}
    for name, count, days in [('hourly', 1, 20), ('salary', 1, 15), ('piece', 5, 0),
                              ('combined', 1, 15), ('rate_change', 1, 20), ('correction', 1, 20)]:
        contractor = people.save_contractor({'name': 'Учебный подрядчик 44 ' + name})
        workers = [people.save_worker(dict(full_name='Учебный Исполнитель 44 ' + name + ' ' + str(i + 1),
            contractor_id=contractor, specialty_id=specialty, effective_date='2026-01-01')) for i in range(count)]
        fixture[name] = dict(contractor=contractor, workers=workers)
        if days:
            sheets = TimesheetRepository(db)
            sheet = sheets.create(contractor, 2026, 10)
            sheets.set_entries(sheet, [dict(employee_id=e, work_date='2026-10-%02d' % day,
                code='Я', hours='8') for e in workers for day in range(1, days + 1)])
            sheets.transition(sheet, 'submit')
            sheets.transition(sheet, 'approve')
    work = WorkRepository(db)
    floor = work.add_floor(1, '1', 1)
    item = work.add_work_item(1, floor, 1)
    plan = work.create_plan_version('CONSTRUCTION', created_at='2026-01-01T00:00:00Z')
    work.add_plan_line(plan, item, '1000', '0.5')
    work.approve_plan_version(plan, approved_at='2026-01-01T01:00:00Z')
    events = EventRepository(db)
    events.add_fact(item, '100', '2026-10-01', event_key='stage44-synthetic-fact')
    confirmation = events.add_confirmation(item, '100', '2026-10-01',
        event_key='stage44-synthetic-confirmation', document_ref='Учебный акт 44')
    brigade_repo = BrigadeRepository(db)
    brigade = brigade_repo.save(fixture['piece']['contractor'], '44')
    for employee in fixture['piece']['workers']:
        brigade_repo.assign(employee, brigade, '2026-01-01')
    catalog = dict(db.execute('SELECT c.id,c.unit FROM work_catalog c JOIN work_items w ON w.catalog_id=c.id WHERE w.id=?', (item,)).fetchone())
    fixture['piece'].update(brigade=brigade, confirmation=confirmation, item=item, **catalog)
    db.close()
    app = create_app({'DATABASE': str(path), 'TESTING': True,
        'SECRET_KEY': 'isolated-stage44-synthetic-only', 'LOCAL_ORIGIN': ORIGIN})
    return app, app.test_client(), fixture, path


def inspect_fixture(path, kind, **params):
    from app.database import connect
    from app.repositories.payroll_repository import PayrollRepository
    db = connect(path)
    try:
        if kind == 'periods':
            return PayrollRepository(db).list_periods()
        if kind == 'report':
            return PayrollRepository(db).report(params['period_id'], params.get('version_id'))
        if kind == 'piece_state':
            return dict(claims=[dict(r) for r in db.execute('SELECT id FROM payroll_claims')],
                batches=db.execute('SELECT count(*) FROM payroll_piece_batches').fetchone()[0])
        if kind == 'sources':
            # No primary-source row contents leave this function or enter logs.
            primary = {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY id')]
                for table in ('timesheet_entries', 'fact_events', 'confirmation_events')}
            return dict(sha256=hashlib.sha256(json.dumps(primary, default=str).encode()).hexdigest(),
                integrity=db.execute('PRAGMA integrity_check').fetchone()[0])
        raise AssertionError('Unknown fixture inspection')
    finally:
        db.close()


def workbook(blob, total, delta=0):
    from_names = ('01. Сводка', '02. По работникам', '03. Почасовая часть', '04. Окладная часть',
                  '05. Сдельная часть', '06. Премии и корректировки', '07. Применённые расценки', '08. Источники')
    with ZipFile(io.BytesIO(blob)) as archive:
        assert archive.testzip() is None
        root = ET.fromstring(archive.read('xl/workbook.xml'))
        assert tuple(s.get('name') for s in root.findall('m:sheets/m:sheet', N)) == from_names
        summary = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        for cell, value in [('C2', total), ('D2', delta)]:
            node = summary.find('.//m:c[@r="' + cell + '"]', N)
            assert node.get('t') == 'n' and Decimal(node.find('m:v', N).text) == Decimal(str(value))
        component_total = Decimal(0)
        for index in range(3, 7):
            root = ET.fromstring(archive.read('xl/worksheets/sheet%d.xml' % index))
            for row in root.findall('m:sheetData/m:row', N)[1:]:
                node = row.find('m:c[@r="F' + row.get('r') + '"]', N)
                if node is not None and node.find('m:v', N) is not None:
                    assert node.get('t') == 'n'
                    component_total += Decimal(node.find('m:v', N).text)
        assert component_total == Decimal(str(total))
        for member in archive.namelist():
            if member.endswith('.xml'):
                assert not ET.fromstring(archive.read(member)).findall('.//m:f', N)
    return list(from_names)


class RouteSession:
    def __init__(self, client):
        self.client, self.html = client, ''

    def get(self, target):
        response = self.client.get(target, base_url=ORIGIN)
        assert response.status_code == 200, (target, response.status_code)
        self.html = response.get_data(as_text=True)
        return self.html

    def post(self, target, values, expected=303):
        payload = dict(csrf_token=token(self.html), workspace_id='1', operation_key=uuid4().hex, **values)
        response = self.client.post(target, base_url=ORIGIN, headers={'Origin': ORIGIN}, data=payload)
        assert response.status_code == expected, (target, response.status_code, response.get_data(as_text=True))
        if response.status_code == 303:
            self.get(response.headers['Location'])
        else:
            self.html = response.get_data(as_text=True)
        return self.html

    def download(self, target):
        response = self.client.get(target, base_url=ORIGIN)
        assert response.status_code == 200 and 'attachment' in response.headers['Content-Disposition']
        return response.data

    def screenshot(self, name):
        pass


def scenarios(session, inspect, fixture):
    checks, periods, export_errors = [], {}, []
    before = inspect('sources')

    def verify_export(target, total, delta=0):
        try:
            return workbook(session.download(target), total, delta)
        except AssertionError as error:
            export_errors.append(dict(route=target, error=str(error) or 'XLSX response or numeric workbook assertion failed'))
            return []
    for route in ('/payroll', '/payroll/terms', '/payroll/rates', '/payroll/operations',
                  '/payroll/periods', '/payroll/periods/new', '/payroll/claims'):
        html = session.get(route)
        assert 'Управленческие начисления' in html and html.count('data-nav-group="payroll"') == 1
    checks.append('All seven payroll sections render with one navigation group')
    settings = dict(hourly={}, salary=dict(system='salary', salary='80000'),
        piece=dict(system='piece'), combined=dict(system='combined', base_kind='salary', salary='80000',
        bonus_kind='percent', bonus_value='20'), rate_change=dict(valid_to='2026-10-11'),
        correction=dict(system='salary', salary='50000'))
    for name, values in settings.items():
        case = fixture[name]
        for employee in case['workers']:
            session.get('/payroll/terms')
            payload = dict(employee_id=str(employee), contractor_id=str(case['contractor']), valid_from='2026-10-01',
                valid_to='', system='hourly', base_kind='hourly', hourly_rate='350', salary='0', currency='RUB',
                bonus_kind='none', bonus_basis='base', bonus_value='0', performance_threshold='1',
                fixed_supplement='0', status='active', expected_revision='0')
            payload.update(values)
            session.post('/payroll/terms', payload)
            if name == 'rate_change':
                session.get('/payroll/terms')
                session.post('/payroll/terms', dict(payload, valid_from='2026-10-11', valid_to='', hourly_rate='400'))
        session.get('/payroll/periods/new')
        session.post('/payroll/periods/new', dict(contractor_id=str(case['contractor']), year='2026', month='10',
            norm_hours='160', comment='Только синтетическая учебная приёмка 44'))
        period = next(p for p in inspect('periods') if p['contractor_id'] == case['contractor'])
        periods[name] = (period['id'], period['version_id'])
    session.get('/payroll/terms')
    session.screenshot('terms')
    checks.append('Rendered form contracts save all four pay systems, explicit full-month norm, and two nonoverlapping rate intervals')
    piece = fixture['piece']
    pid, vid = periods['piece']
    session.get('/payroll/rates')
    session.post('/payroll/rates', dict(catalog_id=str(piece['id']), contractor_id=str(piece['contractor']),
        operation_id='', unit=piece['unit'], amount='300', currency='RUB', valid_from='2026-10-01', valid_to='',
        status='active', expected_revision='0'))
    session.get('/payroll/claims')
    session.post('/payroll/claims', dict(confirmation_event_id=str(piece['confirmation']),
        contractor_id=str(piece['contractor']), brigade_id=str(piece['brigade']), employee_id='',
        operation_id='', quantity='100', proof_ref='Учебное явное назначение пяти исполнителей', expected_revision='0'))
    claim = inspect('piece_state')['claims'][0]['id']
    session.get('/payroll/periods/' + str(pid))
    preview = session.post('/payroll/versions/%d/piece-preview' % vid, dict(claim_id=str(claim), quantity='100',
        method='equal', employee_id=list(map(str, piece['workers'])), reason='Учебный равный расчёт'), 200)
    assert 'Предварительный просмотр сдельного начисления' in preview
    assert inspect('piece_state')['batches'] == 0
    preview_id = token(preview, 'preview_id')
    session.screenshot('piece_preview')
    session.post('/payroll/versions/%d/piece-confirm' % vid, dict(preview_id=preview_id, confirm_changes='1'))
    assert inspect('piece_state')['batches'] == 1
    checks.append('Explicit confirmation claim: preview reserves nothing; checked confirmation creates one equal distribution')

    def action(name, action_name, **values):
        pid, vid = periods[name]
        session.get('/payroll/periods/%d?version_id=%d' % (pid, vid))
        session.post('/payroll/versions/%d/action' % vid, dict(action=action_name, **values))

    expected = dict(hourly=56000, salary=60000, piece=30000, combined=72000, rate_change=60000, correction=50000)
    for name, total in expected.items():
        action(name, 'calculate')
        pid, vid = periods[name]
        report = inspect('report', period_id=pid, version_id=vid)
        assert not report['errors'], (name, report['errors'])
        assert Decimal(str(report['totals']['RUB'])) == total
        rows = report['rows']
        if name == 'piece':
            assert len(rows) == 5 and all(Decimal(str(r['total'])) == 6000 for r in rows)
        if name == 'combined':
            assert Decimal(str(rows[0]['base'])) == 60000 and Decimal(str(rows[0]['bonus'])) == 12000
        html = session.get('/payroll/periods/%d?version_id=%d' % (pid, vid))
        assert str(total) in html.replace('\u00a0', '').replace('\u202f', '').replace(' ', '')
        verify_export('/payroll/periods/%d/export.xlsx?version_id=%d' % (pid, vid), total)
    session.get('/payroll/periods/%d?version_id=%d' % periods['combined'])
    session.screenshot('combined')
    checks.append('Exact synthetic totals: hourly 160×350; salary 80000×120/160; piece 100×300÷5; combined base plus 20%; rate switch 80×350+80×400')
    # Exercise a reasoned manual bonus separately, then preserve the requested
    # clean approved correction example on its own contractor's period.
    action('hourly', 'adjustment', employee_id=str(fixture['hourly']['workers'][0]), amount='500', kind='bonus',
        currency='RUB', effective_date='2026-10-01', reason='Учебная ручная премия с основанием')
    action('hourly', 'calculate')
    assert Decimal(str(inspect('report', period_id=periods['hourly'][0])['totals']['RUB'])) == 56500
    verify_export('/payroll/periods/%d/export.xlsx?version_id=%d' % periods['hourly'], 56500)
    for action_name in ('submit', 'approve'):
        action('correction', action_name)
    pid, old_vid = periods['correction']
    old = inspect('report', period_id=pid, version_id=old_vid)
    assert old['version']['status'] == 'approved'
    session.screenshot('approved')
    action('correction', 'correction', reason='Учебное исправление утверждённого расчёта')
    new_vid = next(p for p in inspect('periods') if p['id'] == pid)['version_id']
    assert new_vid != old_vid
    periods['correction'] = (pid, new_vid)
    action('correction', 'adjustment', employee_id=str(fixture['correction']['workers'][0]), amount='-2500',
        kind='adjustment', currency='RUB', effective_date='2026-10-01', reason='Учебное уточнение суммы')
    action('correction', 'calculate')
    corrected = inspect('report', period_id=pid, version_id=new_vid)
    assert Decimal(str(corrected['totals']['RUB'])) == 47500
    assert Decimal(str(corrected['delta']['RUB'])) == -2500
    session.screenshot('correction')
    prior = inspect('report', period_id=pid, version_id=old_vid)
    assert prior['run'] == old['run'] and prior['lines'] == old['lines'] and prior['totals'] == old['totals']
    old_html = session.get('/payroll/periods/%d?version_id=%d' % (pid, old_vid))
    assert 'предыдущая версия' in old_html and 'Ручная премия или корректировка' not in old_html
    verify_export('/payroll/periods/%d/export.xlsx?version_id=%d' % (pid, old_vid), 50000)
    sheets = verify_export('/payroll/periods/%d/export.xlsx?version_id=%d' % (pid, new_vid), 47500, -2500)
    checks.append('Reasoned manual bonus recalculates; approval freezes original; new correction is 47500 with -2500 delta while approved 50000 snapshot and export remain unchanged')
    after = inspect('sources')
    assert before == after and after['integrity'] == 'ok'
    checks.append('All eight XLSX sheets exist, totals and correction deltas are numeric and equal stored snapshots, XML has no formulas; primary sources unchanged and SQLite integrity ok')
    assert 'Фонд управленческих начислений' not in session.get('/')
    assert not export_errors, export_errors
    return dict(status='passed', checks=checks, xlsx_sheets=sheets,
        numeric_fixture=dict(source='synthetic disposable fixture only', hourly='160*350=56000', salary='80000*120/160=60000',
            piece='100*300/5=6000 each', combined='60000+20%=72000', rate_change='80*350+80*400=60000',
            manual_bonus='56000+500=56500', approved='50000', corrected='47500', correction_delta='-2500'),
        working_database_opened=False, primary_sources_unchanged=True)


def backend_acceptance():
    with tempfile.TemporaryDirectory(prefix='construction-progress-stage44-route-') as tmp:
        app, client, fixture, path = application(Path(tmp))

        def inspect(kind, **params):
            # Match the stdio worker's JSON types before browser execution.
            return json.loads(json.dumps(inspect_fixture(path, kind, **params), default=str))

        result = scenarios(RouteSession(client), inspect, fixture)
        # Intentional guard failures are backend observations, never browser claims.
        session = RouteSession(client)
        session.get('/payroll/terms')
        payload = dict(workspace_id='1', operation_key=uuid4().hex)
        r = client.post('/payroll/terms', base_url=ORIGIN, headers={'Origin': ORIGIN}, data=payload)
        assert r.status_code == 403
        payload['csrf_token'] = token(session.html)
        r = client.post('/payroll/terms', base_url=ORIGIN, headers={'Origin': 'https://example.invalid'}, data=payload)
        assert r.status_code == 403
        result.update(transport='real Flask test_client with rendered CSRF forms; no browser', expected_http_errors=[403])
        result['checks'].append('Missing CSRF and foreign Origin are rejected with 403')
        return result


def worker():
    with tempfile.TemporaryDirectory(prefix='construction-progress-stage44-browser-') as tmp:
        app, client, fixture, path = application(Path(tmp))
        print(json.dumps({'fixture': fixture}), flush=True)
        for line in sys.stdin:
            message = json.loads(line)
            if message.get('kind') == 'inspect':
                result = inspect_fixture(path, message['inspection'], **message.get('params', {}))
            else:
                split = urlsplit(message['url'])
                headers = {k: v for k, v in message['headers'].items() if k.lower() not in ('host', 'content-length', 'cookie')}
                response = client.open(split.path + ('?' + split.query if split.query else ''), method=message['method'],
                    base_url=ORIGIN, headers=headers, data=base64.b64decode(message.get('body', '')),
                    environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
                result = dict(status=response.status_code, headers=dict(response.headers), body=base64.b64encode(response.data).decode())
            print(json.dumps(result, default=str, ensure_ascii=False), flush=True)


def browser_acceptance(results):
    from playwright.sync_api import sync_playwright
    args = ['--no-sandbox', '--disable-dev-shm-usage']
    with sync_playwright() as pw:
        try:
            # Exactly one bounded default-sandbox attempt. No permission escalation.
            browser = pw.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=args, timeout=10000)
        except Exception as error:
            results['launch_attempts'].append(dict(args=args, error=str(error)))
            results.update(status='blocked', blocker='Chromium failed before page creation in the default execution sandbox. Exact launch stderr is retained in launch_attempts; browser interactions, JavaScript behavior and screenshots were not observed.')
            return
        process = subprocess.Popen([str(ROOT / '.venv/bin/python'), str(Path(__file__)), '--worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT)
        try:
            fixture = json.loads(process.stdout.readline())['fixture']

            def send(message):
                process.stdin.write(json.dumps(message) + '\n')
                process.stdin.flush()
                return json.loads(process.stdout.readline())

            context = browser.new_context(viewport={'width': 1366, 'height': 768}, accept_downloads=True)
            page = context.new_page()
            results.update(browser_page_created=True, browser_observation_status='collecting',
                javascript_errors=[], console_errors=[], unexpected_http_errors=[], external_requests=[])

            def bridge(route):
                request = route.request
                if not request.url.startswith(ORIGIN + '/'):
                    results['external_requests'].append(request.url)
                    route.abort()
                    return
                response = send(dict(url=request.url, method=request.method, headers=request.headers,
                    body=base64.b64encode(request.post_data_buffer or b'').decode()))
                route.fulfill(status=response['status'], headers=response['headers'], body=base64.b64decode(response['body']))

            context.route('**/*', bridge)
            page.on('pageerror', lambda e: results['javascript_errors'].append(str(e)))
            page.on('console', lambda m: results['console_errors'].append(m.text) if m.type == 'error' else None)
            page.on('response', lambda r: results['unexpected_http_errors'].append([r.status, r.url]) if r.status >= 400 else None)

            class BrowserSession:
                def get(self, target):
                    response = page.goto(ORIGIN + target, wait_until='networkidle')
                    assert response.status == 200
                    return page.content()

                def post(self, target, values, expected=303):
                    if 'action' in values:
                        form = page.locator('form').filter(has=page.locator('[name="action"][value="' + values['action'] + '"]'))
                    elif '/piece-' in target:
                        form = page.locator('form[action="' + target + '"]')
                    else:
                        form = page.locator('form.payroll-form[method="post"]')
                    assert form.count() == 1, target
                    for name, value in values.items():
                        field = form.locator('[name="' + name + '"]')
                        if isinstance(value, list):
                            for item in value:
                                form.locator('[name="' + name + '"][value="' + item + '"]').check()
                        elif field.evaluate('(el) => el.tagName') == 'SELECT':
                            field.select_option(str(value))
                        elif field.get_attribute('type') == 'checkbox':
                            field.set_checked(value == '1')
                        elif field.get_attribute('type') == 'hidden':
                            field.evaluate('(el, value) => el.value = value', str(value))
                        else:
                            field.fill(str(value))
                    with page.expect_navigation(wait_until='networkidle') as response:
                        form.locator('button').first.click()
                    assert response.value.status == 200
                    return page.content()

                def screenshot(self, name):
                    target = ROOT / 'docs/screenshots' / ('stage44_' + name + '.png')
                    target.parent.mkdir(exist_ok=True)
                    page.screenshot(path=str(target), full_page=False, animations='disabled')
                    results['screenshots'].append(str(target.relative_to(ROOT)))

                def download(self, target):
                    # Use the actual rendered native download link.
                    if not page.locator('a[href="' + target + '"]').count():
                        self.get(target.split('/export.xlsx')[0] + '?' + target.split('?', 1)[1])
                    with page.expect_download() as event:
                        page.locator('a[href="' + target + '"]').click()
                    return Path(event.value.path()).read_bytes()

            accepted = scenarios(BrowserSession(), lambda kind, **p: send(dict(kind='inspect', inspection=kind, params=p)), fixture)
            assert not any(results[k] for k in ('javascript_errors', 'console_errors', 'unexpected_http_errors', 'external_requests'))
            results.update(status='passed', chromium_version=browser.version, browser_observation_status='completed',
                checks=accepted['checks'])
        finally:
            process.terminate()
            process.wait(timeout=5)
            browser.close()


def main():
    target = ROOT / 'docs/stage44/browser_results.json'
    refresh_backend = '--refresh-backend' in sys.argv
    evidence = (json.loads(target.read_text(encoding='utf-8')) if refresh_backend else dict(
        stage='4.4', captured_at=datetime.now(timezone.utc).isoformat(), status='pending',
        environment='Linux default execution sandbox', viewport=[1366, 768],
        transport='Chromium interception to real Flask test_client over subprocess stdio; TCP/WSGI transport not tested',
        launch_attempts=[], checks=[], screenshots=[], browser_page_created=False,
        javascript_errors=None, console_errors=None, unexpected_http_errors=None, external_requests=None,
        browser_observation_status='not_observed: no page created; null error/request fields mean unobserved',
        safari_tested=False, working_database_opened=False, synthetic_data_only=True))
    backend = subprocess.run([str(ROOT / '.venv/bin/python'), str(Path(__file__)), '--backend'],
        cwd=ROOT, text=True, capture_output=True, timeout=90)
    evidence['backend_acceptance'] = (json.loads(backend.stdout) if backend.returncode == 0
        else dict(status='failed', error=backend.stderr))
    if refresh_backend:
        evidence['backend_refreshed_at'] = datetime.now(timezone.utc).isoformat()
        evidence['status'] = 'blocked' if not evidence['browser_page_created'] else evidence['status']
    else:
        try:
            browser_acceptance(evidence)
        except Exception as error:
            evidence.update(status='failed', error=str(error))
    if evidence['backend_acceptance']['status'] != 'passed':
        evidence['status'] = 'failed'
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(status=evidence['status'], backend=evidence['backend_acceptance']['status'],
        evidence=str(target.relative_to(ROOT)), screenshots=evidence['screenshots'])))
    return 0 if evidence['status'] == 'passed' else 2


if __name__ == '__main__':
    if '--worker' in sys.argv:
        worker()
    elif '--backend' in sys.argv:
        print(json.dumps(backend_acceptance(), default=str, ensure_ascii=False))
    else:
        raise SystemExit(main())
