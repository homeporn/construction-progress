"""Exact control totals are projections of linked primary events."""
from decimal import Decimal, localcontext

import pytest

from app.repositories.base import ValidationError
from app.services.work_order_control import WorkOrderControlProjection
from test_work_order_flow import flow, fact, confirm
from test_work_orders import setup_orders


def metric(db, s, **kwargs):
    return WorkOrderControlProjection(db).metrics(s['order'], **kwargs)[0][0]


def test_safe_balance_uses_performed_not_only_confirmed(db, flow):
    fact(flow, '110')
    confirm(flow, '100')
    row = metric(db, flow)
    assert row['assignment'] == Decimal('120')
    assert row['execution_remaining'] == Decimal('10')
    assert row['confirmed_remaining'] == Decimal('20')
    assert row['available'] == Decimal('10')
    assert row['pending_acceptance'] is None
    assert row['matching_required'] is True
    assert row['state_key'] == 'matching_required'


def test_unlinked_events_only_affect_general_totals(db, flow):
    fact(flow, '100')
    fact(flow, '25', linked=False)
    confirm(flow, '100', linked=False)
    row = metric(db, flow)
    assert (row['actual'], row['confirmed'], row['available']) == (Decimal('100'), Decimal('0'), Decimal('20'))
    assert (row['general_actual'], row['general_confirmed']) == (Decimal('125'), Decimal('100'))
    assert row['pending_acceptance'] == Decimal('100')
    assert not row['matching_required']


def test_no_events_and_actual_overrun_are_not_allocation_conflicts(db, flow):
    row = metric(db, flow)
    assert row['available'] == Decimal('120') and row['pending_acceptance'] == 0
    fact(flow, '125', allow_assignment_overrun=True, overrun_reason='Проверено')
    row = metric(db, flow)
    assert row['overrun'] == 5 and row['allocation_excess'] == 5
    assert row['available'] == 0 and row['conflicts'] == []


def test_projection_rejects_foreign_order_and_batches_reads(db, flow):
    queries = []
    db.set_trace_callback(queries.append)
    try:
        result, signature = WorkOrderControlProjection(db).metrics(flow['order'])
    finally:
        db.set_trace_callback(None)
    assert len(signature) == 64 and len(result) == 1
    assert sum('FROM fact_events e' in q for q in queries) == 1
    assert sum('FROM confirmation_events e' in q for q in queries) == 1
    with pytest.raises(ValidationError):
        WorkOrderControlProjection(db, 999).metrics(flow['order'])


def test_decimal_context_does_not_round_projection(db, flow):
    fact(flow, '100.123456789')
    with localcontext() as ctx:
        ctx.prec = 3
        row = metric(db, flow)
    assert row['available'] == Decimal('19.876543211')


def test_legacy_signature_preserves_existing_reviews(db, flow):
    import hashlib
    from app.repositories.work_order_repository import encoded
    fact(flow, '100')
    confirm(flow, '80')
    records=[]
    for table,date in [('fact_events','work_date'),('confirmation_events','confirmed_date')]:
        records.extend((table,)+tuple(r) for r in db.execute('SELECT id,work_order_line_id,quantity_delta,'+date+',recorded_at FROM '+table+' WHERE work_order_line_id=? ORDER BY id',(flow['line'],)))
    expected=hashlib.sha256(encoded(records).encode('utf-8')).hexdigest()
    assert WorkOrderControlProjection(db).metrics(flow['order'])[1]==expected


def control(db):
    from app.repositories.work_order_transfer_repository import WorkOrderTransferRepository
    return WorkOrderTransferRepository(db)


def transfer(db, s, quantity, key='transfer'):
    return control(db).transfer(s['order'],[dict(source_line_id=s['line'],quantity=quantity)],s['header'],s['crew'][:1],reason='Следующая смена',operation_key=key)


def test_draft_reservation_issue_and_cancellation_history(db, flow):
    s=flow
    fact(s,'100');confirm(s,'100')
    before=metric(db,s)
    result=transfer(db,s,'20')
    row=metric(db,s)
    assert (row['transferred'],row['reserved'],row['sent'],row['available'],row['unresolved'])==(20,20,0,0,20)
    assert before['available']==20
    assert WorkOrderControlProjection(db).closure_errors(s['order'])
    s['orders'].issue(result['order_id'])
    row=metric(db,s)
    assert (row['reserved'],row['sent'],row['unresolved'])==(0,20,0)
    assert not WorkOrderControlProjection(db).closure_errors(s['order'])


