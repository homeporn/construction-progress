from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
import json
import sqlite3
from uuid import uuid4

import pytest

from app.calculation_engine.historical import HistoricalCalculator
from app.database import connect
from app.repositories.base import ValidationError
from app.repositories.event_repository import EventRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.work_order_repository import WorkOrderRepository
from app.repositories.work_order_workflow_repository import WorkOrderWorkflowRepository, AssignmentOverrunError
from app.repositories.work_repository import WorkRepository
from app.time_utils import instant
from test_work_orders import setup_orders


@pytest.fixture
def flow(db, setup_orders):
    s = dict(setup_orders)
    order = s['orders'].save_draft(s['header'], s['lines'][:1], s['crew'][:1])
    s['orders'].issue(order)
    line = s['orders'].get_order(order)['lines'][0]['id']
    s.update(order=order, line=line, item=s['items'][0], events=EventRepository(db),
             workflow=WorkOrderWorkflowRepository(db), date=instant()[:10])
    return s


def fact(s, amount, *, linked=True, **kwargs):
    return s['events'].add_fact(s['item'], amount, s['date'], event_key=kwargs.pop('event_key', uuid4().hex),
                               work_order_line_id=s['line'] if linked else None, **kwargs)


def confirm(s, amount, *, linked=True, **kwargs):
    return s['events'].add_confirmation(s['item'], amount, s['date'], document_ref='Акт',
            event_key=kwargs.pop('event_key', uuid4().hex), work_order_line_id=s['line'] if linked else None, **kwargs)


def action(s, target, *, comment='', checks=None, **kwargs):
    if target == 'accepted' and checks is None:
        checks = {s['line']: dict(checked=True, comment='Проверено по документам')}
    return s['workflow'].act(s['order'], target, s['issuer'], comment=comment, checks=checks,
                            event_key=kwargs.pop('event_key', uuid4().hex), **kwargs)


def line(s):
    return s['orders'].get_order(s['order'])['lines'][0]


def foreign_order(db, specialty):
    p = ProjectRepository(db).create_project('Чужой объект', 'FOREIGN', buildings=[dict(code='К1',name='Чужой корпус')])
    w, people = WorkRepository(db, p), PersonnelRepository(db, p)
    c = people.save_contractor(dict(name='Чужой подрядчик'))
    e = people.save_employee(dict(full_name='Чужой ответственный', contractor_id=c, specialty_id=specialty))
    b = w.list_buildings()[0]['id']
    i = w.add_work_item(b, w.add_floor(b,'1',1), 1)
    v = w.create_plan_version('CONSTRUCTION')
    w.add_plan_line(v,i,'1000','0.8');w.approve_plan_version(v)
    r = WorkOrderRepository(db,p)
    o = r.save_draft(dict(shift_date=instant()[:10],shift_start='08:00',shift_end='17:00',contractor_id=c,
                    issued_by_employee_id=e,foreman_employee_id=e), [dict(work_item_id=i,planned_quantity='120')],
                    [dict(specialty_id=specialty,employee_ids=[e])])
    r.issue(o)
    return p,r,o,r.get_order(o)['lines'][0]['id'],i,e


def test_required_acceptance_counts_and_automatic_then_explicit_statuses(db, flow):
    s = flow
    assert s['orders'].get_order(s['order'])['status']=='issued'
    fact(s,'70');fact(s,'30');fact(s,'50',linked=False)
    confirm(s,'60');confirm(s,'40',linked=False)
    row = line(s)
    assert (row['actual'],row['confirmed'],row['remaining']) == (Decimal('100'),Decimal('60'),Decimal('20'))
    assert (row['general_actual'],row['general_confirmed'],row['general_pending']) == (Decimal('150'),Decimal('100'),Decimal('50'))
    document = s['orders'].get_order(s['order'])
    assert [h['new_status'] for h in document['history']] == ['draft','issued','in_progress']
    report = HistoricalCalculator(db).calculate_as_of(s['date'],instant(),s['plan'])
    result = next(r for r in report.items if r.source.work_item_id==s['item'])
    assert (result.source.actual,result.source.confirmed)==(Decimal('150'),Decimal('100'))
    assert result.actual_progress==Decimal('0.15') and result.confirmed_progress==Decimal('0.1')
    action(s,'submitted');action(s,'accepted',comment='Проверены связанные объёмы')
    with pytest.raises(ValidationError,match='остатке'):action(s,'closed')
    action(s,'closed',comment='20 м² перенесены на следующую смену')
    document=s['orders'].get_order(s['order'])
    assert document['status']=='closed' and document['closed_at']
    assert [h['new_status'] for h in document['history']]==['draft','issued','in_progress','submitted','accepted','closed']
    assert len(document['approvals'])==1 and len(document['workflow_actions'])==3
    assert document['closed_snapshot']['values'][0]['actual']=='100'
    assert document['closed_snapshot']['values'][0]['confirmed']=='60'
    assert document['closed_snapshot']['values'][0]['remaining']=='20'
    assert db.execute('SELECT count(*) FROM fact_events').fetchone()[0]==3
    assert db.execute('SELECT count(*) FROM confirmation_events').fetchone()[0]==2


