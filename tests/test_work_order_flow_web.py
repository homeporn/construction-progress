import re
from uuid import uuid4

import pytest
from werkzeug.datastructures import MultiDict

from app.repositories.project_repository import ProjectRepository
from test_work_order_flow import flow, setup_orders, fact, confirm, action
from test_work_order_web import client, get, post


def event_data(s, amount, *, linked=True, **kwargs):
    return dict(discipline='CONSTRUCTION', building_id='1', floor_id='1', work_item_id=str(s['item']),
                version_id=str(s['plan']), quantity_delta=amount, business_date=s['date'],
                event_key=uuid4().hex, work_order_line_id=str(s['line']) if linked else '', document_ref='Акт', **kwargs)


@pytest.mark.parametrize('path',['/facts','/confirmations'])
def test_general_journal_offers_matching_optional_order(client,flow,path):
    s=flow
    fact(s,'100',linked=False)
    response=get(client,path+'?work_item_id='+str(s['item']))
    assert response.status_code==200
    html=response.get_data(as_text=True)
    assert 'Наряд-задание (необязательно)' in html and 'НЗ-0001' in html and 'ООО Строй' in html
    assert 'name="work_order_line_id"' in html and 'data-order-hint=' in html


def test_general_fact_and_confirmation_keep_manual_source_with_selected_order(client,db,flow):
    s=flow
    assert post(client,'/facts',event_data(s,'70',source_type='work_order')).status_code==303
    assert post(client,'/confirmations',event_data(s,'60',source_type='work_order')).status_code==303
    assert tuple(db.execute('SELECT source_type,work_order_line_id FROM fact_events').fetchone())==('manual',s['line'])
    assert tuple(db.execute('SELECT source_type,work_order_line_id FROM confirmation_events').fetchone())==('manual',s['line'])
    assert s['orders'].get_order(s['order'])['status']=='in_progress'
    assert 'НЗ-0001' in get(client,'/facts').get_data(as_text=True)


def test_confirmation_after_unlinked_fact_does_not_rewrite_fact_or_start_order(client,db,flow):
    s=flow
    assert post(client,'/facts',event_data(s,'100',linked=False)).status_code==303
    assert post(client,'/confirmations',event_data(s,'60')).status_code==303
    assert db.execute('SELECT work_order_line_id FROM fact_events').fetchone()[0] is None
    document=s['orders'].get_order(s['order'])
    assert document['status']=='issued'
    assert document['lines'][0]['actual']==0 and document['lines'][0]['confirmed']==60
    html=get(client,'/work-orders/'+str(s['order'])).get_data(as_text=True)
    assert 'общему факту' in html and 'Сдать на проверку</button>' not in html


def test_direct_entry_from_order_creates_one_event_with_work_order_source(client,db,flow):
    s=flow
    path='/work-orders/{}/lines/{}/events/fact'.format(s['order'],s['line'])
    assert get(client,path).status_code==200
    response=post(client,path,event_data(s,'70'))
    assert response.status_code==303 and response.location.endswith('/work-orders/'+str(s['order']))
    assert db.execute('SELECT count(*) FROM fact_events').fetchone()[0]==1
    assert tuple(db.execute('SELECT source_type,work_order_line_id FROM fact_events').fetchone())==('work_order',s['line'])


def test_assignment_overrun_is_a_two_step_operation_with_one_event(client,db,flow):
    s=flow
    values=event_data(s,'130')
    response=post(client,'/facts',values)
    assert response.status_code==400
    html=response.get_data(as_text=True)
    assert 'Событие ещё не создано' in html and 'Подтвердить и сохранить факт' in html
    assert db.execute('SELECT count(*) FROM fact_events').fetchone()[0]==0
    values.update(allow_assignment_overrun='1',overrun_reason='Дополнительный фронт')
    assert post(client,'/facts',values).status_code==303
    assert db.execute('SELECT count(*) FROM fact_events').fetchone()[0]==1
    assert db.execute('SELECT overrun_reason FROM fact_events').fetchone()[0]=='Дополнительный фронт'
    assert post(client,'/facts',values).status_code==400
    assert db.execute('SELECT count(*) FROM fact_events').fetchone()[0]==1


def workflow_data(s,target):
    data=MultiDict(dict(employee_id=str(s['issuer']),event_key=uuid4().hex,event_signature=s['orders'].get_order(s['order'])['event_signature'],comment='20 м² перенесены'))
    if target=='accepted':
        data.add('review_line_id',str(s['line']));data.add('checked_'+str(s['line']),'1')
        data.add('review_comment_'+str(s['line']),'Проверка строки')
    return data


