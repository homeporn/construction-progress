from decimal import Decimal, localcontext
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from app.calculation_engine.models import Status
from app.database import connect
from app.repositories.analytics_repository import AnalyticsRepository
from app.repositories.base import ValidationError
from app.repositories.event_repository import EventRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.work_repository import WorkRepository
from app.services.analytics_service import AnalyticsService, natural_totals, periods, deviations
from app.services.norms import normative_output
from app.time_utils import instant

CUTOFF='2026-10-19T23:59:59Z'


@pytest.fixture
def school(db):
    work=WorkRepository(db);events=EventRepository(db)
    ProjectRepository(db).update_project(1,'Школа','ШК')
    items=[]
    for building in (1,2,3):
        floor=work.add_floor(building,'3',3)
        items.append(work.add_work_item(building,floor,2))
    version=work.create_plan_version('CONSTRUCTION',created_at='2026-01-01T00:00:00Z')
    for item,qty in zip(items,('4000','3500','2500')):work.add_plan_line(version,item,qty,'0.5')
    work.approve_plan_version(version,approved_at='2026-01-01T01:00:00Z')
    for item,actual,confirmed in zip(items,('3000','2000','1500'),('2800','1700','1300')):
        events.add_fact(item,actual,'2026-10-05',recorded_at='2026-10-06T09:00:00Z',event_key='fact-'+str(item))
        events.add_confirmation(item,confirmed,'2026-10-06',recorded_at='2026-10-07T09:00:00Z',event_key='confirmed-'+str(item),document_ref='Акт')
    return dict(work=work,events=events,items=items,version=version,groups=AnalyticsRepository(db),service=AnalyticsService(db))


def report(s,**kwargs):
    defaults=dict(report_date='2026-10-19',knowledge_cutoff=CUTOFF,start_date='2026-10-01',end_date='2026-10-19')
    defaults.update(kwargs);return s['service'].build(**defaults)


def group(s,**kwargs):
    defaults=dict(changed_at='2026-10-08T12:00:00Z');defaults.update(kwargs)
    return s['groups'].save('Шпаклёвка стен',2,**defaults)


def test_school_exact_natural_and_weighted_totals(school):
    data=report(school);row=data['automatic'][0]
    assert (row['planned'],row['actual'],row['confirmed'],row['remaining'],row['pending'])==(10000,6500,5800,3500,700)
    assert (row['actual_ratio'],row['confirmed_ratio'])==(Decimal('.65'),Decimal('.58'))
    assert data['total'].actual_progress==Decimal('.65') and data['total'].confirmed_progress==Decimal('.58')
    assert data['gap']==Decimal('.07') and data['item_count']==data['actual_count']==data['pending_count']==3
    assert data['building_count']==3 and data['latest_event']=='2026-10-07T09:00:00.000000Z'


@pytest.mark.parametrize('difference',['code','unit','discipline'])
def test_incompatible_natural_totals_are_never_combined(school,difference):
    rows=report(school)['rows'];rows[1]=dict(rows[1],**{difference:'INCOMPATIBLE'})
    with pytest.raises(ValidationError):natural_totals(rows)


def test_different_codes_same_unit_stay_separate_and_progress_uses_labor(school):
    s=school;item=s['work'].add_work_item(1,1,3)
    v=s['work'].create_plan_version('CONSTRUCTION',base_version_id=s['version'],created_at='2026-10-01T00:00:00Z')
    s['work'].add_plan_line(v,item,'1000','2');s['work'].approve_plan_version(v,approved_at='2026-10-01T01:00:00Z')
    data=report(s)
    assert len(data['automatic'])==2 and {g['planned'] for g in data['automatic']}=={10000,1000}
    assert not data['series']['natural'] and data['series']['unit']=='%'
    assert data['total'].planned_labor==7000 and data['total'].actual_earned_labor==3250


