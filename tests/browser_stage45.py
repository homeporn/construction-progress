"""Stage 4.5 disposable acceptance; browser and Flask evidence stay separate.

Run with the Python providing Playwright: ``python tests/browser_stage45.py``.
Exactly one default-environment Chromium launch is allowed. Subsequent backend
refreshes use ``--refresh-backend`` and preserve that browser observation.
No sockets, external services, working databases or authentication bypasses.
"""
import base64
from datetime import date, datetime, timezone
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
NS = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def token(html, name='csrf_token'):
    match = re.search(r'name="' + re.escape(name) + r'" value="([^"]*)"', html)
    assert match, name
    return match.group(1)


def application(folder):
    from app.database import initialize
    from app.repositories.brigade_repository import BrigadeRepository
    from app.repositories.event_repository import EventRepository
    from app.repositories.personnel_repository import PersonnelRepository
    from app.repositories.work_order_repository import WorkOrderRepository
    from app.repositories.work_repository import WorkRepository
    from app.time_utils import instant
    from app.web import create_app
    path = folder / 'synthetic-stage45.sqlite3'
    db = initialize(path)
    people, work, orders = PersonnelRepository(db), WorkRepository(db), WorkOrderRepository(db)
    contractor = people.save_contractor(dict(name='Учебный подрядчик 45 · синтетические данные'))
    specialty = people.save_specialty('Учебная специальность 45', 'BROWSER45')
    issuer = people.save_employee(dict(full_name='Учебный Прораб 45', position='Прораб'))
    foreman = people.save_employee(dict(full_name='Учебный Бригадир 45', contractor_id=contractor, position='Бригадир'))
    employee = people.save_employee(dict(full_name='Учебный Исполнитель 45', contractor_id=contractor, specialty_id=specialty))
    today = instant()[:10]
    brigades = BrigadeRepository(db)
    brigade = brigades.save(contractor, '45')
    brigades.assign(employee, brigade, today)
    header = dict(shift_date=today, shift_start='08:00', shift_end='17:00', contractor_id=contractor,
        issued_by_employee_id=issuer, foreman_employee_id=foreman, notes='Только синтетическая приёмка 45')
    crew = [dict(specialty_id=specialty, planned_count=1, employee_ids=[employee])]
    floor = work.add_floor(1, '45', 45)
    items = {name: work.add_work_item(1, floor, catalog) for name, catalog in zip('ABCDEF', [1, 2, 3, 4, 5, 6])}
    plan = work.create_plan_version('CONSTRUCTION', created_at='2026-01-01T00:00:00Z')
    for item in items.values():
        work.add_plan_line(plan, item, '1000', '0.5')
    work.approve_plan_version(plan, approved_at='2026-01-01T01:00:00Z')
    fixture = dict(header=header, crew=crew, brigade=brigade, contractor=contractor, issuer=issuer,
        foreman=foreman, employee=employee, specialty=specialty, date=today, plan=plan, floor=floor, cases={})
    events = EventRepository(db)
    for name, quantity, completed in [('A','120','100'), ('B','40','0'), ('C','120','100'), ('D','20','0'), ('E','120','100')]:
        order = orders.save_draft(header, [dict(work_item_id=items[name], planned_quantity=quantity)], crew, brigade_ids=[brigade])
        orders.issue(order)
        line = orders.get_order(order)['lines'][0]['id']
        fixture['cases'][name] = dict(order=order, line=line, item=items[name])
        if completed != '0':
            events.add_fact(items[name], completed, today, event_key='stage45-explicit-setup-fact-'+name, work_order_line_id=line)
            events.add_confirmation(items[name], completed, today, event_key='stage45-explicit-setup-confirmation-'+name,
                document_ref='Учебный акт 45 '+name, work_order_line_id=line)
    fixture['cases']['F'] = dict(item=items['F'])
    db.close()
    app = create_app(dict(DATABASE=str(path), TESTING=True, LOCAL_ORIGIN=ORIGIN, SECRET_KEY='isolated-stage45-synthetic-only'))
    return app, app.test_client(), fixture, path


