"""Optional local Chromium acceptance, not a pytest dependency.

Run with a Python containing Playwright and openpyxl, for example:
    python tests/browser_stage42.py
The application subprocess uses .venv/bin/python. All personnel are synthetic;
the working database is never opened. Generated evidence goes under docs/.
"""
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import connect, initialize
from app.branding import PRODUCT_NAME
from app.calculation_engine.historical import HistoricalCalculator
from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.event_repository import EventRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.timesheet_repository import TimesheetRepository
from app.repositories.work_repository import WorkRepository
from app.time_utils import instant


def seed(path):
    db = initialize(path)
    project = ProjectRepository(db).create_project('Школа', 'STAGE42-DEMO', buildings=[
        dict(code='УЧ', name='Учебный корпус'), dict(code='СП', name='Спортивный корпус')])
    people = PersonnelRepository(db, project)
    contractor = people.save_contractor(dict(name='ООО «Строймонтаж»'))
    specialty = people.save_specialty('Монтажник учебный', 'ST42-DEMO')
    brigades = BrigadeRepository(db, project)
    b1, b2 = brigades.save(contractor, '1'), brigades.save(contractor, '2')
    employees = [people.save_worker(dict(full_name='Учебный Работник {:02d}'.format(i + 1),
        contractor_id=contractor, specialty_id=specialty, effective_date='2026-01-01')) for i in range(15)]
    for eid in employees[:8]:
        brigades.assign(eid, b1, '2026-01-01')
    for eid in employees[8:13]:
        brigades.assign(eid, b2, '2026-01-01')
    work = WorkRepository(db, project)
    building = work.list_buildings()[0]['id']
    item = work.add_work_item(building, work.add_floor(building, '3', 3), 1)
    plan = work.create_plan_version('CONSTRUCTION', created_at='2026-01-01T00:00:00Z')
    work.add_plan_line(plan, item, '1000', '0.8')  # Explicit illustrative input.
    work.approve_plan_version(plan, approved_at='2026-01-01T01:00:00Z')
    events = EventRepository(db, project)
    events.add_fact(item, '600', '2026-10-20', recorded_at='2026-10-20T10:00:00Z', event_key='42-demo-fact')
    events.add_confirmation(item, '450', '2026-10-20', recorded_at='2026-10-20T11:00:00Z',
                            document_ref='Учебный акт', event_key='42-demo-confirmation')
    result = HistoricalCalculator(db, project).calculate_as_of('2026-10-31', '2026-11-01T00:00:00Z', plan)
    assert (result.total.actual_progress, result.total.confirmed_progress) == (Decimal('.6'), Decimal('.45'))
    assert db.execute('SELECT COUNT(*) FROM work_orders').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM timesheet_entries').fetchone()[0] == 0
    db.close()
    return dict(project=project, contractor=contractor, brigade=b1, employees=employees, plan=plan, item=item)


def launch(path, folder):
    log = open(folder / 'server.log', 'w+', encoding='utf-8')
    process = subprocess.Popen([str(ROOT / '.venv/bin/python'), '-u', 'main.py', '--db', str(path),
                                '--no-browser', '--port', '0'], cwd=ROOT, stdout=log, stderr=log)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        log.flush()
        content = (folder / 'server.log').read_text(encoding='utf-8')
        match = re.search(re.escape(PRODUCT_NAME) + r': (http://127\.0\.0\.1:\d+)', content)
        if match:
            return process, log, match.group(1)
        if process.poll() is not None:
            raise RuntimeError('Application stopped during browser startup: ' + content)
        time.sleep(.1)
    process.terminate()
    process.wait(timeout=5)
    raise RuntimeError('Local application did not become ready')


def report(path, context, sheet_id, version_id=None):
    db = connect(path)
    try:
        return TimesheetRepository(db, context['project']).report(sheet_id, version_id)
    finally:
        db.close()