def test_confirmation_can_reference_an_order_when_fact_is_unlinked(db,flow):
    s=flow
    original=fact(s,'150',linked=False)
    before=tuple(db.execute('SELECT * FROM fact_events WHERE id=?',(original,)).fetchone())
    confirm(s,'60')
    row=line(s)
    assert row['actual']==0 and not row['has_actual'] and row['confirmed']==60
    assert row['general_actual']==150 and row['general_confirmed']==60
    assert s['orders'].get_order(s['order'])['status']=='issued'
    assert tuple(db.execute('SELECT * FROM fact_events WHERE id=?',(original,)).fetchone())==before
    with pytest.raises(ValidationError):action(s,'submitted')
    assert any('общему факту' in warning for warning in s['orders'].get_order(s['order'])['warnings'])


def test_fact_and_confirmation_without_order_keep_ordinary_flow(db,flow):
    s=flow
    fact(s,'100',linked=False);confirm(s,'60',linked=False)
    assert line(s)['actual']==line(s)['confirmed']==0
    assert s['orders'].get_order(s['order'])['status']=='issued'
    assert db.execute('SELECT work_order_line_id FROM fact_events').fetchone()[0] is None


@pytest.mark.parametrize('kind',['fact','confirmation'])
def test_foreign_order_links_are_rejected(db,flow,kind):
    s=flow
    _,_,_,foreign_line,_,_=foreign_order(db,s['specialties'][0])
    fact(s,'100',linked=False)
    with pytest.raises(ValidationError,match='объекту'):
        if kind=='fact':s['events'].add_fact(s['item'],'1',s['date'],event_key='foreign',work_order_line_id=foreign_line)
        else:s['events'].add_confirmation(s['item'],'1',s['date'],event_key='foreign',document_ref='Акт',work_order_line_id=foreign_line)
    assert s['orders'].get_order(s['order'])['status']=='issued'


@pytest.mark.parametrize('kind',['fact','confirmation'])
def test_line_must_match_event_work_item(db,flow,kind):
    s=flow
    other=s['items'][1]
    s['events'].add_fact(other,'100',s['date'],event_key='other')
    with pytest.raises(ValidationError,match='не содержит'):
        if kind=='fact':s['events'].add_fact(other,'1',s['date'],event_key='mismatch',work_order_line_id=s['line'])
        else:s['events'].add_confirmation(other,'1',s['date'],event_key='mismatch',document_ref='Акт',work_order_line_id=s['line'])


def test_draft_or_cancelled_order_cannot_supply_new_events(flow):
    s=flow
    draft=s['orders'].save_draft(s['header'],s['lines'][:1],s['crew'][:1])
    draft_line=s['orders'].get_order(draft)['lines'][0]['id']
    with pytest.raises(ValidationError):s['events'].add_fact(s['item'],'1',s['date'],event_key='draft',work_order_line_id=draft_line)
    s['orders'].cancel(s['order'],'Отмена смены')
    with pytest.raises(ValidationError):fact(s,'1')


def test_assignment_overrun_requires_explicit_acknowledgement_and_reason(db,flow):
    s=flow
    fact(s,'100')
    for kwargs in ({},{'allow_assignment_overrun':True},{'overrun_reason':'Дополнительный фронт'}):
        with pytest.raises(AssignmentOverrunError):fact(s,'30',**kwargs)
    assert line(s)['actual']==100
    fact(s,'30',allow_assignment_overrun=True,overrun_reason='Дополнительный фронт')
    assert line(s)['actual']==130 and line(s)['overrun']==10 and line(s)['remaining']==0
    assert db.execute("SELECT overrun_reason FROM fact_events ORDER BY id DESC LIMIT 1").fetchone()[0]=='Дополнительный фронт'


