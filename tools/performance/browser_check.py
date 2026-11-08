"""Chromium observations on a Backup API copy of a synthetic fixture."""
import sys,json,tempfile,sqlite3,subprocess,time,re
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]
p=Path(sys.argv[1]).resolve();m=json.loads(p.with_suffix('.json').read_text());assert m['marker']=='cp421-fresh-v1'
with tempfile.TemporaryDirectory(prefix='cp421-browser-') as tmp:
 target=Path(tmp)/'copy.sqlite3'
 with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as source:
  with sqlite3.connect(target) as db:source.backup(db)
 log=open(Path(tmp)/'server.log','w+')
 proc=subprocess.Popen([str(ROOT/'.venv/bin/python'),'main.py','--db',str(target),'--no-browser','--port','0'],cwd=ROOT,stdout=log,stderr=log)
 try:
  origin=None
  for _ in range(100):
   log.seek(0);match=re.search('http://127.0.0.1:[0-9]+',log.read())
   if match:origin=match.group();break
   time.sleep(.1)
  assert origin
  with sync_playwright() as pw:
   b=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox']);page=b.new_page(viewport={'width':1366,'height':768});errors=[];external=[]
   page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:external.append(r.url) if not r.url.startswith(origin) else None)
   results={}
   for url in ['/volumes','/facts','/timesheets/'+str(m['refs'][0]['timesheet_id'])]:
    samples=[]
    for n in range(12):
     r=page.goto(origin+url);assert r.status==200
     page.wait_for_load_state('load');page.evaluate('new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
     sample=page.evaluate('({navigation:performance.getEntriesByType("navigation")[0].toJSON(),dom:document.querySelectorAll("*").length,rows:document.querySelectorAll("tbody tr").length})')
     if n>=2:samples.append(sample)
    results[url]=samples
   Path(ROOT/'docs/screenshots').mkdir(exist_ok=True);page.screenshot(path=str(ROOT/'docs/screenshots/stage421_timesheet.png'),animations='disabled')
   page.set_viewport_size({'width':390,'height':844});page.reload();page.locator('[data-navigation-toggle]').click();page.screenshot(path=str(ROOT/'docs/screenshots/stage421_mobile.png'),animations='disabled')
   assert not errors and not external
   (ROOT/'docs/performance/browser.json').write_text(json.dumps({'platform':'Linux Chromium','samples':results,'js_errors':errors,'external_requests':external},indent=2));b.close()
 finally:proc.terminate();proc.wait(timeout=10);log.close()
