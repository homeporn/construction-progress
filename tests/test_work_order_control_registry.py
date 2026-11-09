from decimal import Decimal

from app.repositories.work_order_control_repository import WorkOrderControlRepository
from test_work_orders import setup_orders


def test_disposition_is_not_reported_as_performed_work(db,setup_orders):
    from app.repositories.work_order_transfer_repository import WorkOrderTransferRepository
    s=setup_orders;commands=WorkOrderTransferRepository(db)
    first=s['orders'].save_draft(s['header'],s['lines'][:1],s['crew'][:1]);s['orders'].issue(first)
    first_line=s['orders'].get_order(first)['lines'][0]['id']
    commands.writeoff(first_line,'120','Отменён весь фронт','count-writeoff')
    second=s['orders'].save_draft(s['header'],s['lines'][:1],s['crew'][:1]);s['orders'].issue(second)
    second_line=s['orders'].get_order(second)['lines'][0]['id']
    commands.transfer(second,[dict(source_line_id=second_line,quantity='20')],s['header'],s['crew'][:1],reason='Часть поручения передана',operation_key='count-transfer')
    data=WorkOrderControlRepository(db).analytics()
    assert data['counts']['full']==1  # Explicitly disposed, not performed.
    assert data['counts']['executed']==0
    assert data['counts']['confirmed']==0
    assert data['counts']['partial']==0


def test_registry_uses_sql_count_and_bounded_headers(db, setup_orders):
    s = setup_orders
    for _ in range(51):
        s['orders'].save_draft(s['header'], s['lines'][:1], s['crew'][:1])
    queries = []
    db.set_trace_callback(queries.append)
    try:
        first = WorkOrderControlRepository(db).registry()
        second = WorkOrderControlRepository(db).registry(page=2)
    finally:
        db.set_trace_callback(None)
    assert (first['total'], first['pages'], len(first['rows']), len(second['rows'])) == (51, 2, 50, 1)
    assert first['rows'][0]['volumes'][0]['assignment'] == Decimal('120')
    assert set(r['id'] for r in first['rows']).isdisjoint(r['id'] for r in second['rows'])
    assert any('COUNT(' in q.upper() for q in queries)
    assert any('LIMIT 50 OFFSET 50' in q.upper() for q in queries)


def test_registry_structural_filters_and_project_scope(db, setup_orders):
    s = setup_orders
    first = s['orders'].save_draft(s['header'], s['lines'], s['crew'])
    s['orders'].issue(first)
    second = s['orders'].save_draft(s['header'], s['lines'][:1], s['crew'][:1])
    repo = WorkOrderControlRepository(db)
    assert [r['id'] for r in repo.registry({'search': 'нз-0001'})['rows']] == [first]
    assert [r['id'] for r in repo.registry({'status': 'draft'})['rows']] == [second]
    assert repo.registry({'building_id': 3})['total'] == 0
    assert repo.registry({'contractor_id': s['contractor'], 'date_from': '2026-10-17', 'date_to': '2026-10-17'})['total'] == 2
    assert WorkOrderControlRepository(db, 999).registry()['total'] == 0