def test_cancelled_reservation_returns_capacity_and_keeps_history(db, flow):
    result=transfer(db,flow,'20')
    control(db).cancel_transfer(result['transfer_id'],'Отмена потребности','cancel')
    row=metric(db,flow)
    assert (row['transferred'],row['reserved'],row['available'])==(0,0,120)
    report=WorkOrderControlProjection(db).report(flow['order'])
    assert len(report['transfers'])==1
    assert report['transfers'][0]['active'] is False
    assert report['transfers'][0]['cancellation_reason']=='Отмена потребности'
    assert report['chain_totals'][0]['original_quantity']==120
    assert report['chain_totals'][0]['open_quantity']==120


def test_chain_original_quantity_counted_once_for_three_nodes(db, flow):
    s=flow
    fact(s,'100');confirm(s,'100')
    child=transfer(db,s,'20')
    s['orders'].issue(child['order_id'])
    child_line=db.execute('SELECT id FROM work_order_lines WHERE work_order_id=?',(child['order_id'],)).fetchone()[0]
    cs=dict(s,order=child['order_id'],line=child_line)
    fact(cs,'15');confirm(cs,'15')
    grandchild=transfer(db,cs,'5','grandchild')
    s['orders'].issue(grandchild['order_id'])
    for oid in (s['order'],child['order_id'],grandchild['order_id']):
        report=WorkOrderControlProjection(db).report(oid)
        assert len(report['chain_nodes'])==3 and len(report['chain_edges'])==2
        totals=report['chain_totals'][0]
        assert (totals['original_quantity'],totals['confirmed'],totals['open_quantity'],totals['excess'])==(120,115,5,0)


def test_matching_revisions_and_late_root_correction_do_not_autotruncate(db, flow):
    s=flow
    f=fact(s,'100');c=confirm(s,'80')
    api=control(db)
    first=api.match(s['line'],f,c,'50','Частично',operation_key='match1')
    row=metric(db,s)
    assert row['matched_quantity']==50 and row['pending_acceptance'] is None
    api.match(s['line'],f,c,'80','Полностью',expected_revision=1,operation_key='match2')
    row=metric(db,s)
    assert row['pending_acceptance']==20 and not row['matching_required']
    fact(s,'30',allow_assignment_overrun=True,overrun_reason='Отдельный корень')
    s['events'].add_fact(s['item'],'-30',s['date'],correction_of_id=f,comment='Исправление',event_key='correct')
    row=metric(db,s)
    assert row['matched_quantity']==80
    assert row['conflicts'] and row['pending_acceptance'] is None
    with pytest.raises(ValidationError):
        WorkOrderControlProjection(db).assert_issue(s['order'])


def test_control_knowledge_cutoff_and_report_date_are_independent(db, flow):
    s=flow
    fact(s,'100');confirm(s,'100')
    from app.time_utils import instant
    before=instant()
    result=transfer(db,s,'20')
    row=metric(db,s,knowledge_cutoff=before)
    assert row['available']==20 and row['transferred']==0
    row=metric(db,s)
    assert row['transferred']==20
    control(db).cancel_transfer(result['transfer_id'],'Вернуть','return')
    at_transfer=db.execute('SELECT created_at FROM work_order_transfers WHERE id=?',(result['transfer_id'],)).fetchone()[0]
    assert metric(db,s,knowledge_cutoff=at_transfer)['transferred']==20
    assert metric(db,s)['transferred']==0


def test_writeoff_revoke_and_stale_ancestor_block_issue(db, flow):
    s=flow
    f=fact(s,'100');confirm(s,'100')
    api=control(db)
    resolution=api.writeoff(s['line'],'10','Исключено','writeoff')
    assert metric(db,s)['written_off']==10 and metric(db,s)['available']==10
    api.revoke_writeoff(resolution,'Возвращено','revoke')
    assert metric(db,s)['written_off']==0
    target=transfer(db,s,'20')
    fact(s,'1')
    row=metric(db,s)
    assert row['allocation_excess']==1 and row['conflicts']
    with pytest.raises(ValidationError):
        WorkOrderControlProjection(db).assert_issue(target['order_id'])


def test_zero_confirmed_after_correction_preserves_conflict_and_pending_fact(db, flow):
    s=flow
    f=fact(s,'100');c=confirm(s,'80')
    control(db).match(s['line'],f,c,'80','Сопоставлено',operation_key='match')
    s['events'].add_confirmation(s['item'],'-80',s['date'],document_ref='Отмена',comment='Исправление',correction_of_id=c,event_key='revoke-confirm')
    row=metric(db,s)
    assert row['confirmed']==0 and row['matched_quantity']==80
    assert row['pending_acceptance']==100
    assert row['conflicts']