def test_manual_exclusion_and_historical_composition(school):
    s=school;g=group(s)
    before=report(s)
    s['groups'].save('Шпаклёвка без В',2,group_id=g,expected_version=1,excludes=[s['items'][2]],changed_at='2026-10-09T12:00:00Z')
    current=report(s,group_id=g)['group']
    assert (current['planned'],current['actual'],current['confirmed'])==(7500,5000,4500)
    old=report(s,group_id=g,knowledge_cutoff='2026-10-08T23:59:59Z')['group']
    assert old['version_no']==1 and old['planned']==10000
    assert report(s,group_id=g,group_version=1)['group']['planned']==10000
    assert report(s)['total']==before['total']


def test_manual_inclusion_outside_automatic_building_and_deduplication(school):
    s=school;g=group(s,base_building_id=1,includes=[s['items'][0],s['items'][1]])
    data=report(s,group_id=g)
    assert data['group']['planned']==7500 and len(data['rows'])==2
    assert {r['id'] for r in data['rows']}==set(s['items'][:2])
    s['groups'].revise(g,reset=True,expected_version=1)
    assert report(s,group_id=g)['group']['planned']==4000


@pytest.mark.parametrize('kwargs',[{'includes':[1,1]},{'excludes':[1,1]},{'includes':[1],'excludes':[1]}])
def test_duplicate_group_overrides_rejected_atomically(db,school,kwargs):
    with pytest.raises(ValidationError):group(school,**kwargs)
    assert db.execute('SELECT count(*) FROM analytical_groups').fetchone()[0]==0


def test_overlapping_groups_never_double_object_totals_or_events(db,school):
    s=school;before=report(s)['total']
    facts=[tuple(r) for r in db.execute('SELECT * FROM fact_events')]
    group(s);group(s,includes=[s['items'][0]])
    data=report(s)
    assert len(data['group_views'])==2 and data['total']==before and data['item_count']==3
    assert [tuple(r) for r in db.execute('SELECT * FROM fact_events')]==facts


def test_missing_position_after_plan_change_is_explicit_and_old_view_kept(school):
    s=school;g=group(s,includes=[s['items'][2]])
    v=s['work'].create_plan_version('CONSTRUCTION',base_version_id=s['version'],created_at='2026-10-10T00:00:00Z')
    s['work'].remove_plan_line(v,s['items'][2]);s['work'].approve_plan_version(v,approved_at='2026-10-10T01:00:00Z')
    current=report(s,group_id=g)
    assert current['group']['invalid'] and current['group']['planned'] is None and current['warnings']
    assert current['total'].status==Status.INVALID
    assert report(s,group_id=g,version_id=s['version'])['group']['planned']==10000


def test_archived_buildings_remain_in_natural_and_weighted_reports(school):
    school['work'].archive_building(3)
    data=report(school)
    assert data['automatic'][0]['planned']==10000 and data['buildings'][2]['status']=='archived'


def test_archive_group_is_versioned_and_can_be_restored(school):
    s=school;g=group(s)
    s['groups'].revise(g,archived=True,expected_version=1)
    assert s['groups'].list()==[] and s['groups'].get(g)['archived']==1
    assert not s['groups'].get(g,'2026-10-09T00:00:00Z')['archived']
    assert report(s,group_id=g)['group']['planned']==10000
    s['groups'].revise(g,archived=False,expected_version=2)
    assert s['groups'].get(g)['version_no']==3


@pytest.mark.parametrize('table',['analytical_groups','analytical_group_versions','analytical_group_members'])
def test_group_history_immutable(db,school,table):
    group(school,excludes=[school['items'][2]])
    with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM '+table)
    with pytest.raises(sqlite3.IntegrityError):db.execute('UPDATE '+table+' SET id=id+10')


def test_group_cannot_mix_projects_codes_units_or_direction(db,school):
    s=school;p=ProjectRepository(db).create_project('Другой','OTHER',buildings=[dict(code='A',name='А')])
    w=WorkRepository(db,p);b=w.list_buildings()[0]['id'];i=w.add_work_item(b,w.add_floor(b,'1',1),2)
    for kwargs in [dict(includes=[i]),dict(unit='м³'),dict(discipline='DESIGN',includes=[s['items'][0]])]:
        with pytest.raises(ValidationError):group(s,**kwargs)
    g=group(s)
    with pytest.raises(ValidationError):AnalyticsRepository(db,p).get(g)
    with pytest.raises(ValidationError):AnalyticsService(db,p).build(group_id=g)
    assert AnalyticsService(db,p).build()['rows']==[]


