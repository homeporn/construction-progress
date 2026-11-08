"""Real Windows EXE smoke test, run by build.ps1 before creating a release ZIP."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from http.cookiejar import CookieJar
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from app.portable.instance import authenticated_probe
from app.portable.storage import read_only
from tools.windows.bundle import check_windows_exe


def seed_fixture(profile):
    """Synthetic data only, and only in a marked OS temporary profile."""
    profile = Path(profile).resolve()
    if Path(tempfile.gettempdir()).resolve() not in profile.parents or not (profile/'.windows-smoke-fixture').is_file():
        raise ValueError('Synthetic fixture requires a marked temporary profile')
    from app.database import initialize
    from app.repositories.work_repository import WorkRepository
    from app.repositories.event_repository import EventRepository
    from app.repositories.personnel_repository import PersonnelRepository
    from app.repositories.project_repository import ProjectRepository
    from app.repositories.timesheet_repository import TimesheetRepository
    from app.repositories.work_order_repository import WorkOrderRepository
    from app.calculation_engine.historical import HistoricalCalculator
    from app.time_utils import instant
    db = initialize(profile/'data/construction_progress.sqlite3')
    try:
        if db.execute('SELECT count(*) FROM work_items').fetchone()[0] or db.execute('SELECT count(*) FROM employees').fetchone()[0]:
            raise ValueError('Fixture must be empty')
        ProjectRepository(db).update_project(1, 'Школа — синтетический Windows smoke', 'W1-SYNTHETIC')
        work, people = WorkRepository(db), PersonnelRepository(db)
        floor = work.add_floor(1, '3', 3)
        item = work.add_work_item(1, floor, 1)
        plan = work.create_plan_version('CONSTRUCTION', created_at='2026-01-01T00:00:00Z')
        work.add_plan_line(plan, item, '1000', '0.8')
        work.approve_plan_version(plan, approved_at='2026-01-01T01:00:00Z')
        events = EventRepository(db)
        events.add_fact(item, '600', '2026-01-10', recorded_at='2026-01-10T12:00:00Z', event_key='w1-fact-no-order')
        events.add_confirmation(item, '450', '2026-01-10', recorded_at='2026-01-10T13:00:00Z', document_ref='Синтетический акт', event_key='w1-confirm-no-order')
        contractor = people.save_contractor(dict(name='Синтетический подрядчик W1'))
        specialty = people.save_specialty('Синтетическая специальность W1', 'W1-S')
        employee = people.save_worker(dict(full_name='Тестовый Работник W1', contractor_id=contractor, specialty_id=specialty, effective_date='2026-01-01'))
        orders = WorkOrderRepository(db)
        order = orders.save_draft(dict(shift_date=instant()[:10], shift_start='08:00', shift_end='17:00', contractor_id=contractor,
            issued_by_employee_id=employee, foreman_employee_id=employee),
            [dict(work_item_id=item, planned_quantity='120')], [dict(specialty_id=specialty, employee_ids=[employee])])
        orders.issue(order)
        timesheets = TimesheetRepository(db)
        sheet = timesheets.create(contractor, 2026, 10)
        timesheets.set_entries(sheet, [dict(employee_id=employee, work_date='2026-10-01', code='Я', hours='7.5', comment='Synthetic')])
        timesheets.transition(sheet, 'submit'); timesheets.transition(sheet, 'approve')
        progress = HistoricalCalculator(db).calculate_as_of('2026-10-31', instant(), plan)
        assert progress.total.actual_progress == Decimal('.6') and progress.total.confirmed_progress == Decimal('.45')
        assert timesheets.report(sheet)['total_hours'] == Decimal('7.5')
        assert db.execute('SELECT work_order_line_id FROM fact_events').fetchone()[0] is None
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        return dict(order=order, timesheet=sheet, actual='0.6', confirmed='0.45', hours='7.5')
    finally:
        db.close()


def http(opener, origin, path, data=None):
    request = Request(origin+path, data=urlencode(data).encode() if data is not None else None,
                      headers={'Origin': origin} if data is not None else {})
    started = time.perf_counter()
    with opener.open(request, timeout=60) as response:
        body = response.read()
        assert response.status == 200
    return body, dict(seconds=time.perf_counter()-started, bytes=len(body), status=200)


def exercise(exe, profile, routes, *, repeat_launch=False):
    args = [str(exe), '--data-dir', str(profile), '--no-browser', '--no-pause']
    started = time.perf_counter()
    output = tempfile.TemporaryFile()
    process = subprocess.Popen(args, stdout=output, stderr=output,
                               creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        deadline = time.monotonic()+90
        while time.monotonic() < deadline:
            if process.poll() is not None: raise RuntimeError('EXE exited before ready')
            try:
                record = json.loads((profile/'.instance.json').read_text(encoding='utf-8'))
                if authenticated_probe(record['origin'],record['token']): break
            except (OSError,ValueError,KeyError): pass
            time.sleep(.1)
        else: raise RuntimeError('EXE readiness timed out')
        measurements = {'startup_seconds':time.perf_counter()-started, 'routes':{}}
        opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        for route in routes:
            body, measurement = http(opener, record['origin'], route)
            if route.endswith('.xlsx'): assert body[:2] == b'PK'
            measurements['routes'][route] = measurement
        if repeat_launch:
            duplicate = subprocess.run(args, capture_output=True, timeout=20)
            assert duplicate.returncode == 0 and process.poll() is None
            assert json.loads((profile/'.instance.json').read_text(encoding='utf-8'))['pid'] == process.pid
            measurements['repeat_launch_single_server'] = True
        page, _ = http(opener, record['origin'], '/')
        token = re.search(rb'name="csrf_token" value="([^"]+)"',page).group(1).decode()
        http(opener, record['origin'], '/shutdown', {'csrf_token':token})
        assert process.wait(timeout=30) == 0
        measurements['clean_exit'] = True
        return measurements
    finally:
        if process.poll() is None:
            process.terminate(); process.wait(timeout=10)  # Only this private failed-test process.
        output.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--folder',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    if os.name!='nt' or platform.machine().lower() not in ('amd64','x86_64'):
        parser.error('This smoke test requires real Windows x64. Linux is not Windows acceptance.')
    exe=args.folder.resolve()/'ConstructionProgress.exe'
    check_windows_exe(exe)
    with tempfile.TemporaryDirectory(prefix='cp-w1-windows-') as temporary:
        profile=Path(temporary)/'Test User Тест Документы Construction Progress'/('Длинный профиль '+ 'a'*64)
        profile.mkdir(parents=True)
        (profile/'.windows-smoke-fixture').touch()
        first=exercise(exe,profile,['/','/projects','/timesheets','/static/css/app.css','/personnel/import/template.xlsx'],repeat_launch=True)
        fixture=seed_fixture(profile)
        again=exercise(exe,profile,['/','/volumes','/facts','/confirmations','/analytics',
            '/analytics/export.xlsx','/work-orders/{}/print'.format(fixture['order']),
            '/timesheets/{}'.format(fixture['timesheet']), '/timesheets/{}/export.xlsx'.format(fixture['timesheet'])])
        database=profile/'data/construction_progress.sqlite3'
        before=hashlib.sha256(database.read_bytes()).hexdigest()
        diagnostic=subprocess.run([str(exe),'--data-dir',str(profile),'--diagnose','--no-pause'],capture_output=True,timeout=30)
        assert diagnostic.returncode==0
        diag=json.loads(diagnostic.stdout.decode('utf-8'))
        assert before==hashlib.sha256(database.read_bytes()).hexdigest()
        backup=subprocess.run([str(exe),'--data-dir',str(profile),'--backup','--no-pause'],capture_output=True,timeout=30)
        assert backup.returncode==0 and list((profile/'backups').glob('*.sqlite3'))
        with closing(read_only(database)) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        report=dict(status='native_exe_smoke_passed_manual_browser_acceptance_pending',
            built_at=datetime.now(timezone.utc).isoformat(),windows=platform.platform(),architecture=platform.machine(),
            build_python=platform.python_version(),pyinstaller=version('pyinstaller'),waitress=version('waitress'),
            native_runtime_python=diag['python'],native_sqlite=diag['sqlite'],exe_bytes=exe.stat().st_size,
            exe_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),first_run=first,restart=again,
            fixture=fixture,manual_edge_chrome_and_offline_test='not performed by this script')
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__': main()