def test_assignment_overrun_does_not_change_plan_progress_cap(db,flow):
    s=flow
    fact(s,'1150',allow_assignment_overrun=True,overrun_reason='Перевыполнение')
    report=HistoricalCalculator(db).calculate_as_of(s['date'],instant(),s['plan'])
    result=next(r for r in report.items if r.source.work_item_id==s['item'])
    assert result.actual_progress==1 and result.source.actual==1150
    assert result.overrun_quantity==150
    assert db.execute('SELECT planned_quantity FROM plan_lines WHERE work_item_id=?',(s['item'],)).fetchone()[0]=='1000'


def test_confirmation_is_limited_by_general_fact_not_order_assignment(flow):
    s=flow
    fact(s,'150',linked=False)
    confirm(s,'130')
    assert line(s)['confirmed']==130 and line(s)['actual']==0
    with pytest.raises(ValidationError):confirm(s,'21')


@pytest.mark.parametrize('kind',['fact','confirmation'])
def test_repeat_submission_creates_one_event_and_one_start_transition(db,flow,kind):
    s=flow
    if kind=='confirmation':fact(s,'100')
    save=fact if kind=='fact' else confirm
    save(s,'60',event_key='same')
    with pytest.raises(ValidationError):save(s,'60',event_key='same')
    table='fact_events' if kind=='fact' else 'confirmation_events'
    assert db.execute('SELECT count(*) FROM '+table+" WHERE event_key='same'").fetchone()[0]==1
    assert [r['new_status'] for r in s['orders'].get_order(s['order'])['history']].count('in_progress')==1


@pytest.mark.parametrize('kind',['fact','confirmation'])
def test_corrections_inherit_link_and_preserve_original(db,flow,kind):
    s=flow
    original=fact(s,'100')
    save=fact
    if kind=='confirmation':original=confirm(s,'60');save=confirm
    table='fact_events' if kind=='fact' else 'confirmation_events'
    before=tuple(db.execute('SELECT * FROM '+table+' WHERE id=?',(original,)).fetchone())
    # Omitting the selected order in a correction must inherit it, not detach it.
    correction=save(s,'-20',linked=False,correction_of_id=original,comment='Уточнение объёма')
    row=db.execute('SELECT * FROM '+table+' WHERE id=?',(correction,)).fetchone()
    assert row['work_order_line_id']==s['line'] and row['correction_of_id']==original
    assert tuple(db.execute('SELECT * FROM '+table+' WHERE id=?',(original,)).fetchone())==before
    assert line(s)['actual' if kind=='fact' else 'confirmed']==(80 if kind=='fact' else 40)


def test_correction_cannot_reassign_or_attach_old_unlinked_event(flow):
    s=flow
    original=fact(s,'100',linked=False)
    with pytest.raises(ValidationError,match='связь'):fact(s,'-10',correction_of_id=original,comment='Нельзя перепривязать')
    original=fact(s,'10')
    order=s['orders'].save_draft(s['header'],s['lines'][:1],s['crew'][:1]);s['orders'].issue(order)
    other_line=s['orders'].get_order(order)['lines'][0]['id']
    with pytest.raises(ValidationError):s['events'].add_fact(s['item'],'-1',s['date'],event_key='reassign',
        correction_of_id=original,comment='Замена наряда',work_order_line_id=other_line)


def test_line_cannot_go_negative_while_general_fact_stays_positive(flow):
    s=flow
    original=fact(s,'10');fact(s,'100',linked=False)
    with pytest.raises(ValidationError):fact(s,'-20',correction_of_id=original,comment='Недопустимый минус')
    assert line(s)['actual']==10 and line(s)['general_actual']==110


def test_annulling_all_linked_facts_keeps_status_and_warns(flow):
    s=flow
    original=fact(s,'70');fact(s,'-70',correction_of_id=original,comment='Аннулирование')
    document=s['orders'].get_order(s['order'])
    assert document['status']=='in_progress' and line(s)['actual']==0
    assert any('аннулированы' in warning for warning in document['warnings'])
    with pytest.raises(ValidationError,match='положительный'):action(s,'submitted')


def test_confirmations_do_not_automatically_submit_or_accept(flow):
    s=flow
    fact(s,'100');confirm(s,'100')
    assert s['orders'].get_order(s['order'])['status']=='in_progress'


def test_review_requires_every_line_and_current_view(db,flow):
    s=flow
    fact(s,'70');action(s,'submitted')
    for checks in ({},{s['line']:dict(checked=False)},{s['line']+99:dict(checked=True)}):
        with pytest.raises(ValidationError,match='каждой'):action(s,'accepted',checks=checks)
    with pytest.raises(ValidationError,match='изменились'):action(s,'accepted',expected_signature='0'*64)
    assert not db.execute('SELECT 1 FROM work_order_line_reviews').fetchone()
    assert s['orders'].get_order(s['order'])['status']=='submitted'