def inspect_fixture(path, kind, **params):
    from app.database import connect
    from app.repositories.work_order_repository import WorkOrderRepository
    from app.repositories.work_order_control_repository import WorkOrderControlRepository
    from app.services.work_order_control import WorkOrderControlProjection
    db = connect(path)
    try:
        if kind == 'order':
            return WorkOrderRepository(db).get_order(params['order'])
        if kind == 'chain':
            return WorkOrderControlProjection(db).report(params['order'])
        if kind == 'analytics':
            return WorkOrderControlRepository(db).analytics(params.get('filters'), params.get('report_date'), params.get('cutoff'))
        if kind == 'transfers':
            return [dict(r) for r in db.execute('SELECT t.*,l.source_line_id,l.target_line_id,l.quantity FROM work_order_transfers t JOIN work_order_transfer_lines l ON l.transfer_id=t.id ORDER BY t.id')]
        if kind == 'sources':
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name IN ('fact_events','confirmation_events') OR name LIKE 'timesheet%' OR name LIKE 'labor_%' OR name LIKE 'payroll_%') ORDER BY name")]
            data = {name: sorted([list(row) for row in db.execute('SELECT * FROM '+name)], key=repr) for name in names}
            return dict(sha256=hashlib.sha256(json.dumps(data, default=str, sort_keys=True).encode()).hexdigest(),
                rows={name: len(rows) for name, rows in data.items()}, integrity=db.execute('PRAGMA integrity_check').fetchone()[0])
        if kind == 'unlinked':
            return {table: [dict(r) for r in db.execute('SELECT id,quantity_delta,work_order_line_id FROM '+table+' WHERE work_item_id=?', (params['item'],))] for table in ('fact_events','confirmation_events')}
        raise AssertionError(kind)
    finally:
        db.close()


class RouteSession:
    def __init__(self, client):
        self.client, self.html = client, ''

    def get(self, target):
        response = self.client.get(target, base_url=ORIGIN)
        assert response.status_code == 200, (target, response.status_code, response.text)
        self.html = response.text
        return self.html

    def post(self, target, values, expected=303):
        from werkzeug.datastructures import MultiDict
        payload = MultiDict(values)
        payload.setdefault('csrf_token', token(self.html))
        payload.setdefault('workspace_id', '1')
        response = self.client.post(target, base_url=ORIGIN, headers={'Origin': ORIGIN}, data=payload)
        assert response.status_code == expected, (target, response.status_code, response.text)
        self.html = response.text
        if expected == 303:
            self.get(response.headers['Location'])
        return response

    def download(self, target):
        response = self.client.get(target, base_url=ORIGIN)
        assert response.status_code == 200 and 'attachment' in response.headers['Content-Disposition']
        return response.data


def transfer_values(fixture, source, quantity, reason='Учебный явный перенос 45'):
    # Selected named roles and crew are ordinary domain choices, not login state.
    values = dict(fixture['header'], operation_key=uuid4().hex, reason=reason,
        source_line_id=str(source['line']), brigade_id=str(fixture['brigade']), crew_row='0',
        crew_specialty_0=str(fixture['specialty']), crew_count_0='1', crew_employee_0=str(fixture['employee']))
    values['quantity_'+str(source['line'])] = quantity
    return values