@pytest.mark.parametrize('filters',[{'building_id':1},{'floor_id':1},{'catalog_id':2},{'element':'Стены'}])
def test_filters_shared_by_tables_dynamics_and_groups(school,filters):
    group(school);data=report(school,filters=dict(filters,catalog_id=2))
    count=1 if 'building_id' in filters or 'floor_id' in filters else 3
    assert len(data['rows'])==len(data['group_views'][0]['rows'])==count
    assert data['series']['points'][-1]['actual']==(3000 if count==1 else 6500)


def test_zero_plan_no_division_and_explicit_invalid(school):
    row=dict(report(school)['rows'][0],planned=Decimal(0))
    t=natural_totals([row]);assert t['actual_ratio'] is None and t['remaining']==-3000


def test_overrun_natural_percent_not_capped_but_physical_progress_is(school):
    s=school;s['events'].add_fact(s['items'][0],'2000','2026-10-12',event_key='extra',recorded_at='2026-10-12T09:00:00Z')
    data=report(s,filters=dict(building_id=1));r=data['rows'][0]
    assert r['actual_ratio']==Decimal('1.25') and r['remaining']==-1000
    assert data['total'].actual_progress==1
    assert deviations(data['rows'],'overrun')==data['rows']


def test_backdated_correction_changes_current_not_earlier_knowledge(school):
    s=school;g=group(s)
    s['events'].add_fact(s['items'][0],'-100','2026-10-05',correction_of_id=1,event_key='correction',recorded_at='2026-10-18T12:00:00Z')
    old=report(s,knowledge_cutoff='2026-10-17T12:00:00Z',group_id=g)
    current=report(s,group_id=g)
    assert old['automatic'][0]['actual']==6500 and current['automatic'][0]['actual']==6400
    assert next(p for p in old['series']['points'] if p['date']=='2026-10-05')['actual_period']==6500
    assert next(p for p in current['series']['points'] if p['date']=='2026-10-05')['actual_period']==6400


def test_daily_weekly_monthly_and_range_dynamics(school):
    s=school
    day=report(s,filters=dict(catalog_id=2))['series']['points'];five=next(p for p in day if p['date']=='2026-10-05')
    six=next(p for p in day if p['date']=='2026-10-06')
    assert (five['actual_period'],five['confirmed_period'],six['confirmed'])==(6500,0,5800)
    for bucket in ('week','month','range'):
        data=report(s,bucket=bucket,filters=dict(catalog_id=2))
        assert data['series']['points'][-1]['actual']==6500
        assert sum(p['actual_period'] for p in data['series']['points'])==6500
    assert periods('2026-10-01','2026-10-19','week')==[('2026-10-01','2026-10-04'),('2026-10-05','2026-10-11'),('2026-10-12','2026-10-18'),('2026-10-19','2026-10-19')]


def test_decimal_arithmetic_ignores_low_ambient_precision(school):
    rows=report(school)['rows']
    rows=[dict(r,planned=Decimal('999999999999999999.123456789'),actual=Decimal('0.000000001'),confirmed=Decimal(0)) for r in rows]
    with localcontext() as ctx:
        ctx.prec=6;t=natural_totals(rows)
    assert t['planned']==Decimal('2999999999999999997.370370367') and t['actual']==Decimal('.000000003')


def test_norm_reference_does_not_change_approved_plan(school,db):
    s=school;before=report(s)['total']
    s['work'].set_catalog_norm(2,'0.7')
    assert db.execute('SELECT reference_labor_norm FROM work_catalog WHERE id=2').fetchone()[0]=='0.7'
    assert {r['labor_norm'] for r in s['work'].list_plan_lines(s['version'])}=={'0.5'}
    assert report(s)['total']==before
    with pytest.raises(ValidationError):s['work'].set_catalog_norm(2,'0')
    s['work'].set_catalog_norm(2,None)


