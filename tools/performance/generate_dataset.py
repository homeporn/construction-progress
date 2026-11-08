"""Fresh reproducible synthetic fixtures. Never accepts existing/non-temporary paths."""
import argparse, calendar, hashlib, json, os, random, sqlite3, sys, tempfile, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app.database import initialize
from app.repositories.project_repository import ProjectRepository
from app.repositories.work_repository import WorkRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.timesheet_repository import TimesheetRepository
from app.repositories.work_order_repository import WorkOrderRepository
from app.repositories.analytics_repository import AnalyticsRepository
PROFILES={'small':(1,3,1000,5000,100,3000),'medium':(3,10,10000,100000,500,15000),'large':(5,30,50000,500000,2000,60000)}
STAMP='2026-01-01T00:00:00.000000Z'
def destination(value):
 p=Path(value).expanduser()
 if not p.is_absolute() or p.is_symlink() or p.exists():raise ValueError('Требуется новый абсолютный путь; перезапись запрещена')
 p=p.resolve();tmp=Path(tempfile.gettempdir()).resolve()
 if tmp not in p.parents or ROOT==p or ROOT in p.parents:raise ValueError('Разрешён только временный каталог ОС вне проекта')
 if p.suffix!='.sqlite3' or p.with_suffix('.json').exists():raise ValueError('Нужны новые .sqlite3 и сопроводительный .json')
 p.parent.mkdir(parents=True,exist_ok=True)
 fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
 return p

