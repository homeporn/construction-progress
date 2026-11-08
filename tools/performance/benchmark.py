"""Fresh measurements on marked synthetic databases; no fixed speed assertions."""
import argparse,hashlib,json,math,os,platform,resource,sqlite3,statistics,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def stats(values):
 s=sorted(values);return dict(mean=statistics.mean(s),median=statistics.median(s),p95=s[math.ceil(.95*len(s))-1],maximum=max(s))
def run(path,code_root,repeat=10,only=None):
 path=Path(path).resolve()
 if ROOT in path.parents or path.is_symlink():raise ValueError('Только отдельная синтетическая база')
 meta=json.loads(path.with_suffix('.json').read_text())
 if meta['marker']!='cp421-fresh-v1':raise ValueError('Неизвестная база')
 sys.path.insert(0,str(Path(code_root).resolve()))
 from app.database import connect
 from app.web import create_app
 import app.web as web
 from app.calculation_engine.historical import HistoricalCalculator
 from app.services.analytics_service import AnalyticsService
 from app.repositories.timesheet_repository import TimesheetRepository
 from app.services.timesheet_xlsx import export_timesheet_xlsx
 from app.services.xlsx_export import export_xlsx
 with tempfile.TemporaryDirectory(prefix='cp421-measure-') as tmp:
  target=Path(tmp)/'copy.sqlite3'
  with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as src:
   with sqlite3.connect(target) as dst:src.backup(dst)
  db=connect(target);ref=meta['refs'][0];pid=ref['project_id'];queries=[0]
  def trace(sql):queries[0]+=1
  original=web.connect
  def traced(*a,**kw):
   c=original(*a,**kw);c.set_trace_callback(trace);return c
  web.connect=traced;db.set_trace_callback(trace)
  start=time.perf_counter();app=create_app(dict(DATABASE=str(target),TESTING=True,LOCAL_ORIGIN='http://127.0.0.1:5000'));cold=time.perf_counter()-start;client=app.test_client()
  with client.session_transaction() as session:session['project_id']=pid
  date,cutoff=meta['report_date'],meta['knowledge_cutoff'];q='?report_date='+date+'&knowledge_cutoff='+cutoff
  def page(url):
   r=client.get(url,base_url='http://127.0.0.1:5000');assert r.status_code==200,(url,r.status_code);return r.data
  def xlsx():return export_xlsx(AnalyticsService(db,pid).build(date,cutoff,bucket='range'))
  scenarios={'dashboard':lambda:page('/'+q),'volumes':lambda:page('/volumes'+q),'volumes_filter':lambda:page('/volumes'+q+'&building_id='+str(ref['building_id'])),'facts':lambda:page('/facts'+q),'confirmations':lambda:page('/confirmations'+q),'historical':lambda:HistoricalCalculator(db,pid).calculate_as_of(date,cutoff,ref['plan_id']),'timesheet':lambda:page('/timesheets/'+str(ref['timesheet_id'])),'personnel':lambda:page('/personnel/workers'),'analytics':lambda:page('/analytics/summary'+q),'excel':xlsx,'timesheet_excel':lambda:export_timesheet_xlsx(TimesheetRepository(db,pid).report(ref['timesheet_id']))}
  out={'profile':meta['profile'],'db_bytes':path.stat().st_size,'python':sys.version,'sqlite':sqlite3.sqlite_version,'platform':platform.platform(),'cpu_count':os.cpu_count(),'ram_bytes':os.sysconf('SC_PAGE_SIZE')*os.sysconf('SC_PHYS_PAGES'),'cold_app_start_seconds':cold,'results':{}}
  for name,fn in scenarios.items():
   if only and name not in only:continue
   first=time.perf_counter();fn();cold_op=time.perf_counter()-first;fn()
   n=3 if cold_op>10 else repeat;samples=[];sql=[];sizes=[]
   for _ in range(n):
    queries[0]=0;start=time.perf_counter();result=fn();samples.append(time.perf_counter()-start);sql.append(queries[0]);sizes.append(len(result) if isinstance(result,bytes) else 0)
   out['results'][name]=dict(seconds=stats(samples),samples=samples,repeats=n,exception_reason='Дорогая операция >10с' if n<repeat else None,sql=stats(sql),bytes=stats(sizes),cold_operation_seconds=cold_op,process_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024))
   print(name,out['results'][name]['seconds'],flush=True)
  core=HistoricalCalculator(db,pid).calculate_as_of(date,cutoff,ref['plan_id']);out['controls']={'physical':str(core.total.actual_progress),'confirmed':str(core.total.confirmed_progress),'hours':str(TimesheetRepository(db,pid).report(ref['timesheet_id'])['total_hours'])}
  out['explain']=[list(r) for r in db.execute('EXPLAIN QUERY PLAN SELECT quantity_delta FROM fact_events WHERE work_item_id=? AND work_date<=? AND recorded_at<=?',(ref['work_item_id'],date,cutoff))];db.close();return out
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--db',required=True);p.add_argument('--code-root',default=str(ROOT));p.add_argument('--output',required=True);p.add_argument('--repeat',type=int,default=10);p.add_argument('--scenarios');a=p.parse_args()
 if Path(a.output).exists():p.error('Файл результата уже существует')
 result=run(a.db,a.code_root,a.repeat,a.scenarios.split(',') if a.scenarios else None);Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2))