@pytest.mark.parametrize('value',['0','-1','1e2','1.0','0.0000000001','abc'])
def test_norm_database_requires_canonical_positive_decimal(db,value):
    with pytest.raises(sqlite3.IntegrityError):db.execute('UPDATE work_catalog SET reference_labor_norm=? WHERE id=2',(value,))


def test_normative_output_and_missing_data():
    assert normative_output(5,'8','0.5')==80
    assert normative_output(None,8,'0.5') is None
    assert normative_output(5,None,'0.5') is None
    assert normative_output(5,8,None) is None
    for args in [(0,8,'0.5'),(5,25,'0.5'),(5,8,0),('1.5',8,'0.5')]:
        with pytest.raises(ValueError):normative_output(*args)


def test_repeated_group_form_and_concurrent_revisions(db,school):
    s=school;g=group(s,event_key='same-form');assert group(s,event_key='same-form')==g
    path=db.execute('PRAGMA database_list').fetchone()['file'];barrier=Barrier(2)
    def save(index):
        conn=connect(path)
        try:
            barrier.wait(timeout=5)
            try:AnalyticsRepository(conn).save('Редакция '+str(index),2,group_id=g,expected_version=1);return True
            except ValidationError:return False
        finally:conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(save,[1,2]))==[False,True]
    assert s['groups'].get(g)['version_no']==2


def test_design_and_construction_never_merge(school):
    data=report(school,discipline='DESIGN')
    assert data['total'].status==Status.NO_DATA and not data['rows']


def test_unavailable_plan_returns_invalid_not_zero_or_no_data(school):
    data=report(school,report_date='2025-12-31',start_date='2025-12-30',end_date='2025-12-31',version_id=school['version'])
    assert data['total'].status==Status.INVALID and data['total'].actual_progress is None


def test_object_chart_uses_weighted_percent_even_with_one_code(school):
    data=report(school)
    assert not data['series']['natural'] and data['series']['unit']=='%'
    assert data['series']['points'][-1]['actual']==65
    natural=report(school,filters=dict(catalog_id=2))
    assert natural['series']['unit']=='м²' and natural['series']['points'][-1]['actual']==6500


def test_business_date_and_registration_are_independent_in_every_analytic_view(school):
    visible=report(school,report_date='2026-10-05',end_date='2026-10-05',filters=dict(catalog_id=2))
    assert visible['automatic'][0]['actual']==6500 and visible['automatic'][0]['confirmed']==0
    assert visible['series']['points'][-1]['actual']==6500 and visible['series']['points'][-1]['confirmed']==0
    unknown=report(school,report_date='2026-10-05',end_date='2026-10-05',knowledge_cutoff='2026-10-05T23:59:59Z',filters=dict(catalog_id=2))
    assert unknown['automatic'][0]['actual']==0 and unknown['series']['points'][-1]['actual']==0


def test_plan_change_is_not_shown_as_measured_execution(school):
    s=school;v=s['work'].create_plan_version('CONSTRUCTION',base_version_id=s['version'],created_at='2026-10-15T00:00:00Z')
    for i,q in zip(s['items'],('8000','7000','5000')):s['work'].update_plan_line(v,i,q,'0.5')
    s['work'].approve_plan_version(v,approved_at='2026-10-15T01:00:00Z')
    old=report(s,version_id=s['version']);new=report(s,version_id=v)
    assert old['total'].actual_progress==Decimal('.65') and new['total'].actual_progress==Decimal('.325')
    first_available=next(p for p in new['series']['points'] if p['date']=='2026-10-15')
    assert first_available['actual_period'] is None
    assert new['series']['points'][-1]['actual_period']==0


def test_archived_project_prevents_group_edits_but_keeps_reports(db,school):
    s=school;g=group(s);ProjectRepository(db).set_archived(1,True)
    with pytest.raises(ValidationError):s['groups'].revise(g,reset=True,expected_version=1)
    assert report(s)['total'].actual_progress==Decimal('.65')
