from io import BytesIO
import re
from zipfile import ZipFile

import pytest

from app.calculation_engine.historical import HistoricalCalculator
from app.database import connect
from app.repositories.event_repository import EventRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.project_repository import ProjectRepository
from app.services.personnel_import import PersonnelImportService
from app.services.personnel_xlsx import parse_workers_xlsx,MAX_UPLOAD
from app.time_utils import instant
from test_personnel import personnel,worker,DATE
from test_personnel_import import uploaded,preview,commit,book,EXAMPLE,counts
from test_web import webapp,client,get,post,ORIGIN,event_data
from test_work_orders import setup_orders
from test_work_order_web import order_form


def import_post(client,path,data,**kwargs):
    return post(client,path,data,**kwargs)


@pytest.mark.parametrize('path',['/personnel/workers','/personnel/brigades','/personnel/import','/personnel/import-history','/personnel/availability'])
def test_personnel_pages_open_and_are_local(client,path):
    r=get(client,path);assert r.status_code==200
    assert '<html lang="ru">' in r.get_data(as_text=True)
    assert 'connect-src \'self\'' in r.headers['Content-Security-Policy']


def test_worker_create_edit_archive_restore_filter_and_history_web(client,personnel,db):
    s=personnel
    data=dict(full_name='Тестовый Работник',contractor_id=s['contractor'],specialty_id=s['specialty'],effective_date=DATE,brigade_id='')
    assert post(client,'/personnel/employees/new',data).status_code==303
    eid=db.execute('SELECT id FROM employees').fetchone()[0]
    assert post(client,f'/personnel/employees/{eid}/edit',dict(data,full_name='Работник Изменённый')).status_code==303
    assert 'Работник Изменённый' in get(client,'/personnel/workers?search=изменённый').get_data(as_text=True)
    assert get(client,f'/personnel/workers/{eid}').status_code==200
    for action in ['archive','restore']:assert post(client,f'/personnel/employees/{eid}/status',dict(action=action)).status_code==303
    assert post(client,'/personnel/employees/new',dict(full_name='Нет подрядчика')).status_code==400


def test_brigade_create_assign_rename_remove_archive_restore_web(client,personnel,db):
    s=personnel;eid=worker(s)
    assert post(client,'/personnel/brigades',dict(contractor_id=s['contractor'],number='01')).status_code==303
    bid=db.execute('SELECT id FROM brigades').fetchone()[0];path=f'/personnel/brigades/{bid}'
    assert post(client,path,dict(action='assign',employee_id=eid,effective_date=DATE)).status_code==303
    assert get(client,path+'?as_of='+DATE).status_code==200
    assert get(client,path+'/members.json?as_of='+DATE).json['members'][0]['id']==eid
    assert post(client,path,dict(action='rename',number='Б-01')).status_code==303
    assert post(client,path,dict(action='remove',employee_id=eid,effective_date='2026-10-21')).status_code==303
    for action in ['archive','restore']:assert post(client,path,dict(action=action,effective_date='2026-10-21',confirm_members='1')).status_code==303
    assert 'Б-01' in get(client,path).get_data(as_text=True)


def test_full_five_step_upload_preview_confirm_repeat_and_template(client,personnel,db):
    before=counts(db)
    assert get(client,'/personnel/import/template.xlsx').status_code==200
    assert parse_workers_xlsx(get(client,'/personnel/import/template.xlsx').data)==[]
    s=personnel;sid='web-import-stable'
    assert post(client,'/personnel/import',dict(contractor_id=s['contractor'],effective_date=DATE,session_id=sid)).status_code==303
    path='/personnel/import/'+sid
    assert post(client,path+'?step=2',dict(action='upload',revision=1,workbook=(BytesIO(book()),'работники.xlsx'))).status_code==303
    page=get(client,path+'?step=3');assert page.status_code==200 and 'Найдено 4' in page.get_data(as_text=True)
    repo=PersonnelImportService(db);revision=repo.get(sid)['revision']
    decisions=dict(action='decide',revision=revision,**{'decision_'+str(n):'create' for n in range(2,6)})
    assert post(client,path+'?step=3',decisions).status_code==303
    assert get(client,path+'?step=4').status_code==200
    assert post(client,path+'?step=4',dict(action='preview',revision=repo.get(sid)['revision'])).status_code==303
    assert counts(db)==before
    confirmation=dict(action='confirm',revision=repo.get(sid)['revision'],confirm_new='1',confirm_import='1')
    assert post(client,path+'?step=5',confirmation).status_code==303
    assert post(client,path+'?step=5',confirmation).status_code==303
    assert counts(db)['employees']==4
    assert 'Импорт сохранён' in get(client,path).get_data(as_text=True)
    assert 'работники.xlsx' in get(client,'/personnel/import-history').get_data(as_text=True)


@pytest.mark.parametrize('bad',['csrf','origin','workspace'])
def test_import_and_personnel_changes_keep_security_guards(client,personnel,bad):
    data=dict(contractor_id=personnel['contractor'],effective_date=DATE,session_id='malicious')
    options={}
    if bad=='csrf':options['token']=False
    elif bad=='origin':options['origin']='https://example.com'
    else:data['workspace_id']='999'
    assert post(client,'/personnel/import',data,**options).status_code==403


