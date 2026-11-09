from decimal import Decimal
import sqlite3
import pytest
from app.repositories.base import ValidationError
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.timesheet_repository import TimesheetRepository

@pytest.fixture
def pay_case(db):
 from app.repositories.payroll_rules_repository import PayrollRulesRepository
 from app.repositories.payroll_repository import PayrollRepository
 p=PersonnelRepository(db);c=p.save_contractor({'name':'Синтетический подрядчик начислений'});s=p.save_specialty('Работник','PAY44')
 e=p.save_worker(dict(full_name='Синтетический Работник',contractor_id=c,specialty_id=s,effective_date='2020-01-01'))
 t=TimesheetRepository(db);sid=t.create(c,2026,10)
 t.set_entries(sid,[dict(employee_id=e,work_date='2026-10-'+str(n).zfill(2),code='Я',hours='8') for n in range(1,21)])
 t.transition(sid,'submit');t.transition(sid,'approve')
 rules=PayrollRulesRepository(db);term=rules.save_terms(dict(employee_id=e,contractor_id=c,valid_from='2026-01-01',system='hourly',hourly_rate='350'))
 repo=PayrollRepository(db);pid=repo.create_period(c,2026,10,norm_hours='160',operation_key='create')
 return dict(db=db,e=e,c=c,t=t,sid=sid,repo=repo,rules=rules,term=term,pid=pid,vid=repo.get_version(pid)['id'])

def test_hourly_preview_status_approval_closed_and_replay(pay_case):
 d=pay_case;r=d['repo'];run=r.calculate(d['vid'],operation_key='calc')
 assert r.calculate(d['vid'],operation_key='calc')==run
 report=r.report(d['pid']);assert not report['errors'];assert report['totals']['RUB']==Decimal('56000')
 assert report['rows'][0]['base']==Decimal('56000')
 with pytest.raises(ValidationError):r.transition(d['vid'],'close')
 r.transition(d['vid'],'submit',operation_key='submit');r.transition(d['vid'],'approve',operation_key='approve')
 assert r.transition(d['vid'],'approve',operation_key='approve')
 r.transition(d['vid'],'close');assert r.report(d['pid'])['version']['status']=='closed'
 with pytest.raises(ValidationError):r.update_settings(d['vid'],'170')
 with pytest.raises(ValidationError):r.add_adjustment(d['vid'],d['e'],'1',reason='Нельзя')
 with pytest.raises(sqlite3.IntegrityError):d['db'].execute("update payroll_accruals set amount_minor=0")

def test_manual_bonus_and_negative_adjustment_sources_unchanged(pay_case):
 d=pay_case;r=d['repo'];before=d['db'].execute('select sum(hours_nanos) from timesheet_entries').fetchone()[0]
 r.add_adjustment(d['vid'],d['e'],'5000',kind='bonus',reason='Ручная премия',operation_key='bonus')
 r.add_adjustment(d['vid'],d['e'],'-2500',reason='Управленческое уменьшение',operation_key='adj')
 r.calculate(d['vid']);assert r.report(d['pid'])['totals']['RUB']==Decimal('58500')
 assert d['db'].execute('select sum(hours_nanos) from timesheet_entries').fetchone()[0]==before
 assert d['db'].execute('select count(*) from fact_events').fetchone()[0]==0

def test_correction_preserves_old_run_and_delta(pay_case):
 d=pay_case;r=d['repo'];r.calculate(d['vid']);r.transition(d['vid'],'submit');r.transition(d['vid'],'approve')
 old=r.report(d['pid']);new=r.create_correction(d['pid'],'Исправление данных')
 r.add_adjustment(new,d['e'],'-2500',reason='Исправление',operation_key='difference')
 r.calculate(new);current=r.report(d['pid'])
 assert current['totals']['RUB']==Decimal('53500') and current['delta']['RUB']==Decimal('-2500')
 assert r.report(d['pid'],version_id=d['vid'])['totals']==old['totals']
 assert len(current['versions'])==2