def test_registry_exact_derived_filters_and_analytics_chain_totals(db, setup_orders):
    from app.repositories.event_repository import EventRepository
    from app.repositories.work_order_transfer_repository import WorkOrderTransferRepository
    from app.time_utils import instant
    s=setup_orders
    parent=s['orders'].save_draft(s['header'],s['lines'][:1],s['crew'][:1])
    s['orders'].issue(parent)
    line=db.execute('SELECT id FROM work_order_lines WHERE work_order_id=?',(parent,)).fetchone()[0]
    ev=EventRepository(db)
    ev.add_fact(s['items'][0],'100',instant()[:10],work_order_line_id=line,event_key='f')
    ev.add_confirmation(s['items'][0],'100',instant()[:10],document_ref='Акт',work_order_line_id=line,event_key='c')
    child=WorkOrderTransferRepository(db).transfer(parent,[dict(source_line_id=line,quantity='20')],s['header'],s['crew'][:1],reason='Остаток',operation_key='t')
    s['orders'].issue(child['order_id'])
    repo=WorkOrderControlRepository(db)
    assert repo.registry({'has_transfer':True})['total']==2
    assert repo.registry({'unclosed':True})['total']==2
    assert repo.registry({'unclosed':False})['rows']==[]
    assert repo.registry({'has_remaining':True})['total']==1
    assert repo.registry({'discrepancies':True})['rows'][0]['id']==parent
    result=repo.analytics()
    assert result['by_work'][0]['original_quantity']==120
    assert result['by_work'][0]['open_quantity']==20
    assert result['chain_rows'][0]['confirmed']==100
    assert result['counts']['overdue'] is None
    assert len(result['source_rows'])==2
    assert len(result['transfers'])==1
    assert sum(r['assignment'] for r in result['dynamics'])==120
    assert result['counts']['remaining_lines']==1


def test_large_projection_batches_respect_legacy_sqlite_variable_limit(db, setup_orders):
    import sqlite3
    s=setup_orders
    # SQLite3.24 (supported on Catalina) permits only999 parameters per query.
    db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,999)
    for _ in range(1001):
        s['orders'].save_draft(s['header'],s['lines'][:1],s['crew'][:1])
    repo=WorkOrderControlRepository(db)
    queries=[]
    db.set_trace_callback(queries.append)
    try:
        data=repo.analytics()
    finally:
        db.set_trace_callback(None)
    assert len(data['rows'])==1001
    assert data['by_work'][0]['original_quantity']==Decimal('120120')
    assert len(data['chain_rows'])==1001
    # No SELECT per order/worker: batch reads remain comfortably bounded.
    assert len([q for q in queries if q.lstrip().upper().startswith(('SELECT','WITH'))])<300


def test_brigade_filters_use_historical_snapshots_without_duplicating_volume(db, setup_orders):
    from app.repositories.brigade_repository import BrigadeRepository
    s=setup_orders
    brigades=BrigadeRepository(db)
    first=brigades.save(s['contractor'],'Б1')
    second=brigades.save(s['contractor'],'Б2')
    brigades.assign(s['teams'][0][0],first,'2026-01-01')
    brigades.assign(s['teams'][0][1],second,'2026-01-01')
    order=s['orders'].save_draft(s['header'],s['lines'][:1],s['crew'][:1])
    s['orders'].issue(order)
    brigades.save(s['contractor'],'Переименовано',first)
    repo=WorkOrderControlRepository(db)
    result=repo.registry({'brigade_id':first})
    assert result['total']==1
    assert result['rows'][0]['brigades']==['Б1','Б2']
    analytical=repo.analytics()
    assert sum(r['original_quantity'] for r in analytical['by_brigade'])==120
    assert analytical['by_brigade'][0]['brigade']=='Б1, Б2'


def test_unclosed_is_historical_status_filter_and_includes_empty_draft(db, setup_orders):
    from test_work_order_flow import fact,confirm,action
    from app.repositories.event_repository import EventRepository
    from app.repositories.work_order_workflow_repository import WorkOrderWorkflowRepository
    from app.time_utils import instant
    s=setup_orders
    order=s['orders'].save_draft(s['header'],s['lines'][:1],s['crew'][:1])
    s['orders'].issue(order)
    line=db.execute('SELECT id FROM work_order_lines WHERE work_order_id=?',(order,)).fetchone()[0]
    fs=dict(s,order=order,line=line,item=s['items'][0],date=instant()[:10],events=EventRepository(db),workflow=WorkOrderWorkflowRepository(db))
    fact(fs,'120');confirm(fs,'120');action(fs,'submitted');action(fs,'accepted')
    accepted_at=instant()
    empty=s['orders'].save_draft(s['header'],[],[])
    cancelled=s['orders'].save_draft(s['header'],[],[])
    s['orders'].cancel(cancelled,'Отменён')
    repo=WorkOrderControlRepository(db)
    queries=[]
    db.set_trace_callback(queries.append)
    try:
        assert {r['id'] for r in repo.registry({'unclosed':True})['rows']}=={order,empty}
    finally:
        db.set_trace_callback(None)
    assert any("h.asof_status NOT IN ('closed','cancelled')" in q for q in queries)
    assert not any('LIMIT 200 OFFSET' in q for q in queries)
    assert [r['id'] for r in repo.registry({'unclosed':False})['rows']]==[cancelled]
    action(fs,'closed')
    assert {r['id'] for r in repo.registry({'unclosed':False})['rows']}=={order,cancelled}
    assert repo.registry({'unclosed':True},knowledge_cutoff=accepted_at)['rows'][0]['id']==order