def test_import_size_and_invalid_xlsx_errors_no_500(client,db,personnel):
    service,sid=uploaded(db,personnel)
    s=service.start(personnel['contractor'],DATE);path='/personnel/import/'+s+'?step=2'
    for payload,name in [(b'bad','x.xlsx'),(b'x'*(MAX_UPLOAD+1),'x.xlsx'),(book(),'x.csv')]:
        r=post(client,path,dict(action='upload',revision=1,workbook=(BytesIO(payload),name)))
        assert r.status_code in (400,413) and 'Traceback' not in r.get_data(as_text=True)


def test_foreign_urls_posts_and_brigade_json_are_blocked(client,db,personnel):
    s=personnel;eid=worker(s);bid=s['brigades'].save(s['contractor'],'1');service,sid=uploaded(db,s)
    other=ProjectRepository(db).create_project('Другой','OTHER')
    assert post(client,'/projects/select',dict(project_id=other)).status_code==303
    for path in [f'/personnel/workers/{eid}',f'/personnel/brigades/{bid}',f'/personnel/brigades/{bid}/members.json',f'/personnel/import/{sid}']:
        assert get(client,path).status_code==400
    for path,data in [(f'/personnel/workers/{eid}/assignment',dict(brigade_id=bid,effective_date=DATE)),(f'/personnel/brigades/{bid}',dict(action='rename',number='Подмена')),(f'/personnel/import/{sid}',dict(action='cancel'))]:
        assert post(client,path,data).status_code==400
    assert service.get(sid)['status']=='uploaded' and s['brigades'].get(bid)['number']=='1'


def test_new_imported_workers_usable_in_brigade_order_then_transfer_preserves_history(client,db,personnel,work,item,plan):
    s=personnel;service,sid=uploaded(db,s);preview(service,sid);result=commit(service,sid)
    eid=result['employee_ids'][0];petrov=result['employee_ids'][1]
    b1=db.execute("SELECT id FROM brigades WHERE number='1'").fetchone()[0];b2=db.execute("SELECT id FROM brigades WHERE number='2'").fetchone()[0]
    values=dict(shift_date=DATE,shift_start='08:00',shift_end='17:00',contractor_id=s['contractor'],issued_by_employee_id=eid,foreman_employee_id=eid,
                draft_key='import-order',revision='1',plan_version_id=plan,work_item_id=item,planned_quantity='120',line_remarks='',brigade_id=b1)
    r=post(client,'/work-orders/new',values);assert r.status_code==303
    oid=db.execute('SELECT id FROM work_orders').fetchone()[0]
    assert post(client,f'/work-orders/{oid}/issue',{}).status_code==303
    before=get(client,f'/work-orders/{oid}/print').get_data(as_text=True)
    s['brigades'].assign(petrov,b2,'2026-10-21')
    after=get(client,f'/work-orders/{oid}/print').get_data(as_text=True)
    assert re.search(r'<table class="crew-document">.*?</table>',before).group()==re.search(r'<table class="crew-document">.*?</table>',after).group()
    assert EXAMPLE[0][0] in after and EXAMPLE[1][0] in after
    assert db.execute('SELECT count(*) FROM fact_events').fetchone()[0]==0


def test_import_does_not_affect_progress_or_require_orders_for_events(client,db,personnel,item,plan):
    calc=HistoricalCalculator(db)
    before=calc.calculate_as_of('2026-01-31',instant(),plan)
    service,sid=uploaded(db,personnel);preview(service,sid);commit(service,sid)
    assert calc.calculate_as_of('2026-01-31',instant(),plan).total==before.total
    assert post(client,'/facts',event_data(item,quantity='600')).status_code==303
    assert post(client,'/confirmations',event_data(item,quantity='450',confirmation=True,key='accepted')).status_code==303
    result=calc.calculate_as_of('2026-01-31',instant(),plan).total
    assert str(result.actual_progress)=='0.6' and str(result.confirmed_progress)=='0.45'


def test_shared_contractor_employee_availability_web_keeps_same_id(client,db,personnel):
    s=personnel;eid=worker(s);other=ProjectRepository(db).create_project('Другой','OTHER')
    assert post(client,f"/personnel/contractors/{s['contractor']}/share",dict(target_project_id=other)).status_code==303
    assert post(client,'/projects/select',dict(project_id=other)).status_code==303
    assert post(client,'/personnel/availability',dict(contractor_id=s['contractor'],employee_id=eid,effective_date=DATE)).status_code==303
    assert 'Иванов Иван Иванович' in get(client,'/personnel/workers').get_data(as_text=True)
    assert db.execute('SELECT count(*) FROM employees').fetchone()[0]==1


def test_http_access_log_omits_name_search_query():
    from app.web.server import LocalRequestHandler
    handler=object.__new__(LocalRequestHandler);handler.command='GET';handler.path='/personnel/workers?search=Иванов'
    calls=[];handler.log=lambda *args:calls.append(args)
    handler.log_request(200,123)
    assert 'Иванов' not in str(calls) and 'search=' not in str(calls)
    assert '/personnel/workers' in str(calls)


def test_available_workers_json_uses_selected_project_and_shift_date(client,personnel,db):
    s=personnel;e=worker(s);s['people'].end_project_assignment(e,'2026-10-21')
    assert get(client,'/personnel/available-workers.json?as_of='+DATE).json['employees'][0]['id']==e
    assert get(client,'/personnel/available-workers.json?as_of=2026-10-21').json['employees']==[]
    other=ProjectRepository(db).create_project('Другой','OTHER');post(client,'/projects/select',dict(project_id=other))
    assert get(client,'/personnel/available-workers.json?as_of='+DATE).json['employees']==[]