def test_terms_change_flags_drift_and_blocks_old_preview_approval(pay_case):
 d=pay_case;r=d['repo'];r.calculate(d['vid'])
 term=next(t for t in d['rules'].list_terms() if t['root_id']==d['term'])
 new=dict(term,hourly_rate='400')
 d['rules'].save_terms(new,term_id=d['term'],expected_revision=term['revision_no'])
 assert r.report(d['pid'])['source_changed']
 with pytest.raises(ValidationError):r.transition(d['vid'],'submit')
 r.calculate(d['vid']);assert r.report(d['pid'])['totals']['RUB']==64000

def test_foreign_period_and_duplicate_month_rejected(pay_case):
 from app.repositories.project_repository import ProjectRepository
 from app.repositories.payroll_repository import PayrollRepository
 d=pay_case;other=ProjectRepository(d['db']).create_project('Другой','PAYOTHER')
 with pytest.raises(ValidationError):PayrollRepository(d['db'],other).report(d['pid'])
 with pytest.raises(ValidationError):d['repo'].create_period(d['c'],2026,10)
 with pytest.raises(ValidationError):d['repo'].add_adjustment(d['vid'],d['e'],'1',reason='')

def test_dashboard_uses_only_approved_snapshot_once_and_explains_cost(pay_case):
 d=pay_case;r=d['repo'];r.calculate(d['vid']);r.transition(d['vid'],'submit');r.transition(d['vid'],'approve')
 report=r.dashboard(year=2026,month=10,contractor_id=d['c'])
 assert report['funds']=={'RUB':Decimal('56000')}
 assert report['by_contractor'][0]['amount']==Decimal('56000')
 assert report['by_method'][0]['system']=='hourly'
 assert report['metrics']['RUB']['employee_count']==1
 assert report['metrics']['RUB']['average']==Decimal('56000')
 assert report['metrics']['RUB']['hours']==Decimal('160')
 assert report['metrics']['RUB']['hour_cost']==Decimal('350')
 new=r.create_correction(d['pid'],'Проверка итогов')
 r.add_adjustment(new,d['e'],'-2500',reason='Уточнение')
 r.calculate(new)
 assert r.dashboard()['funds']=={'RUB':Decimal('56000')}
 assert r.dashboard()['warnings']
 r.transition(new,'submit');r.transition(new,'approve')
 assert r.dashboard()['funds']=={'RUB':Decimal('53500')}
 assert r.dashboard(year=2027)['funds']=={}

def test_timesheet_revision_preserves_50000_and_creates_47500_with_difference(pay_case):
 d=pay_case;r=d['repo']
 term=next(t for t in d['rules'].list_terms() if t['root_id']==d['term'])
 d['rules'].save_terms(dict(term,hourly_rate='312.5'),term_id=d['term'],expected_revision=term['revision_no'])
 r.calculate(d['vid']);r.transition(d['vid'],'submit');r.transition(d['vid'],'approve')
 assert r.report(d['pid'])['totals']['RUB']==Decimal('50000')
 d['t'].create_correction(d['sid'],'Уточнение отработанного дня')
 d['t'].set_entries(d['sid'],[dict(employee_id=d['e'],work_date='2026-10-20',code='В',hours='0')])
 d['t'].transition(d['sid'],'submit');d['t'].transition(d['sid'],'approve')
 assert r.report(d['pid'])['source_changed']
 assert r.dashboard()['warnings']
 new=r.create_correction(d['pid'],'Корректирующий табель')
 r.calculate(new);current=r.report(d['pid'])
 assert current['totals']['RUB']==Decimal('47500')
 assert current['delta']['RUB']==Decimal('-2500')
 assert r.report(d['pid'],d['vid'])['totals']['RUB']==Decimal('50000')
 r.transition(new,'submit');r.transition(new,'approve')

def test_currency_change_does_not_reuse_hours_in_ambiguous_cost(pay_case):
 d=pay_case;r=d['repo']
 r.add_adjustment(d['vid'],d['e'],'100',currency='USD',reason='Отдельная валюта')
 r.calculate(d['vid']);r.transition(d['vid'],'submit');r.transition(d['vid'],'approve')
 report=r.dashboard()
 assert report['funds']=={'RUB':Decimal('56000'),'USD':Decimal('100')}
 assert report['metrics']['RUB']['hour_cost'] is None
 assert report['metrics']['USD']['hour_cost'] is None
 assert report['warnings']