def test_current_remaining_and_open_counts_do_not_recount_disposed_parent(db, setup_orders):
    from app.repositories.work_order_transfer_repository import WorkOrderTransferRepository
    from app.repositories.work_order_workflow_repository import WorkOrderWorkflowRepository
    from test_work_order_flow import action
    s=setup_orders
    parent=s['orders'].save_draft(s['header'],s['lines'][:1],s['crew'][:1])
    s['orders'].issue(parent)
    line=db.execute('SELECT id FROM work_order_lines WHERE work_order_id=?',(parent,)).fetchone()[0]
    child=WorkOrderTransferRepository(db).transfer(parent,[dict(source_line_id=line,quantity='120')],s['header'],s['crew'][:1],reason='Весь остаток',operation_key='all')
    s['orders'].issue(child['order_id'])
    fs=dict(s,order=parent,line=line,workflow=WorkOrderWorkflowRepository(db))
    action(fs,'submitted');action(fs,'accepted');action(fs,'closed')
    ordinary_draft=s['orders'].save_draft(s['header'],[],[])
    repo=WorkOrderControlRepository(db)
    assert [r['id'] for r in repo.registry({'has_remaining':True})['rows']]==[child['order_id']]
    result=repo.analytics()
    assert result['counts']['remaining_lines']==1
    assert result['counts']['open']==1
    assert result['counts']['draft']==1


def test_daily_dynamics_uses_primary_business_dates_not_order_shift(db, setup_orders):
    from app.repositories.event_repository import EventRepository
    from app.time_utils import instant
    s=setup_orders
    order=s['orders'].save_draft(s['header'],s['lines'][:1],s['crew'][:1])
    s['orders'].issue(order)
    line=db.execute('SELECT id FROM work_order_lines WHERE work_order_id=?',(order,)).fetchone()[0]
    events=EventRepository(db)
    events.add_fact(s['items'][0],'10',instant()[:10],work_order_line_id=line,event_key='late-fact')
    events.add_confirmation(s['items'][0],'7',instant()[:10],document_ref='Акт',work_order_line_id=line,event_key='late-confirm')
    result=WorkOrderControlRepository(db).analytics()
    by_day={r['shift_date']:r for r in result['dynamics']}
    assert by_day[s['header']['shift_date']]['assignment']==120
    assert by_day[s['header']['shift_date']]['actual']==0
    assert by_day[instant()[:10]]['actual']==10
    assert by_day[instant()[:10]]['confirmed']==7
    assert by_day[instant()[:10]]['assignment']==0
    assert result['counts']['open']==1  # in_progress after a partial linked fact
    fid=db.execute('SELECT id FROM fact_events WHERE event_key=?',('late-fact',)).fetchone()[0]
    events.add_fact(s['items'][0],'-2',instant()[:10],correction_of_id=fid,comment='Исправление',event_key='daily-correction')
    corrected=WorkOrderControlRepository(db).analytics()
    assert next(r for r in corrected['dynamics'] if r['shift_date']==instant()[:10])['actual']==8
