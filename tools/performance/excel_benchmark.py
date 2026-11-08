"""Existing XLSX writer component; these are not end-to-end analytics timings."""
import sys,json,time,resource
from pathlib import Path
from decimal import Decimal
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from app.services.xlsx_export import sheet,pack_workbook
from tools.performance.benchmark import stats
out={}
for n in (10000,50000):
 samples=[]
 for rep in range(12):
  start=time.perf_counter();body=sheet(['Код','Наименование','Единица','План','Факт','Норма'],[['L'+str(i),'Учебная работа','м²',Decimal('1000'),Decimal('600.125'),Decimal('0.5')] for i in range(n)]);data=pack_workbook(['Ведомость'],[body]);elapsed=time.perf_counter()-start
  if rep>=2:samples.append(elapsed)
 out[str(n)]={'seconds':stats(samples),'samples':samples,'xlsx_bytes':len(data),'process_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024}
Path(sys.argv[1]).write_text(json.dumps(out,indent=2))
