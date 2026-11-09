"""Stage4.5 browser-facing commands on disposable SQLite with real local guards."""
from io import BytesIO
import re
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from werkzeug.datastructures import MultiDict

from app.repositories.project_repository import ProjectRepository
from app.repositories.work_order_control_repository import WorkOrderControlRepository
from test_work_order_web import client, get, post, ORIGIN
from test_work_order_flow import flow, fact, confirm, setup_orders


def transfer_data(s, amount='20', key=None):
    values=MultiDict(dict(s['header'],shift_date=s['date'],reason='Остаток следующей смене',operation_key=key or uuid4().hex))
    values.add('source_line_id',str(s['line']))
    values.add('quantity_'+str(s['line']),amount)
    for i,c in enumerate(s['crew'][:1]):
        values.add('crew_row',str(i));values.add('crew_specialty_'+str(i),str(c['specialty_id']))
        values.add('crew_count_'+str(i),str(c['planned_count']))
        for e in c['employee_ids']:values.add('crew_employee_'+str(i),str(e))
    return values


def test_existing_card_transfer_form_partial_reservation_and_cancel(client,db,flow):
    s=flow;fact(s,'100');confirm(s,'100')
    path='/work-orders/'+str(s['order'])
    html=get(client,path).get_data(as_text=True)
    assert 'Доступно к переносу' in html and path+'/transfer' in html
    page=get(client,path+'/transfer')
    assert page.status_code==200
    assert 'name="source_line_id"' in page.text and 'name="shift_date"' in page.text
    assert 'name="brigade_id"' in page.text and 'data-search-select' in page.text
    values=transfer_data(s,'12')
    result=post(client,path+'/transfer',values)
    assert result.status_code==303
    assert post(client,path+'/transfer',values).status_code==303
    transfer=db.execute('SELECT * FROM work_order_transfers').fetchone()
    assert s['orders'].get_order(s['order'])['lines'][0]['available']==8
    html=get(client,path).text
    assert 'Зарезервировано' in html and '/work-orders/'+str(transfer['target_order_id']) in html
    response=post(client,'/work-orders/transfers/'+str(transfer['id'])+'/cancel',dict(reason='Отмена смены',operation_key=uuid4().hex))
    assert response.status_code==303
    assert s['orders'].get_order(s['order'])['lines'][0]['available']==20
    assert 'Отмена смены' in get(client,path).text
    assert db.execute('SELECT count(*) FROM fact_events').fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM confirmation_events').fetchone()[0]==1


def test_matches_writeoff_revoke_and_cross_line_scope(client,db,flow):
    s=flow;f=fact(s,'100');c=confirm(s,'80')
    base='/work-orders/{}/lines/{}'.format(s['order'],s['line'])
    page=get(client,base+'/match')
    assert page.status_code==200 and 'Требуется сопоставление' in page.text
    assert post(client,base+'/match',dict(fact_event_id=f,confirmation_event_id=c,quantity='80',reason='Сопоставлены акты',expected_revision='0',operation_key=uuid4().hex)).status_code==303
    assert s['orders'].get_order(s['order'])['lines'][0]['pending_acceptance']==20
    assert post(client,base+'/writeoff',dict(quantity='20',reason='Фронт исключён',operation_key=uuid4().hex)).status_code==303
    rid=db.execute('SELECT id FROM work_order_resolutions').fetchone()[0]
    assert post(client,'/work-orders/resolutions/'+str(rid)+'/revoke',dict(reason='Восстановить фронт',operation_key=uuid4().hex)).status_code==303
    assert s['orders'].get_order(s['order'])['lines'][0]['written_off']==0
    other=s['orders'].save_draft(s['header'],s['lines'][:1],s['crew'][:1])
    assert post(client,'/work-orders/{}/lines/{}/writeoff'.format(other,s['line']),dict(quantity='1',reason='Подмена',operation_key=uuid4().hex)).status_code==400


def test_registry_real_fifty_row_pages_and_filter_retention(client,setup_orders):
    s=setup_orders
    for _ in range(51):s['orders'].save_draft(s['header'],s['lines'][:1],s['crew'][:1])
    first=get(client,'/work-orders?status=draft&has_remaining=1')
    second=get(client,'/work-orders?status=draft&has_remaining=1&page=2')
    assert first.status_code==second.status_code==200
    assert len(re.findall('data-order-id=',first.text))==50
    assert len(re.findall('data-order-id=',second.text))==1
    assert 'page=2' in first.text and 'has_remaining=1' in first.text
    for name in ('brigade_id','has_transfer','unclosed','discrepancies','report_date','knowledge_cutoff'):
        assert 'name="'+name+'"' in first.text