def assert_workbook(blob, data):
    from app.services.work_order_control_xlsx import SHEETS
    numeric_cells = 0
    with ZipFile(io.BytesIO(blob)) as archive:
        assert archive.testzip() is None
        wb = ET.fromstring(archive.read('xl/workbook.xml'))
        names = [r.get('name') for r in wb.findall('x:sheets/x:sheet', NS)]
        assert names == list(SHEETS)
        parsed = []
        for index in range(1, 8):
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet%d.xml' % index))
            assert not sheet.findall('.//x:f', NS)
            assert sheet.find('x:autoFilter', NS) is not None
            rows = sheet.findall('x:sheetData/x:row', NS)
            headers = {re.sub(r'\d+$', '', c.get('r')): ''.join(c.itertext()) for c in rows[0]}
            records = []
            for row in rows[1:]:
                record = {}
                for c in row:
                    header = headers[re.sub(r'\d+$', '', c.get('r'))]
                    raw = c.find('x:v', NS)
                    value = Decimal(raw.text) if c.get('t') == 'n' else ''.join(c.itertext())
                    record[header] = value
                    if c.get('t') == 'n': numeric_cells += 1
                    if header == 'Дата среза':
                        assert c.get('t') == 'n' and c.get('s') == '2'
                        assert value == (date.fromisoformat(data['report_date']) - date(1899,12,30)).days
                    if header in ('Отсечение знаний UTC', 'Записан UTC') and raw is not None:
                        assert c.get('t') == 'n' and c.get('s') == '5'
                records.append(record)
            parsed.append(records)
        quantity_headers = ('Задание','Факт по наряду','Подтверждение по наряду','Остаток выполнения','Остаток подтверждения','Перенесено','Резерв в черновиках','Выдано получателю','Списано','Доступно к переносу','Ожидает приёмки','Открыто','Незакрыто')
        quantity_keys = ('assignment','actual','confirmed','execution_remaining','confirmed_remaining','transferred','reserved','sent','written_off','available','pending_acceptance','open_quantity','unresolved')
        registry = {(int(r['Наряд ID']),r.get('Код'),r.get('Ед.')):r for r in parsed[0]}
        for order in data['registry']:
            for volume in order['volumes']:
                actual = registry[(order['id'],volume['code'],volume['unit'])]
                for key,label in zip(quantity_keys,quantity_headers):
                    if volume.get(key) is not None:
                        assert actual[label] == Decimal(str(volume[key])), (order['id'],key)
        for index, expected in [(1,data['rows']), (2,[r for r in data['rows'] if r['status']!='cancelled' and Decimal(r['unresolved'])>0]), (5,data['discrepancies'])]:
            actual = {int(r['Строка ID']): r for r in parsed[index] if 'Строка ID' in r}
            assert set(actual) == {r['line_id'] for r in expected}
            for row in expected:
                for key, label in zip(quantity_keys, quantity_headers):
                    value = row.get(key)
                    if value is not None:
                        assert actual[row['line_id']][label] == Decimal(str(value)), (row['line_id'], key)
        chains = {int(r['Корневой наряд ID']): r for r in parsed[4] if 'Корневой наряд ID' in r}
        assert len(chains) == len(data['chain_rows'])
        for row in data['chain_rows']:
            for key, label in [('original_quantity','Первоначально выдано'),('confirmed','Подтверждено'),('open_quantity','Открыто'),('written_off','Списано')]:
                assert chains[row['root_order_id']][label] == Decimal(str(row[key]))
        assert any(r.get('Основание') == '=SUM(1,2)' for r in parsed[3])
        assert len(parsed[3]) == len(data['transfers'])
        for actual,expected in zip(parsed[3],data['transfers']):
            assert actual['Количество'] == Decimal(str(expected['quantity']))
            assert actual['Исходная строка ID'] == expected['source_line_id']
            assert actual['Новая строка ID'] == expected['target_line_id']
        assert len(parsed[6]) == len(data['source_rows'])
        for actual,expected in zip(parsed[6],data['source_rows']):
            assert actual['Количество (изменение)'] == Decimal(str(expected['quantity']))
            assert actual['Событие ID'] == expected['id']
        assert numeric_cells > 0
    return dict(sheets=names, numeric_cells_checked=numeric_cells, line_quantity_parity=True,
        registry_quantity_parity=True, transfer_quantity_parity=True, source_quantity_parity=True,
        chain_quantity_parity=True, literal_formula_text=True, numeric_dates_and_utc=True, no_formulas=True)


