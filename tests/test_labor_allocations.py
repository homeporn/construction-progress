from decimal import Decimal
import sqlite3
import pytest
from app.repositories.base import ValidationError
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.timesheet_repository import TimesheetRepository
from app.repositories.event_repository import EventRepository
from app.repositories.work_repository import WorkRepository

@pytest.fixture
def labor_case(db,work,item,plan):
 from app.repositories.labor_repository import LaborRepository
 p=PersonnelRepository(db);c=p.save_contractor({'name':'Синтетический подрядчик 43'})
 s=p.save_specialty('Специальность 43','S43');b=BrigadeRepository(db).save(c,'01')
 ids=[p.save_worker(dict(full_name='Тестовый Работник '+str(i),contractor_id=c,specialty_id=s,effective_date='2026-01-01')) for i in range(5)]
 for i in ids:BrigadeRepository(db).assign(i,b,'2026-01-01')
 t=TimesheetRepository(db);sheet=t.create(c,2026,10)
 t.set_entries(sheet,[dict(employee_id=i,work_date='2026-10-01',code='Я',hours='8') for i in ids]);t.transition(sheet,'submit');t.transition(sheet,'approve')
 event=EventRepository(db).add_fact(item,'100','2026-10-01',event_key='fact43',recorded_at='2026-10-02T00:00:00Z')
 return dict(db=db,repo=LaborRepository(db),t=t,sheet=sheet,employees=ids,contractor=c,brigade=b,item=item,event=event,plan=plan)

def test_bulk_allocation_and_remaining(labor_case):
 d=labor_case;r=d['repo'];r.allocate(d['item'],'2026-10-01',[dict(employee_id=i,hours='8') for i in d['employees']],operation_key='bulk')
 assert sum(x['hours'] for x in r.hour_rows())==Decimal(40)
 assert all(x['remaining']==0 for x in r.available_hours('2026-10-01'))
 assert d['db'].execute('select count(*) from fact_events').fetchone()[0]==1

def test_split_hours_partial_and_revision_history(labor_case,work):
 d=labor_case;r=d['repo'];emp=d['employees'][0]
 r.allocate(d['item'],'2026-10-01',[dict(employee_id=emp,hours='5')],operation_key='a')
 assert r.available_hours('2026-10-01')[0]['remaining']==3
 with pytest.raises(ValidationError):r.allocate(d['item'],'2026-10-01',[dict(employee_id=emp,hours='9',expected_revision=1)],operation_key='b',reason='Правка')
 r.allocate(d['item'],'2026-10-01',[dict(employee_id=emp,hours='3.125',expected_revision=1)],operation_key='c',reason='Правка')
 assert r.hour_rows()[0]['hours']==Decimal('3.125') and len(r.history())==2
 assert d['db'].execute('select count(*) from labor_hour_allocations').fetchone()[0]==2

def test_bulk_error_is_atomic(labor_case):
 d=labor_case;r=d['repo']
 with pytest.raises(ValidationError):r.allocate(d['item'],'2026-10-01',[dict(employee_id=d['employees'][0],hours='4'),dict(employee_id=d['employees'][1],hours='9')],operation_key='bad')
 assert not r.hour_rows() and not r.history()

def test_idempotency_and_stale_revisions(labor_case):
 d=labor_case;r=d['repo'];rows=[dict(employee_id=d['employees'][0],hours='4')]
 a=r.allocate(d['item'],'2026-10-01',rows,operation_key='same')
 assert a==r.allocate(d['item'],'2026-10-01',rows,operation_key='same')
 assert len(r.hour_rows())==1
 with pytest.raises(ValidationError):r.allocate(d['item'],'2026-10-01',[dict(employee_id=d['employees'][0],hours='2')],operation_key='other')
 with pytest.raises(ValidationError):r.allocate(d['item'],'2026-10-01',[dict(employee_id=d['employees'][0],hours='2')],operation_key='same')

@pytest.mark.parametrize('hours',['-1','0','24.000000001','0.0000000001','NaN','Infinity'])
def test_invalid_hours(labor_case,hours):
 d=labor_case
 with pytest.raises((ValidationError,ValueError)):d['repo'].allocate(d['item'],'2026-10-01',[dict(employee_id=d['employees'][0],hours=hours)],operation_key='bad')

def test_unapproved_or_missing_day(labor_case):
 d=labor_case
 with pytest.raises(ValidationError):d['repo'].allocate(d['item'],'2026-10-02',[dict(employee_id=d['employees'][0],hours='1')],operation_key='no-time')

def test_attribution_split_capacity_and_correction(labor_case):
 d=labor_case;r=d['repo']
 r.attribute(d['event'],d['contractor'],'60',brigade_id=d['brigade'],operation_key='v1')
 r.attribute(d['event'],d['contractor'],'40',operation_key='v2')
 assert sum(x['quantity'] for x in r.attribution_rows())==100
 with pytest.raises(ValidationError):r.attribute(d['event'],d['contractor'],'61',brigade_id=d['brigade'],expected_revision=1,reason='Правка',operation_key='v3')
 EventRepository(d['db']).add_fact(d['item'],'-10','2026-10-01',correction_of_id=d['event'],comment='Исправление',recorded_at='2026-10-03T00:00:00Z',event_key='negative')
 assert r.fact_roots('2026-10-31')[0]['available']==90
 r.attribute(d['event'],d['contractor'],'50',brigade_id=d['brigade'],expected_revision=1,reason='Правка',operation_key='v4')
 assert sum(x['quantity'] for x in r.attribution_rows())==90