def test_control_export_has_seven_numeric_sheets_and_same_slice(client,db,flow):
    s=flow;fact(s,'100');confirm(s,'100')
    post(client,'/work-orders/'+str(s['order'])+'/transfer',transfer_data(s))
    assert get(client,'/work-orders/control').status_code==200
    response=get(client,'/work-orders/control/export.xlsx')
    assert response.status_code==200
    ns={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with ZipFile(BytesIO(response.data)) as z:
        wb=ET.fromstring(z.read('xl/workbook.xml'))
        assert len(wb.findall('x:sheets/x:sheet',ns))==7
        bodies=[ET.fromstring(z.read('xl/worksheets/sheet'+str(i)+'.xml')) for i in range(1,8)]
        assert all(b.find('x:autoFilter',ns) is not None for b in bodies)
        assert any(c.find('x:v',ns).text=='120' for c in bodies[1].findall('.//x:c[@t="n"]',ns))
        assert not any(b.findall('.//x:f',ns) for b in bodies)
    assert get(client,'/work-orders/control?knowledge_cutoff=bad').status_code==400


@pytest.mark.parametrize('guard',['csrf','origin','workspace','host'])
def test_new_command_forms_keep_local_request_guards(client,db,flow,guard):
    s=flow;values=transfer_data(s,'120');get(client,'/')
    with client.session_transaction(base_url=ORIGIN) as state:
        values['csrf_token']=state['csrf_token'];values['workspace_id']=str(state['project_id'])
    if guard=='csrf':values['csrf_token']='bad'
    if guard=='workspace':values['workspace_id']='2'
    result=client.post('/work-orders/'+str(s['order'])+'/transfer',data=values,
        base_url='http://bad.invalid' if guard=='host' else ORIGIN,
        headers={'Origin':'http://bad.invalid' if guard=='origin' else ORIGIN})
    assert result.status_code==403
    assert not db.execute('SELECT 1 FROM work_order_transfers').fetchone()


def test_foreign_workspace_and_archived_project_are_readonly(client,db,flow):
    s=flow
    ProjectRepository(db).set_archived(1,True)
    html=get(client,'/work-orders/'+str(s['order'])).text
    assert not re.search(r'<form[^>]+method="post"[^>]+action="/work-orders/',html)
    assert post(client,'/work-orders/'+str(s['order'])+'/transfer',transfer_data(s,'120')).status_code==400
    assert get(client,'/work-orders/control').status_code==200
    project=ProjectRepository(db).create_project('Другой объект','OTHER')
    post(client,'/projects/select',dict(project_id=project))
    assert get(client,'/work-orders/'+str(s['order'])+'/transfer').status_code==400


def workflow_post(client,s,target):
    values=MultiDict(dict(employee_id=str(s['issuer']),event_key=uuid4().hex,event_signature=s['orders'].get_order(s['order'])['event_signature']))
    if target=='accepted':
        values.add('review_line_id',str(s['line']));values.add('checked_'+str(s['line']),'1')
        values.add('review_comment_'+str(s['line']),'Проверены объёмы и остатки')
    return post(client,'/work-orders/{}/workflow/{}'.format(s['order'],target),values)


def test_issued_recipient_and_repeat_review_required_before_source_close(client,db,flow):
    s=flow;fact(s,'100');confirm(s,'100')
    assert workflow_post(client,s,'submitted').status_code==303
    assert workflow_post(client,s,'accepted').status_code==303
    assert post(client,'/work-orders/{}/workflow/closed'.format(s['order']),
                dict(employee_id=s['issuer'],event_key=uuid4().hex,event_signature=s['orders'].get_order(s['order'])['event_signature'],comment='Просто комментарий')).status_code==400
    assert post(client,'/work-orders/'+str(s['order'])+'/transfer',transfer_data(s)).status_code==303
    assert workflow_post(client,s,'closed').status_code==400
    target=db.execute('SELECT target_order_id FROM work_order_transfers').fetchone()[0]
    assert post(client,'/work-orders/'+str(target)+'/issue',{}).status_code==303
    assert workflow_post(client,s,'closed').status_code==400
    assert workflow_post(client,s,'accepted').status_code==303
    assert workflow_post(client,s,'closed').status_code==303
    assert s['orders'].get_order(s['order'])['status']=='closed'
    assert 'НЗ-0001' in get(client,'/work-orders/'+str(s['order'])+'/print').text


def test_fully_transferred_without_fact_has_explicit_submission(client,db,flow):
    s=flow
    assert post(client,'/work-orders/'+str(s['order'])+'/transfer',transfer_data(s,'120')).status_code==303
    target=db.execute('SELECT target_order_id FROM work_order_transfers').fetchone()[0]
    assert post(client,'/work-orders/'+str(target)+'/issue',{}).status_code==303
    assert 'Сдать на проверку</button>' in get(client,'/work-orders/'+str(s['order'])).text
    for target_status in ('submitted','accepted','closed'):
        assert workflow_post(client,s,target_status).status_code==303
    assert not db.execute('SELECT 1 FROM fact_events').fetchone()


def test_chain_links_and_historical_controls_on_existing_card(client,db,flow):
    from app.repositories.work_order_transfer_repository import WorkOrderTransferRepository
    from app.time_utils import instant
    s=flow;fact(s,'100');confirm(s,'100');cutoff=instant()
    post(client,'/work-orders/'+str(s['order'])+'/transfer',transfer_data(s))
    first=db.execute('SELECT * FROM work_order_transfers').fetchone()
    post(client,'/work-orders/'+str(first['target_order_id'])+'/issue',{})
    child=s['orders'].get_order(first['target_order_id']);child_line=child['lines'][0]['id']
    s['events'].add_fact(s['item'],'15',s['date'],event_key=uuid4().hex,work_order_line_id=child_line)
    s['events'].add_confirmation(s['item'],'15',s['date'],event_key=uuid4().hex,document_ref='Акт новой смены',work_order_line_id=child_line)
    second=WorkOrderTransferRepository(db).transfer(child['id'],[dict(source_line_id=child_line,quantity='5')],dict(s['header'],shift_date=s['date']),reason='Третья смена',operation_key=uuid4().hex)
    html=get(client,'/work-orders/'+str(s['order'])).text
    assert '/work-orders/'+str(second['order_id']) in html and '115' in html
    previous=get(client,'/work-orders/{}?knowledge_cutoff={}'.format(s['order'],cutoff)).text
    assert 'Третья смена' not in previous and 'Остаток следующей смене' not in previous
    assert '/transfer' not in previous and '/writeoff' not in previous


def test_matching_revision_zero_revoke_and_stale_conflict(client,db,flow):
    s=flow;f=fact(s,'100');c=confirm(s,'80')
    url='/work-orders/{}/lines/{}/match'.format(s['order'],s['line'])
    values=dict(fact_event_id=f,confirmation_event_id=c,quantity='80',reason='Сопоставление',expected_revision='0',operation_key=uuid4().hex)
    assert post(client,url,values).status_code==303
    assert 'name="expected_revision" value="1"' in get(client,url).text
    values.update(quantity='0',reason='Отмена ошибочного сопоставления',expected_revision='1',operation_key=uuid4().hex)
    assert post(client,url,values).status_code==303
    assert s['orders'].get_order(s['order'])['lines'][0]['matching_required']
    values.update(quantity='80',operation_key=uuid4().hex)
    assert post(client,url,values).status_code==400
    assert db.execute('SELECT count(*) FROM work_order_volume_matches').fetchone()[0]==2


def test_invalid_transfer_preserves_selected_rows_and_crew_without_orphans(client,db,flow):
    s=flow;fact(s,'100');confirm(s,'100')
    values=transfer_data(s,'21');values['comment']='Сохранить ввод'
    result=post(client,'/work-orders/'+str(s['order'])+'/transfer',values)
    assert result.status_code==400
    assert 'value="21"' in result.text and 'Сохранить ввод' in result.text
    assert 'name="crew_employee_0"' in result.text
    assert not db.execute('SELECT 1 FROM work_order_transfers').fetchone()
    assert db.execute('SELECT count(*) FROM work_orders').fetchone()[0]==1


def test_accepted_confirmation_link_and_mandatory_fresh_review(client,flow):
    s=flow;fact(s,'120');confirm(s,'100')
    assert workflow_post(client,s,'submitted').status_code==303
    assert workflow_post(client,s,'accepted').status_code==303
    html=get(client,'/work-orders/'+str(s['order'])).text
    assert '/events/confirmation' in html
    confirm(s,'20')
    assert workflow_post(client,s,'closed').status_code==400
    assert workflow_post(client,s,'accepted').status_code==303
    assert workflow_post(client,s,'closed').status_code==303


def test_export_literal_text_and_empty_metadata(client,flow):
    s=flow;fact(s,'100');confirm(s,'100')
    values=transfer_data(s);values['reason']='=SUM(1,2)'
    assert post(client,'/work-orders/'+str(s['order'])+'/transfer',values).status_code==303
    response=get(client,'/work-orders/control/export.xlsx?has_transfer=1&contractor_id='+str(s['contractor']))
    with ZipFile(BytesIO(response.data)) as z:
        xml=z.read('xl/worksheets/sheet4.xml').decode()
        assert '=SUM(1,2)' in xml and '<f>' not in xml
        assert '&quot;has_transfer&quot;' not in xml  # ordinary XML text carries JSON literals
        assert 'has_transfer' in xml
    response=get(client,'/work-orders/control/export.xlsx?search=NO-SUCH-ORDER')
    assert response.status_code==200
    with ZipFile(BytesIO(response.data)) as z:
        assert all('Дата среза' in z.read('xl/worksheets/sheet'+str(i)+'.xml').decode() for i in range(1,8))


@pytest.mark.parametrize('resource',['transfer','matching','writeoff','cancel_transfer','revoke'])
def test_control_commands_are_scoped_to_current_workspace(client,db,flow,resource):
    from app.repositories.work_order_transfer_repository import WorkOrderTransferRepository
    s=flow;fact(s,'100');confirm(s,'100')
    commands=WorkOrderTransferRepository(db)
    if resource=='revoke':
        rid=commands.writeoff(s['line'],'20','Тестовое решение',uuid4().hex)
    else:
        result=commands.transfer(s['order'],[dict(source_line_id=s['line'],quantity='20')],dict(s['header'],shift_date=s['date']),reason='Перенос',operation_key=uuid4().hex)
    project=ProjectRepository(db).create_project('Другой объект','OTHER')
    post(client,'/projects/select',dict(project_id=project))
    base='/work-orders/{}/lines/{}'.format(s['order'],s['line'])
    if resource=='transfer':response=get(client,'/work-orders/'+str(s['order'])+'/transfer')
    elif resource=='matching':response=get(client,base+'/match')
    elif resource=='writeoff':response=post(client,base+'/writeoff',dict(quantity='1',reason='Нет доступа',operation_key=uuid4().hex))
    elif resource=='revoke':response=post(client,'/work-orders/resolutions/'+str(rid)+'/revoke',dict(reason='Нет доступа',operation_key=uuid4().hex))
    else:response=post(client,'/work-orders/transfers/'+str(result['transfer_id'])+'/cancel',dict(reason='Нет доступа',operation_key=uuid4().hex))
    assert response.status_code==400
    assert 'ООО Строй' not in get(client,'/work-orders/control').text


def test_export_sheet_quantities_equal_shared_context(client,db,flow):
    from decimal import Decimal
    s=flow;fact(s,'100');confirm(s,'100')
    values=transfer_data(s,'12');post(client,'/work-orders/'+str(s['order'])+'/transfer',values)
    path='/work-orders/{}/lines/{}/writeoff'.format(s['order'],s['line'])
    post(client,path,dict(quantity='8',reason='Остаток исключён',operation_key=uuid4().hex))
    response=get(client,'/work-orders/control/export.xlsx')
    context=WorkOrderControlRepository(db).analytics()
    ns={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    def values(z,index):
        root=ET.fromstring(z.read('xl/worksheets/sheet'+str(index)+'.xml'))
        rows=[]
        for row in root.findall('x:sheetData/x:row',ns):
            cells={}
            for c in row.findall('x:c',ns):
                cells[re.sub(r'[0-9]','',c.attrib['r'])]=Decimal(c.find('x:v',ns).text) if c.attrib.get('t')=='n' else ''.join(c.itertext())
            rows.append(cells)
        return rows
    with ZipFile(BytesIO(response.data)) as z:
        registry=values(z,1)[1:];control=values(z,2)[1:];opened=values(z,3)[1:]
        transfers=values(z,4)[1:];chains=values(z,5)[1:];conflicts=values(z,6)[1:];sources=values(z,7)[1:]
        assert sum(r['K'] for r in registry)==sum(r['assignment'] for r in context['rows'])
        assert sum(r['M'] for r in control)==sum(r['confirmed'] for r in context['rows'])
        assert sum(r['W'] for r in opened)==sum(r['unresolved'] for r in context['rows'])
        assert sum(r['H'] for r in transfers)==sum(r['quantity'] for r in context['transfers'])
        assert chains[0]['E']==120 and chains[0]['F']==100 and chains[0]['G']==12 and chains[0]['H']==8
        assert sum(r['M'] for r in conflicts)==100
        assert sum(r['H'] for r in sources if r['A'] in ('Факт','Подтверждение'))==200
        assert len(control)==len(context['rows']) and len(transfers)==len(context['transfers'])


def test_xlsx_oversized_snapshots_are_lossless_chunks_on_seventh_sheet(db,flow):
    import hashlib
    import json
    from app.services.work_order_control_xlsx import export_xlsx
    from app.repositories.work_order_transfer_repository import WorkOrderTransferRepository
    s=flow;fact(s,'100');confirm(s,'100')
    WorkOrderTransferRepository(db).transfer(s['order'],[dict(source_line_id=s['line'],quantity='20')],dict(s['header'],shift_date=s['date']),reason='Перенос',operation_key=uuid4().hex)
    data=WorkOrderControlRepository(db).analytics()
    payload=json.dumps({'crew':['=Иванов synthetic '+str(i)+' Я'*30 for i in range(1800)]},ensure_ascii=False)
    assert len(payload)>32767
    data['transfers'][0]['source_executor_snapshot']=payload
    data['transfers'][0]['target_executor_snapshot']=payload
    ns={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with ZipFile(BytesIO(export_xlsx(data))) as z:
        assert len(ET.fromstring(z.read('xl/workbook.xml')).findall('x:sheets/x:sheet',ns))==7
        source=ET.fromstring(z.read('xl/worksheets/sheet7.xml'))
        chunks={}
        for row in source.findall('x:sheetData/x:row',ns)[1:]:
            cells={re.sub('[0-9]','',c.attrib['r']):''.join(c.itertext()) for c in row.findall('x:c',ns)}
            if cells.get('A')=='Текстовая часть':
                assert cells['Q']==hashlib.sha256(payload.encode()).hexdigest()
                chunks.setdefault(cells['N'],[]).append((int(float(cells['O'])),cells['R']))
        assert set(chunks)=={'04. Переносы!O2','04. Переносы!P2'}
        assert all(''.join(piece for _,piece in sorted(parts))==payload for parts in chunks.values())
        for i in range(1,8):
            root=ET.fromstring(z.read('xl/worksheets/sheet'+str(i)+'.xml'))
            assert all(len(t.text or '')<=32767 for t in root.findall('.//x:t',ns))
            assert not root.findall('.//x:f',ns)


def test_export_includes_control_sources_and_respects_knowledge_slice(client,db,flow):
    from app.time_utils import instant
    s=flow;f=fact(s,'100');c=confirm(s,'100');cutoff=instant()
    base='/work-orders/{}/lines/{}'.format(s['order'],s['line'])
    post(client,base+'/match',dict(fact_event_id=f,confirmation_event_id=c,quantity='100',reason='Явное сопоставление',expected_revision='0',operation_key=uuid4().hex))
    post(client,base+'/writeoff',dict(quantity='20',reason='Явное списание',operation_key=uuid4().hex))
    with ZipFile(BytesIO(get(client,'/work-orders/control/export.xlsx').data)) as z:
        source=z.read('xl/worksheets/sheet7.xml').decode()
        assert 'Явное сопоставление' in source and 'Явное списание' in source
        assert 'work_order_resolutions:' in source and 'work_order_volume_matches:' in source
    with ZipFile(BytesIO(get(client,'/work-orders/control/export.xlsx?knowledge_cutoff='+cutoff).data)) as z:
        source=z.read('xl/worksheets/sheet7.xml').decode()
        assert 'Явное сопоставление' not in source and 'Явное списание' not in source


def test_inherited_draft_edit_locks_rows_but_serializes_header_and_crew(client,db,flow):
    s=flow;fact(s,'100');confirm(s,'100')
    post(client,'/work-orders/'+str(s['order'])+'/transfer',transfer_data(s))
    target=db.execute('SELECT target_order_id FROM work_order_transfers').fetchone()[0]
    child=s['orders'].get_order(target);line=child['lines'][0]
    html=get(client,'/work-orders/'+str(target)+'/edit').text
    assert 'data-inherited="1"' in html
    assert re.search(r'<input name="planned_quantity"[^>]+readonly',html)
    assert re.search(r'<select name="work_item_id"[^>]+disabled',html)
    assert '<input type="hidden" name="work_item_id" value="'+str(s['item'])+'">' in html
    assert 'id="add-order-line"' not in html
    values=transfer_data(s)
    values['draft_key']=child['draft_key'];values['revision']=str(child['revision'])
    values['notes']='Новый состав и время при фиксированном объёме'
    values.add('work_item_id',str(s['item']));values.add('planned_quantity','20');values.add('line_remarks',line['remarks'])
    assert post(client,'/work-orders/'+str(target)+'/edit',values).status_code==303
    revised=s['orders'].get_order(target)
    assert revised['lines'][0]['id']==line['id'] and revised['lines'][0]['assignment']==20
    assert revised['notes']==values['notes'] and revised['crew']