def scenarios(session, inspect, fixture):
    cases, checks, outcomes, invariants = fixture['cases'], [], {}, []
    def state(source):
        return inspect('order', order=source['order'])
    def line(source):
        return state(source)['lines'][0]
    def screen_parity(source):
        html = session.get('/work-orders/%d' % source['order'])
        row = line(source)
        for attribute,key in [('data-assignment','assignment'),('data-linked-actual','actual'),('data-linked-confirmed','confirmed'),('data-available','available')]:
            match = re.search('<td '+attribute+r'>([^<]*)',html)
            assert match,attribute
            rendered=match.group(1).replace('\u00a0','').replace('\u202f','').replace(' ','').replace(',','.')
            assert Decimal(rendered)==Decimal(row[key]),(attribute,rendered,row[key])
    def move(source, amount, expected=303, values=None):
        session.get('/work-orders/%d/transfer' % source['order'])
        before = inspect('sources')
        response = session.post('/work-orders/%d/transfer' % source['order'], values or transfer_values(fixture, source, amount), expected)
        after = inspect('sources')
        assert before == after and after['integrity']=='ok'
        invariants.append(dict(operation='transfer', status=response.status_code, primary_time_labor_payroll_unchanged=True))
        if expected != 303: return None
        row = inspect('transfers')[-1]
        return dict(order=row['target_order_id'], line=row['target_line_id'], item=source['item'], transfer=row['id'])
    def chain(source, original, confirmed, opened):
        total = inspect('chain', order=source['order'])['chain_totals'][0]
        assert [Decimal(total[k]) for k in ('original_quantity','confirmed','open_quantity')] == list(map(Decimal,[original,confirmed,opened]))
        return {k: total[k] for k in ('original_quantity','confirmed','open_quantity')}
    a = move(cases['A'], '20', values=transfer_values(fixture,cases['A'],'20','=SUM(1,2)'))
    assert state(a)['status']=='draft' and Decimal(line(cases['A'])['available'])==0
    outcomes['A'] = dict(target_status='draft', source_available='0', **chain(cases['A'],'120','100','20'))
    checks.append('A: 120 assigned, 100 fact/confirmed; transfer 20 reserves a draft and preserves original chain 120')
    b1 = move(cases['B'],'25'); assert Decimal(line(cases['B'])['available'])==15
    b2 = move(cases['B'],'15'); assert Decimal(line(cases['B'])['available'])==0
    outcomes['B'] = dict(transferred=['25','15'], source_available='0', **chain(cases['B'],'40','0','40'))
    checks.append('B: 40 splits into 25 and 15 with no source availability')
    c = move(cases['C'],'20')
    session.post('/work-orders/%d/issue' % c['order'], {})
    for kind in ('fact','confirmation'):
        url = '/work-orders/%d/lines/%d/events/%s' % (c['order'],c['line'],kind)
        session.get(url)
        session.post(url, dict(work_item_id=c['item'],work_order_line_id=c['line'],building_id=1,floor_id=fixture['floor'],
            discipline='CONSTRUCTION',version_id=fixture['plan'],quantity_delta='15',business_date=fixture['date'],
            event_key=uuid4().hex,document_ref='Учебный акт следующей смены',comment='Явное событие приёмки C'))
    c2 = move(c,'5')
    outcomes['C'] = dict(transfers=['20','5'], **chain(cases['C'],'120','115','5'))
    assert '/work-orders/%d' % c2['order'] in session.get('/work-orders/%d' % cases['C']['order'])
    checks.append('C: chain 120 → 20 → 5 keeps original 120, confirmed 115, open 5; recipient fact and confirmation are explicit separate posts')
    # Two form snapshots model a controlled stale-tab conflict. This is not a
    # simultaneous race; the repository suite owns real concurrent writers.
    stale1, stale2 = transfer_values(fixture,cases['D'],'15'), transfer_values(fixture,cases['D'],'15')
    move(cases['D'],'15',values=stale1); move(cases['D'],'15',expected=400,values=stale2)
    assert Decimal(line(cases['D'])['available'])==5
    outcomes['D'] = dict(kind='controlled sequential stale-form conflict; not a simultaneous browser race', http_statuses=[303,400], transferred='15', source_available='5')
    checks.append('D: two controlled 15-from-20 submissions save one and reject one; source availability remains 5')
    e = move(cases['E'],'20')
    before = inspect('sources')
    session.post('/work-orders/transfers/%d/cancel' % e['transfer'], dict(reason='Учебная отмена переноса 45',operation_key=uuid4().hex))
    assert inspect('sources')==before
    assert Decimal(line(cases['E'])['available'])==20 and state(e)['status']=='cancelled'
    history = inspect('chain',order=cases['E']['order'])['timeline']
    assert any(r['kind']=='transfer_cancelled' for r in history)
    assert 'Учебная отмена переноса 45' in session.get('/work-orders/%d' % cases['E']['order'])
    outcomes['E'] = dict(source_available='20', target_status='cancelled', cancellation_history_retained=True)
    checks.append('E: cancel draft transfer 20 restores availability 20 and retains original transfer plus cancellation')
    for kind,url,amount in [('fact','/facts','12'),('confirmation','/confirmations','7')]:
        session.get(url+'?work_item_id='+str(cases['F']['item']))
        session.post(url,dict(work_item_id=cases['F']['item'],work_order_line_id='',building_id=1,floor_id=fixture['floor'],
            discipline='CONSTRUCTION',version_id=fixture['plan'],quantity_delta=amount,business_date=fixture['date'],
            event_key=uuid4().hex,document_ref='Учебный акт без наряда',comment='Явное событие приёмки F'))
    unlinked = inspect('unlinked',item=cases['F']['item'])
    assert all(len(rows)==1 and rows[0]['work_order_line_id'] is None for rows in unlinked.values())
    outcomes['F'] = dict(unlinked_fact='12', unlinked_confirmation='7', order_required=False)
    checks.append('F: ordinary real fact and confirmation form posts succeed without a work order')
    for source in [cases[k] for k in 'ABCDE']+[a,b1,b2,c,c2,e]:
        screen_parity(source)
    checks.append('Rendered Flask card assignment, linked fact/confirmation and availability equal service values on all 11 source/recipient cards')
    session.get('/work-orders?has_transfer=1&contractor_id='+str(fixture['contractor']))
    assert 'name="has_transfer"' in session.html
    session.get('/work-orders/control')
    report = inspect('analytics')
    # Pin the same cutoff and report date in both UI and Excel requests.
    from urllib.parse import urlencode
    query = urlencode(dict(report_date=report['report_date'],knowledge_cutoff=report['cutoff']))
    session.get('/work-orders/control?'+query)
    blob = session.download('/work-orders/control/export.xlsx?'+query)
    workbook = assert_workbook(blob,report)
    target = ROOT/'docs/examples/stage45_work_order_control.xlsx'
    target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(blob)
    checks.append('Seven-sheet XLSX has exact service line/chain parity, numeric dates and UTC, literal = text, filters and no formulas')
    sources = inspect('sources')
    assert sources['integrity']=='ok'
    return dict(status='passed',checks=checks,cases=outcomes,transfer_invariants=invariants,xlsx=workbook,
        final_synthetic_source_rows=sources['rows'],working_database_opened=False,synthetic_data_only=True,
        export='docs/examples/stage45_work_order_control.xlsx', crew_and_responsible_roles_selected=True)