def test_submit_review_close_and_print_show_only_linked_quantities(client,db,flow):
    s=flow
    fact(s,'100');fact(s,'50',linked=False);confirm(s,'60');confirm(s,'40',linked=False)
    path='/work-orders/'+str(s['order'])
    for target in ['submitted','accepted','closed']:
        assert post(client,path+'/workflow/'+target,workflow_data(s,target)).status_code==303
    html=get(client,path).get_data(as_text=True)
    assert 'Закрыт' in html and 'Общие по работе' in html and '20 м² перенесены' in html
    printed=get(client,path+'/print').get_data(as_text=True)
    assert re.findall(r'<td[^>]+data-order-actual[^>]*>(.*?)</td>',printed)==['100']
    assert re.findall(r'<td[^>]+data-order-confirmed[^>]*>(.*?)</td>',printed)==['60']
    assert 'Проверено' in printed and 'Проверка строки' in printed
    assert 'Снимок на момент закрытия' in printed
    assert db.execute('SELECT count(*) FROM fact_events').fetchone()[0]==2
    assert db.execute('SELECT count(*) FROM confirmation_events').fetchone()[0]==2


def test_historical_card_and_print_hide_future_workflow_and_events(client,flow):
    from app.time_utils import instant
    s=flow
    fact(s,'70');cutoff=instant();fact(s,'30');confirm(s,'60')
    action(s,'submitted');action(s,'accepted');action(s,'closed',comment='Остаток переносится')
    path='/work-orders/'+str(s['order'])
    response=get(client,path+'?knowledge_cutoff='+cutoff)
    assert response.status_code==200
    html=response.get_data(as_text=True)
    assert '/workflow/submitted' not in html and '/workflow/closed' not in html
    printed=get(client,path+'/print?knowledge_cutoff='+cutoff).get_data(as_text=True)
    assert re.findall(r'<td[^>]+data-order-actual[^>]*>(.*?)</td>',printed)==['70']
    assert re.findall(r'<td[^>]+data-order-confirmed[^>]*>(.*?)</td>',printed)==['']
    assert 'Снимок на момент закрытия' not in printed


def test_workflow_rejects_stale_view_missing_checks_and_bad_transition(client,flow):
    s=flow
    fact(s,'70')
    path='/work-orders/'+str(s['order'])
    values=workflow_data(s,'closed')
    assert post(client,path+'/workflow/closed',values).status_code==400
    values=workflow_data(s,'submitted');values['event_signature']='0'*64
    assert post(client,path+'/workflow/submitted',values).status_code==400
    action(s,'submitted')
    assert post(client,path+'/workflow/accepted',workflow_data(s,'submitted')).status_code==400
    assert s['orders'].get_order(s['order'])['status']=='submitted'


def test_workflow_and_order_line_urls_reject_other_workspace(client,db,flow):
    s=flow
    p=ProjectRepository(db).create_project('Чужой объект','OTHER')
    post(client,'/projects/select',dict(project_id=p))
    assert get(client,'/work-orders/{}/lines/{}/events/fact'.format(s['order'],s['line'])).status_code==400
    assert post(client,'/work-orders/{}/workflow/submitted'.format(s['order']),dict(employee_id=s['issuer'],event_key='attack',event_signature='0'*64)).status_code==400


def test_correction_form_inherits_closed_order_link(client,db,flow):
    s=flow
    original=fact(s,'100')
    action(s,'submitted');action(s,'accepted');action(s,'closed',comment='20 м² не выполнены')
    values=event_data(s,'-10',linked=False,correction_of_id=str(original),comment='Уточнение после закрытия')
    assert post(client,'/facts',values).status_code==303
    assert db.execute('SELECT work_order_line_id FROM fact_events ORDER BY id DESC').fetchone()[0]==s['line']
    printed=get(client,'/work-orders/{}/print'.format(s['order'])).get_data(as_text=True)
    assert 'Проверено ранее; данные изменились' in printed and 'После закрытия' in printed


def test_historical_print_link_preserves_visible_cutoff(client,flow):
    from html import unescape
    from app.time_utils import instant
    s=flow
    fact(s,'70');cutoff=instant();fact(s,'30')
    action(s,'submitted');action(s,'accepted');action(s,'closed',comment='20 м² не выполнены')
    path='/work-orders/'+str(s['order'])
    html=get(client,path+'?knowledge_cutoff='+cutoff).get_data(as_text=True)
    href=unescape(re.search(r'href="([^"]+)">Печатная форма</a>',html).group(1))
    assert 'knowledge_cutoff='+cutoff in href
    printed=get(client,href).get_data(as_text=True)
    assert re.findall(r'<td[^>]+data-order-actual[^>]*>(.*?)</td>',printed)==['70']
    assert 'Снимок на момент закрытия' not in printed