def test_branch_transfers_and_different_work_units_keep_natural_groups(db, setup_orders):
    s=setup_orders
    order=s['orders'].save_draft(s['header'],s['lines'],s['crew'])
    s['orders'].issue(order)
    lines=db.execute('SELECT id,work_item_id FROM work_order_lines WHERE work_order_id=? ORDER BY sort_order',(order,)).fetchall()
    from app.repositories.event_repository import EventRepository
    from app.time_utils import instant
    source=dict(s,order=order,line=lines[0]['id'],item=lines[0]['work_item_id'],events=EventRepository(db),date=instant()[:10])
    fact(source,'100');confirm(source,'100')
    left=transfer(db,source,'10','left')
    right=transfer(db,source,'10','right')
    for child,quantity in [(left,'8'),(right,'7')]:
        s['orders'].issue(child['order_id'])
        lid=db.execute('SELECT id FROM work_order_lines WHERE work_order_id=?',(child['order_id'],)).fetchone()[0]
        cs=dict(source,order=child['order_id'],line=lid)
        fact(cs,quantity);confirm(cs,quantity)
    report=WorkOrderControlProjection(db).report(order)
    assert len(report['chain_totals'])==2
    first=next(r for r in report['chain_totals'] if r['catalog_id']==1)
    assert (first['original_quantity'],first['confirmed'],first['open_quantity'])==(120,115,5)
    second=next(r for r in report['chain_totals'] if r['catalog_id']!=1)
    assert second['original_quantity']==60 and second['open_quantity']==60
    assert len({(r['code'],r['unit']) for r in report['chain_totals']})==2


def test_transfer_and_closed_states_keep_independent_matching_flags(db, flow):
    from test_work_order_flow import action
    s=flow
    fact(s,'100');confirm(s,'100')
    child=transfer(db,s,'20')
    assert metric(db,s)['state_key']=='reserved'
    s['orders'].issue(child['order_id'])
    row=metric(db,s)
    assert row['state_key']=='transferred' and row['matching_required']
    action(s,'submitted');action(s,'accepted');action(s,'closed')
    assert metric(db,s)['state_key']=='closed'
    c=db.execute('SELECT id FROM confirmation_events WHERE work_order_line_id=?',(s['line'],)).fetchone()[0]
    s['events'].add_confirmation(s['item'],'-1',s['date'],document_ref='Исправление',comment='Исправление',correction_of_id=c,event_key='late-c')
    assert metric(db,s)['state_key']=='review_required'


def test_partial_transfer_state_and_nonnegative_closed_quantity(db, flow):
    from app.time_utils import instant
    s=flow
    child=transfer(db,s,'20')
    s['orders'].issue(child['order_id'])
    assert metric(db,s)['state_key']=='partial_transferred'
    f=fact(s,'10');c=confirm(s,'5')
    # SQL constraints preserve links but Python validates cumulative net. A
    # diagnostic projection still handles externally-imported invalid net safely.
    values=dict(db.execute('SELECT * FROM confirmation_events WHERE id=?',(c,)).fetchone())
    values.pop('id');values.update(quantity_delta='-30',correction_of_id=c,event_key='invalid-net',recorded_at=instant())
    db.execute('INSERT INTO confirmation_events('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')',list(values.values()))
    row=metric(db,s)
    assert row['conflicts']
    assert row['closed_quantity']==0


def test_transfer_signature_changes_on_issue_but_not_child_review_status(db, flow):
    from test_work_order_flow import action
    s=flow
    fact(s,'100');confirm(s,'100')
    target=transfer(db,s,'20')
    projection=WorkOrderControlProjection(db)
    draft_signature=projection.metrics(s['order'])[1]
    s['orders'].issue(target['order_id'])
    sent_signature=projection.metrics(s['order'])[1]
    assert sent_signature!=draft_signature
    lid=db.execute('SELECT id FROM work_order_lines WHERE work_order_id=?',(target['order_id'],)).fetchone()[0]
    child=dict(s,order=target['order_id'],line=lid)
    fact(child,'20');confirm(child,'20')
    assert projection.metrics(s['order'])[1]==sent_signature
    action(child,'submitted')
    submitted_signature=projection.metrics(child['order'])[1]
    action(child,'accepted')
    assert projection.metrics(child['order'])[1]==submitted_signature
    action(child,'closed')
    assert projection.metrics(child['order'])[1]==submitted_signature
    assert projection.metrics(s['order'])[1]==sent_signature
