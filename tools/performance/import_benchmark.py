"""Measure the real personnel-import workflow, exclusively in TemporaryDirectory."""
import sys,json,tempfile,time,resource,platform
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tools.performance.benchmark import stats
from app.database import initialize
from app.repositories.personnel_repository import PersonnelRepository
from app.services.personnel_import import PersonnelImportService
from app.services.xlsx_export import pack_workbook,sheet
result={'platform':platform.platform(),'python':sys.version,'runs':{}}
for count in (100,500,2000):
 samples=[];sqls=[]
 for repetition in range(12):
  with tempfile.TemporaryDirectory(prefix='cp421-import-fresh-') as tmp:
   db=initialize(Path(tmp)/'import.sqlite3');people=PersonnelRepository(db);c=people.save_contractor({'name':'Синтетический подрядчик'})
   payload=pack_workbook(['Работники'],[sheet(['ФИО','Номер бригады','Специальность'],[['Синтетический Человек '+str(i),str(i%10),'Учебная специальность'] for i in range(count)],exact_column=False)])
   q=[0]
   def trace(_):q[0]+=1
   db.set_trace_callback(trace);s=PersonnelImportService(db);start=time.perf_counter();sid=s.start(c,'2026-01-01');s.upload(sid,payload,'workers.xlsx',revision=s.get(sid)['revision']);s.decide(sid,{r['row_number']:{'decision':'create','acknowledge':False} for r in s.rows(sid)},revision=s.get(sid)['revision']);preview=s.preview(sid,revision=s.get(sid)['revision']);assert not preview['errors'];s.confirm(sid,revision=s.get(sid)['revision'],confirm_new=True);elapsed=time.perf_counter()-start
   assert db.execute('select count(*) from employees').fetchone()[0]==count
   assert not db.execute('pragma foreign_key_check').fetchall();db.close()
   if repetition>=2:samples.append(elapsed);sqls.append(q[0])
 result['runs'][str(count)]={'seconds':stats(samples),'samples':samples,'sql':stats(sqls),'xlsx_bytes':len(payload),'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024}
 print(count,result['runs'][str(count)]['seconds'],flush=True)
Path(sys.argv[1]).write_text(json.dumps(result,ensure_ascii=False,indent=2))