def test_recheck_after_correction_is_needed_for_closure(flow):
    s=flow
    original=fact(s,'100');action(s,'submitted');action(s,'accepted')
    fact(s,'-10',correction_of_id=original,comment='Уточнение после проверки')
    assert s['orders'].get_order(s['order'])['status']=='accepted'
    assert not line(s)['review_current']
    with pytest.raises(ValidationError,match='Повторите'):action(s,'closed',comment='Остаток переносится')
    action(s,'accepted',comment='Повторно проверено');action(s,'closed',comment='30 м² не выполнены')
    assert s['orders'].get_order(s['order'])['status']=='closed'


def test_correction_after_closing_keeps_snapshot_history_and_warns(flow):
    s=flow
    original=fact(s,'100');confirm(s,'60')
    action(s,'submitted');action(s,'accepted');action(s,'closed',comment='20 м² перенесены')
    before=s['orders'].get_order(s['order'])
    fact(s,'-10',correction_of_id=original,comment='Уточнение обмера после закрытия')
    after=s['orders'].get_order(s['order'])
    assert after['status']=='closed' and after['closed_snapshot']==before['closed_snapshot']
    assert after['lines'][0]['actual']==90 and not after['lines'][0]['review_current']
    assert after['history']==before['history']
    assert any('После закрытия' in warning for warning in after['warnings'])


@pytest.mark.parametrize('target',['submitted','accepted','closed'])
def test_workflow_actions_are_idempotent_and_do_not_create_volume_events(db,flow,target):
    s=flow
    fact(s,'100')
    if target in ('accepted','closed'):action(s,'submitted')
    if target=='closed':action(s,'accepted')
    first=action(s,target,comment='Остаток перенесён',event_key='same-action')
    assert action(s,target,comment='Остаток перенесён',event_key='same-action')==first
    assert db.execute('SELECT count(*) FROM work_order_workflow_actions WHERE event_key=?',('same-action',)).fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM fact_events').fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM confirmation_events').fetchone()[0]==0


@pytest.mark.parametrize('table',['work_order_workflow_actions','work_order_line_reviews'])
def test_new_approvals_and_reviews_are_immutable(db,flow,table):
    s=flow
    fact(s,'70');action(s,'submitted');action(s,'accepted')
    with pytest.raises(sqlite3.IntegrityError):db.execute('UPDATE '+table+" SET comment='Подмена'")
    with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM '+table)


def test_event_knowledge_cutoff_hides_later_events_statuses_and_approvals(db,flow):
    s=flow
    before=instant()
    fact(s,'70');after_first=instant()
    fact(s,'30');confirm(s,'60')
    action(s,'submitted');action(s,'accepted');action(s,'closed',comment='20 м² перенесены')
    early=s['orders'].get_order(s['order'],knowledge_cutoff=before)
    middle=s['orders'].get_order(s['order'],knowledge_cutoff=after_first)
    assert early['status']=='issued' and not early['lines'][0]['has_actual']
    assert middle['status']=='in_progress' and middle['lines'][0]['actual']==70 and middle['lines'][0]['confirmed']==0
    assert not middle['workflow_actions'] and middle['closed_snapshot'] is None
    report=HistoricalCalculator(db).calculate_as_of(s['date'],after_first,s['plan'])
    assert next(r.source.actual for r in report.items if r.source.work_item_id==s['item'])==70


@pytest.mark.parametrize('operation',['fact','confirmation','submit'])
def test_project_archiving_blocks_mutations_but_preserves_history(db,flow,operation):
    s=flow
    fact(s,'70')
    ProjectRepository(db).set_archived(1,True)
    with pytest.raises(ValidationError):
        if operation=='fact':fact(s,'1')
        elif operation=='confirmation':confirm(s,'1')
        else:action(s,'submitted')
    assert line(s)['actual']==70