def backend_acceptance():
    with tempfile.TemporaryDirectory(prefix='construction-progress-stage45-route-') as tmp:
        app, client, fixture, path = application(Path(tmp))
        inspect = lambda kind, **params: json.loads(json.dumps(inspect_fixture(path,kind,**params),default=str))
        session = RouteSession(client)
        result = scenarios(session,inspect,fixture)
        session.get('/work-orders/%d/transfer' % fixture['cases']['B']['order'])
        values = transfer_values(fixture,fixture['cases']['B'],'1')
        response = client.post('/work-orders/%d/transfer' % fixture['cases']['B']['order'],base_url=ORIGIN,headers={'Origin':ORIGIN},data=values)
        assert response.status_code==403
        values['csrf_token']=token(session.html)
        response = client.post('/work-orders/%d/transfer' % fixture['cases']['B']['order'],base_url=ORIGIN,headers={'Origin':'https://example.invalid'},data=values)
        assert response.status_code==403
        result.update(transport='real Flask test_client with rendered CSRF forms; no browser',expected_http_errors=[400,403],
            local_guard_checks=['missing CSRF rejected 403','foreign Origin rejected 403'],
            concurrent_race_scope='D is controlled sequential route conflict, not a simultaneous race; concurrency belongs to repository tests')
        return result