def main():
    from openpyxl import Workbook, load_workbook
    from playwright.sync_api import sync_playwright, expect

    screenshots = ROOT / 'docs/screenshots'
    examples = ROOT / 'docs/examples'
    screenshots.mkdir(exist_ok=True)
    examples.mkdir(exist_ok=True)
    results = dict(stage='4.2', environment='Linux / Chromium', macos_safari_tested=False,
                   viewport=[1366, 768], narrow_viewport=[390, 844], checks=[],
                   javascript_errors=[], unexpected_http_errors=[], external_requests=[])

    with tempfile.TemporaryDirectory(prefix='construction-progress-stage42-browser-') as tmp:
        folder = Path(tmp)
        path = folder / 'демо табель.sqlite3'
        context = seed(path)
        process, log, origin = launch(path, folder)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
                    args=['--no-sandbox', '--disable-dev-shm-usage'])
                browser_context = browser.new_context(viewport={'width': 1366, 'height': 768}, accept_downloads=True)
                page = browser_context.new_page()
                page.on('pageerror', lambda e: results['javascript_errors'].append(str(e)))
                page.on('request', lambda r: results['external_requests'].append(r.url)
                        if urlsplit(r.url).scheme in ('http', 'https') and not r.url.startswith(origin + '/') else None)
                expected_errors = set()
                page.on('response', lambda r: results['unexpected_http_errors'].append([r.status, r.url])
                        if r.status >= 400 and r.url not in expected_errors else None)
                page.on('dialog', lambda d: d.accept())

                def goto(path):
                    response = page.goto(origin + path)
                    assert response and response.status == 200, (path, response.status if response else None)
                    page.wait_for_load_state('networkidle')

                def submit(form, button):
                    with page.expect_navigation(wait_until='networkidle'):
                        form.get_by_role('button', name=button, exact=True).click()
                    assert page.locator('h1').inner_text() != 'Не удалось выполнить операцию'

                def screenshot(name):
                    page.screenshot(path=str(screenshots / name), full_page=False, animations='disabled')

                goto('/')
                assert page.locator('.application-navigation [data-nav-group]').count() == 7
                selector = page.get_by_label('Текущий объект')
                selector.select_option(str(context['project']))
                submit(selector.locator('..').locator('..'), 'Выбрать')
                expect(page.locator('.topbar-project')).to_have_text('Школа')
                results['checks'].append('Seven navigation groups and global project switch')

                group = page.locator('details[data-nav-group="time"]')
                group.locator('summary').click()
                page.wait_for_timeout(400)
                page.reload(wait_until='networkidle')
                assert page.locator('details[data-nav-group="time"]').get_attribute('open') is not None
                page.locator('[data-navigation-toggle]').click()
                page.wait_for_timeout(400)
                page.reload(wait_until='networkidle')
                assert 'nav-collapsed' in page.locator('body').get_attribute('class')
                page.locator('[data-navigation-toggle]').click()
                page.wait_for_timeout(400)
                results['checks'].append('Menu expansion and sidebar collapse survive reload in session')

                goto('/timesheets/new')
                create = page.locator('form.ts-form')
                create.locator('[name="contractor_id"]').select_option(str(context['contractor']))
                create.locator('[name="year"]').fill('2026')
                create.locator('[name="month"]').select_option('10')
                create.locator('[name="comment"]').fill('Учебная приёмка этапа 4.2; все работники вымышлены')
                submit(create, 'Создать черновик')
                sheet_id = int(re.search(r'/timesheets/(\d+)', page.url).group(1))
                assert page.locator('#timesheet-matrix tbody tr[data-employee]').count() == 15
                assert report(path, context, sheet_id)['total_hours'] == 0
                results['checks'].append('October 2026 draft contains 15 workers and no attendance/hours')
                screenshot('stage42_timesheet_empty.png')

                filters = page.locator('form.ts-bulk-filters')
                filters.locator('[name="brigade_id"]').select_option(str(context['brigade']))
                filters.locator('[name="as_of"]').fill('2026-10-20')
                submit(filters, 'Показать состав')
                bulk = page.locator('form.ts-bulk-form')
                assert bulk.locator('[name="employee_id"]').count() == 8
                for eid in context['employees'][:7]:
                    bulk.locator('[name="employee_id"][value="{}"]'.format(eid)).check()
                bulk.locator('[name="start_date"]').fill('2026-10-20')
                bulk.locator('[name="end_date"]').fill('2026-10-20')
                bulk.locator('[name="code"]').select_option('Я')
                bulk.locator('[name="hours"]').fill('8')
                submit(bulk, 'Проверить изменения')
                assert report(path, context, sheet_id)['total_hours'] == 0
                assert page.locator('.notice.error').count() == 0
                screenshot('stage42_bulk_preview.png')
                preview_form = page.locator('.ts-preview-form')
                preview_form.locator('[name="confirm_changes"]').check()
                submit(preview_form, 'Применить изменения')
                assert report(path, context, sheet_id)['total_hours'] == 56
                assert not report(path, context, sheet_id)['workers'][7]['entries']
                results['checks'].append('Historical brigade roster: selected 7/8, preview writes no hours, confirmation writes 56h')

                def cell(eid, when, code, value):
                    page.locator('button.ts-cell[data-employee="{}"][data-date="{}"]'.format(eid, when)).click()
                    editor = page.locator('#cell-editor form')
                    expect(editor.locator('[name="employee_id"]')).to_have_value(str(eid))
                    expect(editor.locator('[name="work_date"]')).to_have_value(when)
                    editor.locator('[name="code"]').select_option(code)
                    editor.locator('[name="hours"]').fill(value)
                    submit(editor, 'Сохранить запись')

                cell(context['employees'][0], '2026-10-20', 'Я', '6')
                cell(context['employees'][7], '2026-10-20', 'Б', '0')
                assert report(path, context, sheet_id)['total_hours'] == 54
                worker8 = next(w for w in report(path, context, sheet_id)['workers'] if w['employee_id'] == context['employees'][7])
                assert worker8['absence_days'] == 1 and worker8['total_hours'] == 0
                results['checks'].append('Individual cell edit and sickness code: 54 worked hours, eighth worker has absence')
                page.locator('#timesheet-matrix').scroll_into_view_if_needed()
                screenshot('stage42_timesheet_filled.png')

                with page.expect_download() as download:
                    page.get_by_role('link', name='Экспорт Excel', exact=True).click()
                artifact = examples / 'stage42_Табель_октябрь_2026.xlsx'
                download.value.save_as(str(artifact))
                workbook = load_workbook(artifact, data_only=False)
                assert workbook.sheetnames == ['Месяц', 'Табель', 'Сведения']
                flat = workbook['Табель']
                entries = list(flat.iter_rows(min_row=2, values_only=True))
                assert len(entries) == 8 and sum(Decimal(str(r[4])) for r in entries if r[3] == 'Я') == 54
                assert workbook['Месяц'].freeze_panes == 'E3'
                assert not any(c.data_type == 'f' for sheet in workbook for row in sheet for c in row)
                workbook.close()
                results['checks'].append('Local XLSX download: 3 sheets, stable IDs, numeric 54h, frozen panes, no formulas')

                submit(page.locator('form').filter(has=page.locator('input[name="action"][value="submit"]')), 'Передать на проверку')
                assert report(path, context, sheet_id)['version']['status'] == 'submitted'
                submit(page.locator('form').filter(has=page.locator('input[name="action"][value="approve"]')), 'Утвердить табель')
                approved = report(path, context, sheet_id)
                original_version = approved['version']['id']
                assert approved['version']['status'] == 'approved' and approved['total_hours'] == 54
                assert page.locator('#cell-editor').count() == 0
                screenshot('stage42_timesheet_approved.png')
                results['checks'].append('Submit and approve; approved matrix has no editing controls')

                correction = page.locator('form').filter(has=page.locator('input[name="action"][value="correction"]'))
                correction.locator('[name="reason"]').fill('Учебное исправление продолжительности явки')
                submit(correction, 'Создать корректирующую версию')
                correction_version = report(path, context, sheet_id)['version']['id']
                assert correction_version != original_version
                cell(context['employees'][0], '2026-10-20', 'Я', '4')
                assert report(path, context, sheet_id)['total_hours'] == 52
                assert report(path, context, sheet_id, original_version)['total_hours'] == 54
                results['checks'].append('Correction V2 changes current total to 52h; approved V1 remains 54h')

                import_book = folder / 'проверка табеля.xlsx'
                workbook = Workbook()
                sheet = workbook.active
                sheet.title = 'Табель'
                sheet.append(['ID работника', 'ФИО', 'Дата', 'Код', 'Часы'])
                sheet.append([context['employees'][0], 'Учебный Работник 01', '2026-10-21', 'Я', 4.5])
                workbook.save(import_book)
                workbook.close()
                page.get_by_role('link', name='Импорт Excel', exact=True).click()
                form = page.locator('form[enctype="multipart/form-data"]')
                form.locator('[name="workbook"]').set_input_files(str(import_book))
                submit(form, 'Загрузить и проверить')
                preview_url = page.url
                assert report(path, context, sheet_id)['total_hours'] == 52
                assert page.locator('.notice.error').count() == 0
                screenshot('stage42_excel_preview.png')
                form = page.locator('.ts-preview-form')
                form.locator('[name="confirm_changes"]').check()
                repeated_payload = form.evaluate('(f) => Object.fromEntries(new FormData(f))')
                submit(form, 'Применить изменения')
                assert report(path, context, sheet_id)['total_hours'] == Decimal('56.5')
                response = browser_context.request.post(preview_url, form=repeated_payload, headers={'Origin': origin})
                assert response.status == 200
                assert report(path, context, sheet_id)['total_hours'] == Decimal('56.5')
                assert report(path, context, sheet_id, original_version)['total_hours'] == 54
                results['checks'].append('XLSX upload, persisted preview, explicit apply and repeated POST: exactly 4.5h once')

                goto('/timesheets/{}?version_id={}'.format(sheet_id, original_version))
                assert page.locator('#cell-editor').count() == 0
                assert page.locator('select[name="version_id"] option').count() == 2
                page.locator('#timesheet-history').evaluate('(e) => e.open = true')
                screenshot('stage42_timesheet_history.png')
                results['checks'].append('Original approved version remains selectable and audit history visible')

                goto('/timesheets/{}'.format(sheet_id))
                page.set_viewport_size({'width': 390, 'height': 844})
                page.wait_for_timeout(250)
                assert page.locator('[data-navigation-toggle]').is_visible()
                page.locator('[data-navigation-toggle]').click()
                assert 'nav-mobile-open' in page.locator('body').get_attribute('class')
                screenshot('stage42_navigation_mobile.png')
                page.keyboard.press('Escape')
                assert 'nav-mobile-open' not in page.locator('body').get_attribute('class')
                width = page.evaluate('({scroll: document.documentElement.scrollWidth, viewport: innerWidth})')
                assert width['scroll'] <= width['viewport'] + 1, width
                results['checks'].append('390px screen: compact sidebar, drawer toggle/Escape and contained horizontal table scrolling')

                page.set_viewport_size({'width': 1366, 'height': 768})
                selector = page.get_by_label('Текущий объект')
                selector.select_option('1')
                submit(selector.locator('..').locator('..'), 'Выбрать')
                assert page.locator('.topbar-project').inner_text() == 'Строительный проект'
                forbidden = origin + '/timesheets/' + str(sheet_id)
                expected_errors.add(forbidden)
                response = page.goto(forbidden)
                assert response.status == 400
                assert 'не принадлежит текущему объекту' in page.locator('main').inner_text()
                results['checks'].append('Project switch resets workspace; direct foreign timesheet URL is rejected')

                db = connect(path)
                try:
                    final = HistoricalCalculator(db, context['project']).calculate_as_of('2026-10-31', '2026-11-01T00:00:00Z', context['plan'])
                    assert (final.total.actual_progress, final.total.confirmed_progress) == (Decimal('.6'), Decimal('.45'))
                    assert db.execute('SELECT COUNT(*) FROM fact_events').fetchone()[0] == 1
                    assert db.execute('SELECT COUNT(*) FROM confirmation_events').fetchone()[0] == 1
                    assert not db.execute('PRAGMA foreign_key_check').fetchall()
                    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                finally:
                    db.close()
                results['checks'].append('Construction progress remains 60%/45%, independent manual fact/confirmation unchanged, integrity/FKs valid')
                assert not results['javascript_errors'], results['javascript_errors']
                assert not results['unexpected_http_errors'], results['unexpected_http_errors']
                assert not results['external_requests'], results['external_requests']
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
            log.close()
    results['completed_at'] = instant()
    results['passed'] = True
    result_path = ROOT / 'docs/stage42_browser_results.json'
    result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
