"""Actual repository writes on a private Backup API fixture; no production access."""
import sys,json,tempfile,sqlite3,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tools.performance.benchmark import stats
from app.database import connect
from app.repositories.event_repository import EventRepository
from app.repositories.timesheet_repository import TimesheetRepository
p=Path(sys.argv[1]).resolve();m=json.loads(p.with_suffix('.json').read_text());assert m['marker']=='cp421-fresh-v1' and ROOT not in p.parents
with tempfile.TemporaryDirectory(prefix='cp421-write-') as folder:
 target=Path(folder)/'copy.sqlite3'
 with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as source:
  with sqlite3.connect(target) as dst:source.backup(dst)
 ref=m['refs'][0];db=connect(target);e=EventRepository(db,ref['project_id']);t=TimesheetRepository(db,ref['project_id']);sid=ref['timesheet_id'];vid=t.create_correction(sid,'Синтетический замер');w=t.report(sid)['workers'][0]['employee_id'];out={}
 for name in ['fact','confirmation','cell']:
  samples=[]
  for n in range(12):
   start=time.perf_counter()
   if name=='fact':
    args=dict(event_key='writefact'+str(n),recorded_at=m['knowledge_cutoff']);eid=e.add_fact(ref['work_item_id'],'0.001',m['report_date'],**args)
   elif name=='confirmation':e.add_confirmation(ref['work_item_id'],'0.001',m['report_date'],event_key='writeconfirm'+str(n),recorded_at=m['knowledge_cutoff'],document_ref='Учебный акт')
   else:t.set_entries(sid,[dict(employee_id=w,work_date='2026-11-01',code='Я',hours='6.125')],version_id=vid,operation_key='writecell'+str(n))
   if n>=2:samples.append(time.perf_counter()-start)
  out[name]={'seconds':stats(samples),'samples':samples}
 def concurrent(n):
  c=connect(target)
  try:
   if n%2:EventRepository(c,ref['project_id']).add_fact(ref['work_item_id'],'0.001',m['report_date'],recorded_at=m['knowledge_cutoff'],event_key='concurrent'+str(n))
   else:TimesheetRepository(c,ref['project_id']).report(sid)
   return None
  except Exception as ex:return str(ex)
  finally:c.close()
 with ThreadPoolExecutor(max_workers=4) as pool:errors=[e for e in pool.map(concurrent,range(20)) if e]
 out['concurrency']={'operations':20,'errors':errors,'locked':sum('locked' in e for e in errors)}
 out['sqlite']={k:db.execute('PRAGMA '+k).fetchone()[0] for k in ['busy_timeout','journal_mode','foreign_keys']};assert not db.execute('PRAGMA foreign_key_check').fetchall();db.close()
 Path(sys.argv[2]).write_text(json.dumps(out,indent=2))