def worker():
    with tempfile.TemporaryDirectory(prefix='construction-progress-stage45-browser-') as tmp:
        app,client,fixture,path=application(Path(tmp))
        print(json.dumps(dict(fixture=fixture)),flush=True)
        for line in sys.stdin:
            message=json.loads(line)
            if message.get('kind')=='inspect':
                result=inspect_fixture(path,message['inspection'],**message.get('params',{}))
            else:
                split=urlsplit(message['url'])
                headers={k:v for k,v in message['headers'].items() if k.lower() not in ('host','content-length','cookie')}
                response=client.open(split.path+('?' + split.query if split.query else ''),method=message['method'],base_url=ORIGIN,
                    headers=headers,data=base64.b64decode(message.get('body','')),environ_overrides={'REMOTE_ADDR':'127.0.0.1'})
                result=dict(status=response.status_code,headers=dict(response.headers),body=base64.b64encode(response.data).decode())
            print(json.dumps(result,default=str,ensure_ascii=False),flush=True)


def browser_acceptance(results):
    from playwright.sync_api import sync_playwright
    args=['--no-sandbox','--disable-dev-shm-usage']
    with sync_playwright() as pw:
        try:
            browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=args,timeout=10000)
        except Exception as error:
            results['launch_attempts'].append(dict(args=args,error=str(error)))
            results.update(status='blocked',blocker='Chromium failed before page creation in the default execution sandbox. Exact stderr is retained; browser interactions, JavaScript behavior and screenshots were not observed. No escalation or alternate launch attempted.')
            return
        results['launch_attempts'].append(dict(args=args,status='launched'))
        process=subprocess.Popen([str(ROOT/'.venv/bin/python'),str(Path(__file__)),'--worker'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=ROOT)
        try:
            fixture=json.loads(process.stdout.readline())['fixture']
            def send(message):
                process.stdin.write(json.dumps(message)+'\n');process.stdin.flush()
                return json.loads(process.stdout.readline())
            context=browser.new_context(viewport={'width':1366,'height':768},accept_downloads=True)
            page=context.new_page()
            results.update(browser_page_created=True,browser_observation_status='collecting',javascript_errors=[],console_errors=[],unexpected_http_errors=[],external_requests=[])
            def bridge(route):
                request=route.request
                if not request.url.startswith(ORIGIN+'/'):
                    results['external_requests'].append(request.url);route.abort();return
                response=send(dict(url=request.url,method=request.method,headers=request.all_headers(),body=base64.b64encode(request.post_data_buffer or b'').decode()))
                route.fulfill(status=response['status'],headers=response['headers'],body=base64.b64decode(response['body']))
            context.route('**/*',bridge)
            page.on('pageerror',lambda e:results['javascript_errors'].append(str(e)))
            page.on('console',lambda m:results['console_errors'].append(m.text) if m.type=='error' else None)
            page.on('response',lambda r:results['unexpected_http_errors'].append([r.status,r.url]) if r.status>=400 else None)
            a=fixture['cases']['A']
            page.goto(ORIGIN+'/work-orders/%d/transfer'%a['order'],wait_until='networkidle')
            page.locator('[name="source_line_id"][value="%d"]'%a['line']).check()
            page.locator('[name="quantity_%d"]'%a['line']).fill('20')
            for name,value in [('contractor_id',fixture['contractor']),('issued_by_employee_id',fixture['issuer']),('foreman_employee_id',fixture['foreman']),('brigade_id',fixture['brigade'])]:
                page.locator('[name="'+name+'"]').select_option(str(value))
            page.locator('#add-transfer-crew').click()
            page.locator('[name="crew_specialty_0"]').select_option(str(fixture['specialty']))
            page.locator('[name="crew_count_0"]').fill('1')
            page.locator('[name="crew_employee_0"]').select_option(str(fixture['employee']))
            page.locator('[name="reason"]').fill('Учебная браузерная приёмка A')
            with page.expect_navigation(wait_until='networkidle'):
                page.get_by_role('button',name='Создать черновик и зарезервировать остаток',exact=True).click()
            order=send(dict(kind='inspect',inspection='order',params=dict(order=a['order'])))
            assert Decimal(order['lines'][0]['available'])==0
            for name,target in [('transfer_draft',page.url),('source',ORIGIN+'/work-orders/%d'%a['order']),('control',ORIGIN+'/work-orders/control')]:
                page.goto(target,wait_until='networkidle')
                screenshot=ROOT/'docs/screenshots'/('stage45_'+name+'.png')
                screenshot.parent.mkdir(exist_ok=True);page.screenshot(path=str(screenshot),animations='disabled')
                results['screenshots'].append(str(screenshot.relative_to(ROOT)))
            assert not any(results[k] for k in ('javascript_errors','console_errors','unexpected_http_errors','external_requests'))
            results.update(status='passed',browser_observation_status='completed',chromium_version=browser.version,
                checks=['Real UI A transfer with explicit roles, brigade and named crew; source availability 0; draft/source/control pages'],
                scope='Browser A and navigation only; A–F acceptance is separate Flask evidence')
        finally:
            process.terminate();process.wait(timeout=5);browser.close()


def main():
    target=ROOT/'docs/stage45/browser_results.json'
    refresh='--refresh-backend' in sys.argv
    evidence=json.loads(target.read_text()) if refresh else dict(stage='4.5',captured_at=datetime.now(timezone.utc).isoformat(),status='pending',
        environment='Linux default execution sandbox',viewport=[1366,768],transport='Chromium interception to real Flask test_client over subprocess stdio; native TCP/WSGI not tested',
        launch_attempts=[],checks=[],screenshots=[],browser_page_created=False,javascript_errors=None,console_errors=None,unexpected_http_errors=None,external_requests=None,
        browser_observation_status='not_observed: no page created; null error/request fields mean unobserved',safari_tested=False,working_database_opened=False,synthetic_data_only=True)
    backend=subprocess.run([str(ROOT/'.venv/bin/python'),str(Path(__file__)),'--backend'],cwd=ROOT,text=True,capture_output=True,timeout=90)
    evidence['backend_acceptance']=json.loads(backend.stdout) if backend.returncode==0 else dict(status='failed',error=backend.stderr)
    if refresh:
        evidence['backend_refreshed_at']=datetime.now(timezone.utc).isoformat()
    else:
        try:browser_acceptance(evidence)
        except Exception as error:evidence.update(status='failed',error=str(error))
    if evidence['backend_acceptance']['status']!='passed':evidence['status']='failed'
    elif not evidence['browser_page_created']:evidence['status']='blocked'
    target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(status=evidence['status'],backend=evidence['backend_acceptance']['status'],evidence=str(target.relative_to(ROOT)),screenshots=evidence['screenshots'])))
    return 0 if evidence['status']=='passed' else 2


if __name__=='__main__':
    if '--worker' in sys.argv:worker()
    elif '--backend' in sys.argv:print(json.dumps(backend_acceptance(),default=str,ensure_ascii=False))
    else:raise SystemExit(main())