@pytest.mark.parametrize('same_kind',['fact','confirmation'])
def test_concurrent_operations_preserve_start_history_and_general_limits(db,flow,same_kind):
    s=flow
    if same_kind=='confirmation':fact(s,'100',linked=False)
    filename=db.execute('PRAGMA database_list').fetchone()['file']
    barrier=Barrier(2)
    def save(index):
        connection=connect(filename)
        try:
            events=EventRepository(connection)
            barrier.wait(timeout=5)
            try:
                if same_kind=='fact':events.add_fact(s['item'],'30',s['date'],event_key='race-'+str(index),work_order_line_id=s['line'])
                else:events.add_confirmation(s['item'],'60',s['date'],event_key='race-'+str(index),document_ref='Акт',work_order_line_id=s['line'])
                return 'saved'
            except ValidationError:return 'rejected'
        finally:connection.close()
    with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(save,[1,2]))
    if same_kind=='fact':
        assert results==['saved','saved'] and line(s)['actual']==60
        assert [r['new_status'] for r in s['orders'].get_order(s['order'])['history']].count('in_progress')==1
    else:
        assert sorted(results)==['rejected','saved'] and line(s)['confirmed']==60
        assert s['orders'].get_order(s['order'])['status']=='issued'


def test_new_plan_exclusion_blocks_new_linked_events_but_allows_correction(flow):
    s=flow
    original=fact(s,'70')
    version=s['work'].create_plan_version('CONSTRUCTION',base_version_id=s['plan'])
    s['work'].remove_plan_line(version,s['item']);s['work'].approve_plan_version(version)
    with pytest.raises(ValidationError):fact(s,'1')
    fact(s,'-10',correction_of_id=original,comment='Уточнение старого выполнения')
    assert line(s)['actual']==60


def test_event_source_and_link_are_independent(db,flow):
    s=flow
    fact(s,'30',source_type='manual');fact(s,'30',source_type='work_order')
    assert [tuple(r) for r in db.execute('SELECT source_type,work_order_line_id FROM fact_events ORDER BY id')]==[('manual',s['line']),('work_order',s['line'])]
    with pytest.raises(ValidationError):fact(s,'1',linked=False,source_type='work_order')


def test_foreign_reviewer_or_foreign_order_cannot_be_used(db,flow):
    s=flow
    _,_,other,_,_,employee=foreign_order(db,s['specialties'][0])
    fact(s,'70')
    with pytest.raises(ValidationError):s['workflow'].act(other,'submitted',s['issuer'],event_key='foreign-order')
    with pytest.raises(ValidationError):s['workflow'].act(s['order'],'submitted',employee,event_key='foreign-employee')


def test_historical_card_does_not_leak_later_closure_or_cancellation(flow):
    s=flow
    fact(s,'70');cutoff=instant()
    action(s,'submitted');action(s,'accepted');action(s,'closed',comment='50 м² перенесены')
    earlier=s['orders'].get_order(s['order'],knowledge_cutoff=cutoff)
    assert earlier['closed_at'] is None and earlier['cancellation_reason']==''
    assert earlier['updated_at']<=cutoff and earlier['workflow_reason']=='Первое выполнение, связанное с нарядом'
    other=s['orders'].save_draft(s['header'],s['lines'][:1],s['crew'][:1])
    s['orders'].issue(other);before_cancel=instant()
    s['orders'].cancel(other,'Отмена следующей смены')
    earlier=s['orders'].get_order(other,knowledge_cutoff=before_cancel)
    assert earlier['status']=='issued' and earlier['cancellation_reason']==''
    assert earlier['history'][-1]['new_status']=='issued'


def test_cancelling_in_progress_keeps_events_and_allows_only_corrections(db,flow):
    s=flow
    original=fact(s,'70');confirm(s,'60')
    s['orders'].cancel(s['order'],'Смена прекращена')
    assert s['orders'].get_order(s['order'])['status']=='cancelled'
    assert (line(s)['actual'],line(s)['confirmed'])==(70,60)
    with pytest.raises(ValidationError):fact(s,'1')
    with pytest.raises(ValidationError):confirm(s,'1')
    fact(s,'-10',correction_of_id=original,comment='Уточнён обмер прекращённой смены')
    assert s['orders'].get_order(s['order'])['status']=='cancelled'
    assert (line(s)['actual'],line(s)['confirmed'])==(60,60)
    assert db.execute('SELECT count(*) FROM fact_events').fetchone()[0]==2


@pytest.mark.parametrize('status',['submitted','accepted','closed'])
def test_new_events_obey_document_status_and_global_fact_stays_independent(flow,status):
    s=flow
    fact(s,'70');action(s,'submitted')
    if status in ('accepted','closed'):action(s,'accepted')
    if status=='closed':action(s,'closed',comment='50 м² перенесены')
    with pytest.raises(ValidationError):fact(s,'1')
    if status=='submitted':confirm(s,'10')
    else:
        with pytest.raises(ValidationError):confirm(s,'10')
    fact(s,'1',linked=False)
    assert line(s)['general_actual']==71 and line(s)['actual']==70
    assert s['orders'].get_order(s['order'])['status']==status