def test_historical_revisions(labor_case):
 d=labor_case;r=d['repo'];i=d['employees'][0]
 # Allocations cannot be registered before the source approval.
 from app.time_utils import instant
 first=instant();r.allocate(d['item'],'2026-10-01',[dict(employee_id=i,hours='5')],recorded_at=first,operation_key='hist1')
 r.allocate(d['item'],'2026-10-01',[dict(employee_id=i,hours='2',expected_revision=1)],reason='Правка',operation_key='hist2')
 assert r.hour_rows(first)[0]['hours']==5 and r.hour_rows()[0]['hours']==2

def test_cross_project_and_immutable_sql(labor_case):
 from app.repositories.labor_repository import LaborRepository
 from app.repositories.project_repository import ProjectRepository
 d=labor_case;other=ProjectRepository(d['db']).create_project('Другой','OTHER43')
 with pytest.raises(ValidationError):LaborRepository(d['db'],other).attribute(d['event'],d['contractor'],'1',operation_key='foreign')
 d['repo'].attribute(d['event'],d['contractor'],'1',operation_key='own')
 with pytest.raises(sqlite3.IntegrityError):d['db'].execute("update labor_operations set reason='rewritten'")
 with pytest.raises(sqlite3.IntegrityError):d['db'].execute('delete from labor_volume_attributions')

def test_stale_approved_entry_rejected_inside_transaction(labor_case):
 d=labor_case;old=d['repo'].available_hours('2026-10-01')[0]
 d['t'].create_correction(d['sheet'],'Новая версия');d['t'].transition(d['sheet'],'submit');d['t'].transition(d['sheet'],'approve')
 with pytest.raises(ValidationError):d['repo'].allocate(d['item'],'2026-10-01',[dict(employee_id=d['employees'][0],hours='4',expected_timesheet_entry_id=old['entry_id'])],operation_key='stale')
 assert not d['repo'].hour_rows()

def test_absence_correction_can_revoke_old_allocation(labor_case):
 d=labor_case;r=d['repo'];i=d['employees'][0]
 r.allocate(d['item'],'2026-10-01',[dict(employee_id=i,hours='8')],operation_key='initial')
 d['t'].create_correction(d['sheet'],'Больничный');d['t'].set_entries(d['sheet'],[dict(employee_id=i,work_date='2026-10-01',code='Б',hours='0')])
 d['t'].transition(d['sheet'],'submit');d['t'].transition(d['sheet'],'approve')
 r.allocate(d['item'],'2026-10-01',[dict(employee_id=i,hours='0',expected_revision=1)],reason='Снять после больничного',operation_key='revoke')
 assert r.hour_rows()[0]['hours']==0

def test_overattribution_can_be_reduced_in_steps(labor_case):
 d=labor_case;r=d['repo']
 r.attribute(d['event'],d['contractor'],'60',brigade_id=d['brigade'],operation_key='a1')
 r.attribute(d['event'],d['contractor'],'40',operation_key='a2')
 EventRepository(d['db']).add_fact(d['item'],'-70','2026-10-01',correction_of_id=d['event'],comment='Исправление',recorded_at='2026-10-03T00:00:00Z',event_key='reduce70')
 r.attribute(d['event'],d['contractor'],'0',brigade_id=d['brigade'],expected_revision=1,reason='Первое снятие',operation_key='a3')
 r.attribute(d['event'],d['contractor'],'30',expected_revision=1,reason='Второе снятие',operation_key='a4')
 assert sum(x['quantity'] for x in r.attribution_rows())==30

def test_employer_transfer_cannot_reassign_existing_hours_or_prevent_revocation(labor_case):
 d=labor_case;r=d['repo'];i=d['employees'][0]
 r.allocate(d['item'],'2026-10-01',[dict(employee_id=i,hours='4')],operation_key='first-employer')
 r.attribute(d['event'],d['contractor'],'1',employee_id=i,operation_key='old-worker-attr')
 brigades=BrigadeRepository(d['db']);brigades.remove(i,d['brigade'],'2026-10-02')
 people=PersonnelRepository(d['db']);c2=people.save_contractor({'name':'Второй работодатель'})
 e=people.employee(i)
 people.save_worker(dict(full_name=e['full_name'],contractor_id=c2,specialty_id=e['specialty_id'],effective_date='2026-10-01'),i)
 t=TimesheetRepository(d['db']);s=t.create(c2,2026,10);t.set_entries(s,[dict(employee_id=i,work_date='2026-10-01',code='Я',hours='8')]);t.transition(s,'submit');t.transition(s,'approve')
 sources=r.available_hours('2026-10-01');second=next(x for x in sources if x['employee_id']==i and x['contractor_id']==c2)
 assert second['allocated']==0
 with pytest.raises(ValidationError):r.allocate(d['item'],'2026-10-01',[dict(employee_id=i,hours='3',expected_revision=1,expected_timesheet_entry_id=second['entry_id'])],reason='Нельзя менять работодателя',operation_key='bad-employer')
 r.attribute(d['event'],d['contractor'],'0',employee_id=i,expected_revision=1,reason='Снятие старого назначения',operation_key='revoke-employee')
 assert r.attribution_rows()[0]['quantity']==0