def generate(value,profile='small',seed=421,counts=None):
 p=destination(value);start=time.perf_counter();rng=random.Random(seed)
 np,nb,ni,ne,nw,nt=counts or PROFILES[profile];db=initialize(p)
 try:
  projects=[1]+[ProjectRepository(db).create_project('Учебный объект '+str(i),'LOAD'+str(i)) for i in range(2,np+1)]
  buildings=[(r['id'],1) for r in WorkRepository(db).list_buildings()]
  for i in range(3,nb):
   pid=projects[i%np];bid=WorkRepository(db,pid).add_building('L'+str(i),'Учебный корпус '+str(i));buildings.append((bid,pid))
  floors={}
  for bid,pid in buildings:
   floors[bid]=[WorkRepository(db,pid).add_floor(bid,str(j),j) for j in range(1,31)]
  cats=[];db.execute('BEGIN IMMEDIATE')
  for i in range(100):
   unit=('м²','м³','шт.','пог. м')[i%4]
   cid=db.execute('INSERT INTO work_catalog(code,work_type,element,name,unit) VALUES (?,?,?,?,?)',('LOAD.'+str(i),'Учебная работа '+str(i),('Стены','Пол','Потолок')[i%3],'Синтетическая работа '+str(i),unit)).lastrowid;cats.append((cid,unit))
  items=[]
  for i in range(ni):
   bid,pid=buildings[i%nb];slot=i//nb;floor=floors[bid][slot//100];cid,unit=cats[slot%100];direction='DESIGN' if i%17==0 else 'CONSTRUCTION'
   iid=db.execute('INSERT INTO work_items(building_id,floor_id,catalog_id,discipline) VALUES (?,?,?,?)',(bid,floor,cid,direction)).lastrowid;items.append((iid,pid,unit,direction))
  db.commit();plans={}
  for pid in projects:
   for direction in ('CONSTRUCTION','DESIGN'):
    selected=[i for i in items if i[1]==pid and i[3]==direction]
    if not selected:continue
    w=WorkRepository(db,pid)
    for ver in (1,2):
     v=w.create_plan_version(direction,created_at=STAMP);db.execute('BEGIN IMMEDIATE')
     for iid,_,unit,_ in selected:db.execute('INSERT INTO plan_lines(plan_version_id,work_item_id,planned_quantity,labor_norm,unit) VALUES (?,?,?,?,?)',(v,iid,'100' if ver==1 else '120','0.5',unit))
     db.commit();w.approve_plan_version(v,approved_at='2026-01-02T00:00:00Z');plans[(pid,direction)]=v
  db.execute('BEGIN IMMEDIATE');first={};last={}
  for i in range(ne):
   iid,pid,unit,direction=items[i%ni];cycle=i//ni
   confirm=cycle%2==1;table='confirmation_events' if confirm else 'fact_events';datefield='confirmed_date' if confirm else 'work_date'
   qty='20' if confirm else ('150' if iid%19==0 else str(40+rng.randrange(5)))
   correction=None
   if cycle==4:qty='-1';correction=first.get(iid)
   stamp='2026-09-{:02d}T12:00:{:02d}.000000Z'.format(cycle+1,i%60)
   columns='work_item_id,quantity_delta,'+datefield+',recorded_at,event_key,unit,correction_of_id,source_type'
   values=[iid,qty,'2026-08-{:02d}'.format(cycle+1),stamp,'seed-'+str(i),unit,correction,'manual']
   if confirm:columns+=',document_ref';values.append('Учебный акт')
   eid=db.execute('INSERT INTO '+table+'('+columns+') VALUES ('+','.join('?' for _ in values)+')',values).lastrowid
   if not confirm:first.setdefault(iid,eid)
  db.commit();refs=[]
  for pi,pid in enumerate(projects):
   people=PersonnelRepository(db,pid);c=people.save_contractor({'name':'Учебный подрядчик '+str(pid)});existing=people.specialty_by_name('Учебный монтажник');s=existing['id'] if existing else people.save_specialty('Учебный монтажник','LOADSPEC1');b=BrigadeRepository(db,pid).save(c,'01');workers=[]
   for i in range(nw):
    if i%np!=pi:continue
    eid=people.save_worker(dict(full_name='Синтетический Работник '+str(i),contractor_id=c,specialty_id=s,effective_date='2026-01-01'));workers.append(eid)
    if i%4:BrigadeRepository(db,pid).assign(eid,b,'2026-01-01')
   tr=TimesheetRepository(db,pid);tid=tr.create(c,2026,11);v=tr.report(tid)['version']['id'];db.execute('BEGIN IMMEDIATE')
   # Valid bulk fixture creation: FK, checks, immutable-document triggers stay enabled.
   for eid in workers:
    for d in range(1,31):db.execute("INSERT INTO timesheet_entries(version_id,employee_id,work_date,attendance_code_id,code_snapshot,counts_as_work,requires_hours,hours_nanos,created_at,updated_at) VALUES (?,?,?,1,'Я',1,1,8000000000,?,?)",(v,eid,'2026-11-%02d'%d,STAMP,STAMP))
   db.commit();tr.transition(tid,'submit');tr.transition(tid,'approve')
   pick=next(i for i in items if i[1]==pid and i[3]=='CONSTRUCTION');w=WorkRepository(db,pid);meta=w.list_work_items(discipline='CONSTRUCTION')[0]
   orders=WorkOrderRepository(db,pid);oid=orders.save_draft(dict(shift_date='2026-11-01',shift_start='08:00',shift_end='17:00',contractor_id=c,issued_by_employee_id=workers[0],foreman_employee_id=workers[-1]),[dict(work_item_id=meta['id'],planned_quantity='10')],[dict(specialty_id=s,planned_count=1,employee_ids=[workers[0]])]);orders.issue(oid)
   group=AnalyticsRepository(db,pid).save('Учебная группа',meta['catalog_id'],changed_at=STAMP)
   refs.append(dict(project_id=pid,plan_id=plans[(pid,'CONSTRUCTION')],timesheet_id=tid,contractor_id=c,employee_id=workers[0],work_item_id=pick[0],group_id=group,building_id=meta['building_id']))
  if nb>1:WorkRepository(db,buildings[-1][1]).archive_building(buildings[-1][0],True)
  assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok';assert not db.execute('PRAGMA foreign_key_check').fetchall()
  actual={t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ['projects','buildings','work_items','fact_events','confirmation_events','employees','timesheet_entries']}
  assert actual['timesheet_entries']==nt
  manifest=dict(marker='cp421-fresh-v1',profile=profile,seed=seed,counts=actual,refs=refs,report_date='2026-11-30',knowledge_cutoff='2026-12-01T00:00:00Z',bytes=p.stat().st_size,generation_seconds=time.perf_counter()-start)
  p.with_suffix('.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2));return manifest
 finally:db.close()
if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--output',required=True);a.add_argument('--profile',choices=PROFILES,default='small');a.add_argument('--seed',type=int,default=421);args=a.parse_args();print(json.dumps(generate(args.output,args.profile,args.seed),ensure_ascii=False))
