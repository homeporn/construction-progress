"""Optional real Chromium smoke; synthetic temporary profile, no working DB.

Run with a Python providing Playwright: python tests/browser_rebranding.py.
Uses the repository venv for the real server. A blocked browser is reported as
blocked, never replaced by Flask test_client or a rendered screenshot mock.
"""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.branding import PRODUCT_NAME, PRODUCT_FAMILY_NAME


def main():
    from playwright.sync_api import sync_playwright
    evidence = ROOT/'docs/rebranding'
    evidence.mkdir(exist_ok=True)
    result = dict(status='pending', viewport=[1366,768], engine='Chromium',
                  platform=sys.platform, checks=[], js_errors=[], http_errors=[],
                  external_requests=[], screenshots=[], flask_client_substitution=False)
    process = browser = None
    try:
        with sync_playwright() as playwright:
            result['phase'] = 'browser_launch'
            browser = playwright.chromium.launch(executable_path='/usr/bin/chromium',headless=True)
            with tempfile.TemporaryDirectory(prefix='plankton-rebrand-browser-') as temporary:
                profile = Path(temporary)
                (profile/'.windows-smoke-fixture').touch()
                python = ROOT/('.venv/Scripts/python.exe' if sys.platform=='win32' else '.venv/bin/python')
                fixture = subprocess.check_output([str(python),'-c',
                    'import json,sys; from tools.windows.smoke import seed_fixture; print(json.dumps(seed_fixture(sys.argv[1])))',
                    str(profile)],cwd=ROOT,text=True,encoding='utf-8',timeout=30)
                ids=json.loads(fixture)
                result['phase'] = 'server_launch'
                with (profile/'server.log').open('w+',encoding='utf-8') as log:
                    process=subprocess.Popen([str(python),'-u','main.py','--db',
                        str(profile/'data/construction_progress.sqlite3'),'--no-browser','--port','0'],
                        cwd=ROOT,stdout=log,stderr=log)
                    deadline=time.monotonic()+20
                    while time.monotonic()<deadline:
                        content=(profile/'server.log').read_text(encoding='utf-8')
                        ready=re.search(re.escape(PRODUCT_NAME)+r': (http://127\.0\.0\.1:\d+)',content)
                        if ready: break
                        if process.poll() is not None: raise RuntimeError('Server startup: '+content)
                        time.sleep(.1)
                    else: raise RuntimeError('Server readiness timed out')
                    origin=ready.group(1)
                    result['phase']='browser_checks'
                    context=browser.new_context(viewport=dict(width=1366,height=768))
                    page=context.new_page()
                    page.on('pageerror',lambda error:result['js_errors'].append(str(error)))
                    page.on('response',lambda response:result['http_errors'].append(dict(url=response.url,status=response.status)) if response.status>=400 else None)
                    def request_guard(route):
                        if route.request.url.startswith(origin+'/'):
                            route.continue_()
                        else:
                            result['external_requests'].append(route.request.url)
                            route.abort()
                    context.route('**/*',request_guard)
                    for route in ('/','/projects','/volumes','/facts','/confirmations',
                        '/work-orders','/personnel','/timesheets','/labor','/payroll','/settings',
                        '/timesheets/'+str(ids['timesheet']),'/work-orders/'+str(ids['order'])):
                        assert page.goto(origin+route).status==200,route
                        assert page.title()==PRODUCT_NAME if route=='/' else page.title().endswith(' | '+PRODUCT_FAMILY_NAME)
                        assert page.locator('.brand-title').inner_text()==PRODUCT_FAMILY_NAME
                        result['checks'].append(route)
                    page.goto(origin+'/')
                    for filename,collapsed in (('home.png',False),('collapsed.png',True)):
                        if collapsed:
                            page.locator('[data-navigation-toggle]').click()
                            page.wait_for_function("document.body.classList.contains('nav-collapsed')")
                        assert page.locator('.brand-title').is_visible()
                        assert page.locator('.brand-title').evaluate('(e)=>e.scrollWidth<=e.clientWidth')
                        path=evidence/filename
                        page.screenshot(path=str(path),full_page=True)
                        result['screenshots'].append(path.relative_to(ROOT).as_posix())
                    page.set_viewport_size(dict(width=390,height=844))
                    page.locator('[data-navigation-toggle]').click()
                    assert page.locator('.brand-title').is_visible()
                    assert page.locator('.brand-title').evaluate('(e)=>e.scrollWidth<=e.clientWidth')
                    assert page.goto(origin+'/work-orders/'+str(ids['order'])+'/print').status==200
                    assert page.title().endswith(' | '+PRODUCT_FAMILY_NAME)
                    page.goto(origin+'/analytics/export')
                    with page.expect_download() as download:
                        page.locator('[data-export-xlsx]').click()
                    assert download.value.suggested_filename.startswith('PlanktonTime-')
                    assert not (result['js_errors'] or result['http_errors'] or result['external_requests'])
                    page.goto(origin+'/')
                    page.on('dialog',lambda dialog:dialog.accept())
                    page.locator('form[action="/shutdown"] button').click()
                    assert process.wait(timeout=15)==0
                    result['checks'].extend(['1366x768 and 390x844 wordmark','print title','XLSX download','protected shutdown'])
                    result['status']='passed'
    except Exception as error:
        result['status']='blocked' if result.get('phase') in ('browser_launch','server_launch') else 'failed'
        result['error']=str(error)
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass  # sync_playwright may already have closed it during unwinding.
        if process is not None and process.poll() is None:
            process.terminate();process.wait(timeout=10)
        target=evidence/'browser_result.json'
        target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(status=result['status'],phase=result.get('phase'),evidence=str(target)),ensure_ascii=False))
    return 0 if result['status']=='passed' else 2


if __name__=='__main__':
    raise SystemExit(main())
