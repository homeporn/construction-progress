"""Construction control commands on real disposable SQLite, never primary side effects."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest

from app.database import connect
from app.repositories.base import ValidationError
from app.repositories.event_repository import EventRepository
from app.repositories.work_order_repository import WorkOrderRepository
from test_work_orders import setup_orders
from test_work_order_flow import flow, fact, confirm, action


@pytest.fixture
def control(flow, db):
    from app.repositories.work_order_transfer_repository import WorkOrderTransferRepository
    return dict(flow, control=WorkOrderTransferRepository(db), db=db)


def move(s, quantity='20', key='move', **options):
    header = dict(s['header'], shift_date=s['date'])
    return s['control'].transfer(s['order'], [dict(source_line_id=s['line'], quantity=quantity)],
        header, s['crew'][:1], reason='Передача оставшегося фронта', operation_key=key, **options)


def test_full_transfer_keeps_assignment_and_does_not_create_primary_events(control):
    s=control;fact(s,'100');confirm(s,'100')
    before=[tuple(r) for r in s['db'].execute('SELECT * FROM fact_events')]
    result=move(s);child=s['orders'].get_order(result['order_id'])
    assert child['status']=='draft' and child['order_number'] is None
    assert child['lines'][0]['planned_quantity']=='20'
    original=s['orders'].get_order(s['order'])['lines'][0]
    assert original['assignment']==120 and original['confirmed']==100
    assert original['transferred']==20 and original['reserved']==20 and original['available']==0
    assert [tuple(r) for r in s['db'].execute('SELECT * FROM fact_events')]==before
    assert s['db'].execute('SELECT count(*) FROM confirmation_events').fetchone()[0]==1


def test_partial_branch_reservations_are_exact_and_idempotent(control):
    s=control;fact(s,'80');confirm(s,'80')
    first=move(s,'25','part1');assert move(s,'25','part1')==first
    with pytest.raises(ValidationError):move(s,'24','part1')
    assert s['orders'].get_order(s['order'])['lines'][0]['available']==15
    move(s,'15','part2')
    assert s['orders'].get_order(s['order'])['lines'][0]['available']==0
    with pytest.raises(ValidationError):move(s,'1','part3')
    assert s['db'].execute('SELECT count(*) FROM work_order_transfers').fetchone()[0]==2


@pytest.mark.parametrize('value',['0','-1','20.000000001','NaN','Infinity',True,1.1])
def test_invalid_transfer_never_leaves_an_orphan_draft(control,value):
    s=control;fact(s,'100');confirm(s,'100')
    before=s['db'].execute('SELECT count(*) FROM work_orders').fetchone()[0]
    with pytest.raises((ValidationError,ValueError)):move(s,value)
    assert s['db'].execute('SELECT count(*) FROM work_orders').fetchone()[0]==before
    assert not s['db'].execute('SELECT 1 FROM work_order_control_commands').fetchone()


def test_transfer_never_reissues_already_executed_unconfirmed_part(control):
    s=control;fact(s,'110');confirm(s,'100')
    with pytest.raises(ValidationError):move(s,'20')
    move(s,'10')
    row=s['orders'].get_order(s['order'])['lines'][0]
    assert row['execution_remaining']==10 and row['confirmed_remaining']==20
    assert row['pending_acceptance'] is None and row['matching_required']


def test_transfer_without_linked_fact_ignores_general_journal(control):
    s=control;fact(s,'90',linked=False)
    move(s,'120')
    assert s['orders'].get_order(s['order'])['lines'][0]['actual']==0
    assert s['db'].execute('SELECT count(*) FROM fact_events').fetchone()[0]==1


def test_safe_cancel_returns_capacity_and_preserves_draft_history(control):
    s=control;fact(s,'100');confirm(s,'100');result=move(s)
    assert s['control'].cancel_transfer(result['transfer_id'],'Следующая смена отменена','cancel1')
    assert s['control'].cancel_transfer(result['transfer_id'],'Следующая смена отменена','cancel1')
    assert s['orders'].get_order(s['order'])['lines'][0]['available']==20
    assert s['orders'].get_order(result['order_id'])['status']=='cancelled'
    assert s['db'].execute('SELECT count(*) FROM work_order_transfer_lines').fetchone()[0]==1
    assert s['db'].execute('SELECT count(*) FROM work_order_transfer_cancellations').fetchone()[0]==1


def test_issued_child_and_parent_cannot_be_cancelled_around_protection(control):
    s=control;fact(s,'100');confirm(s,'100');result=move(s);s['orders'].issue(result['order_id'])
    with pytest.raises(ValidationError):s['control'].cancel_transfer(result['transfer_id'],'Нельзя уничтожить','unsafe')
    for order_id in (result['order_id'],s['order']):
        with pytest.raises(ValidationError):s['orders'].cancel(order_id,'Обход контроля')
    assert s['orders'].get_order(s['order'])['lines'][0]['sent']==20


def test_late_fact_blocks_issue_of_a_stale_reserved_child(control):
    s=control;fact(s,'100');confirm(s,'100');result=move(s)
    fact(s,'10')
    with pytest.raises(ValidationError):s['orders'].issue(result['order_id'])
    assert s['orders'].get_order(result['order_id'])['status']=='draft'
    assert s['orders'].get_order(s['order'])['lines'][0]['allocation_excess']==10
    s['control'].cancel_transfer(result['transfer_id'],'Уточнён исходный остаток','cancel-stale')
    assert s['orders'].get_order(s['order'])['lines'][0]['available']==10


def test_transferred_draft_edits_preserve_origin_line_ids(control):
    s=control;fact(s,'100');confirm(s,'100');result=move(s)
    child=s['orders'].get_order(result['order_id']);target=child['lines'][0]
    header=dict(s['header'],shift_date=s['date'],notes='Новый состав и пояснение')
    s['orders'].save_draft(header,[dict(work_item_id=s['item'],planned_quantity='20')],s['crew'][:1],order_id=child['id'],expected_revision=child['revision'])
    revised=s['orders'].get_order(child['id'])
    assert revised['lines'][0]['id']==target['id']
    with pytest.raises(ValidationError):s['orders'].save_draft(header,[dict(work_item_id=s['item'],planned_quantity='19')],s['crew'][:1],order_id=child['id'])
    assert s['orders'].get_order(child['id'])['lines'][0]['planned_quantity']=='20'


def test_two_real_connections_cannot_transfer_30_from_20(control):
    from app.repositories.work_order_transfer_repository import WorkOrderTransferRepository
    s=control;fact(s,'100');confirm(s,'100');barrier=Barrier(2)
    path=s['db'].execute('PRAGMA database_list').fetchone()[2]
    def run(index):
        db=connect(path)
        try:
            local=dict(s,control=WorkOrderTransferRepository(db))
            barrier.wait(timeout=10)
            try:return move(local,'15','race-'+str(index))
            except ValidationError as exc:return exc
        finally:db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[1,2]))
    assert sum(isinstance(r,dict) for r in results)==1
    assert sum(isinstance(r,ValidationError) for r in results)==1
    assert s['orders'].get_order(s['order'])['lines'][0]['available']==5


def test_exact_matching_and_late_correction_are_separate_metadata(control):
    s=control;f=fact(s,'100');c=confirm(s,'80')
    assert s['orders'].get_order(s['order'])['lines'][0]['pending_acceptance'] is None
    s['control'].match(s['line'],f,c,'80','Явное сопоставление документов',operation_key='match')
    assert s['orders'].get_order(s['order'])['lines'][0]['pending_acceptance']==20
    with pytest.raises(ValidationError):s['control'].match(s['line'],f,c,'81','Ошибка',expected_revision=1,operation_key='match-over')
    s['events'].add_confirmation(s['item'],'-10',s['date'],document_ref='Уточнение',event_key='matching-correction',correction_of_id=c,comment='Уточнение')
    assert s['orders'].get_order(s['order'])['lines'][0]['conflicts']


def test_explicit_resolution_and_review_are_required_for_close(control):
    s=control;fact(s,'100');confirm(s,'100');action(s,'submitted');action(s,'accepted')
    with pytest.raises(ValidationError):action(s,'closed',comment='Просто текст не переносит20')
    s['control'].writeoff(s['line'],'20','Управленческая отмена фронта','resolve')
    with pytest.raises(ValidationError):action(s,'closed',comment='Нужна повторная проверка')
    action(s,'accepted');action(s,'closed')
    assert s['orders'].get_order(s['order'])['status']=='closed'
    assert s['db'].execute('SELECT count(*) FROM fact_events').fetchone()[0]==1


def test_three_shift_chain_keeps_initial_120_confirmed_115_open_5(control):
    from app.services.work_order_control import WorkOrderControlProjection
    s=control;fact(s,'100');confirm(s,'100');first=move(s);s['orders'].issue(first['order_id'])
    child=s['orders'].get_order(first['order_id']);child_line=child['lines'][0]['id']
    events=EventRepository(s['db'])
    events.add_fact(s['item'],'15',s['date'],event_key='child-fact',work_order_line_id=child_line)
    events.add_confirmation(s['item'],'15',s['date'],event_key='child-confirmation',document_ref='Акт дочерней смены',work_order_line_id=child_line)
    second=s['control'].transfer(child['id'],[dict(source_line_id=child_line,quantity='5')],dict(s['header'],shift_date=s['date']),s['crew'][:1],reason='Следующая смена',operation_key='next')
    totals=WorkOrderControlProjection(s['db']).report(s['order'])['chain_totals']
    assert len(totals)==1
    assert (totals[0]['original_quantity'],totals[0]['confirmed'],totals[0]['open_quantity'])==(Decimal(120),Decimal(115),Decimal(5))
    assert s['orders'].get_order(second['order_id'])['status']=='draft'


def test_direct_draft_cancellation_cannot_leave_hidden_reservation(control):
    s=control;fact(s,'100');confirm(s,'100');result=move(s)
    s['orders'].cancel(result['order_id'],'Отмена через прежнюю карточку')
    assert s['orders'].get_order(s['order'])['lines'][0]['available']==20
    assert s['db'].execute('SELECT count(*) FROM work_order_transfer_cancellations').fetchone()[0]==1


def test_writeoff_reversal_and_fully_disposed_nofact_lifecycle(control):
    s=control
    writeoff=s['control'].writeoff(s['line'],'120','Рабочий фронт полностью отменён','all-writeoff')
    assert s['orders'].get_order(s['order'])['lines'][0]['available']==0
    s['control'].revoke_writeoff(writeoff,'Решение отменено','undo-writeoff')
    assert s['orders'].get_order(s['order'])['lines'][0]['available']==120
    with pytest.raises(ValidationError):s['control'].revoke_writeoff(writeoff,'Повторная отмена','undo-again')
    s['control'].writeoff(s['line'],'120','Повторно отменён с новым основанием','new-writeoff')
    action(s,'submitted');action(s,'accepted');action(s,'closed')
    assert s['orders'].get_order(s['order'])['status']=='closed'
    assert [r['new_status'] for r in s['orders'].get_order(s['order'])['history']]==['draft','issued','in_progress','submitted','accepted','closed']
    for table in ('fact_events','confirmation_events','timesheet_entries','labor_hour_allocations','payroll_accruals'):
        assert s['db'].execute('SELECT count(*) FROM '+table).fetchone()[0]==0


def test_confirmed_order_without_linked_fact_can_finish_via_issued_transfer(control):
    s=control;fact(s,'100',linked=False);confirm(s,'100')
    result=move(s);s['orders'].issue(result['order_id'])
    action(s,'submitted');action(s,'accepted');action(s,'closed')
    row=s['orders'].get_order(s['order'])['lines'][0]
    assert row['actual']==0 and row['confirmed']==100 and row['sent']==20
    assert s['db'].execute('SELECT count(*) FROM fact_events').fetchone()[0]==1


def test_draft_reservation_needs_issuance_before_parent_close(control):
    s=control;fact(s,'100');confirm(s,'100');result=move(s)
    action(s,'submitted');action(s,'accepted')
    with pytest.raises(ValidationError):action(s,'closed',comment='Черновик ещё не является выданным поручением')
    s['orders'].issue(result['order_id'])
    with pytest.raises(ValidationError):action(s,'closed')  # Child status changed the reviewed signature.
    action(s,'accepted');action(s,'closed')


def test_transfer_transaction_rolls_back_draft_crew_and_command_after_late_error(control,monkeypatch):
    s=control;fact(s,'100');confirm(s,'100')
    counts={table:s['db'].execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('work_orders','work_order_lines','work_order_crew','work_order_crew_members','work_order_status_history')}
    original=s['control']._executor
    def fail_on_target(order_id):
        if order_id!=s['order']:raise ValidationError('Искусственный сбой после создания черновика')
        return original(order_id)
    monkeypatch.setattr(s['control'],'_executor',fail_on_target)
    with pytest.raises(ValidationError):move(s)
    assert counts=={table:s['db'].execute('SELECT count(*) FROM '+table).fetchone()[0] for table in counts}
    assert not s['db'].execute('SELECT 1 FROM work_order_control_commands').fetchone()
    assert not s['db'].execute('SELECT 1 FROM work_order_transfers').fetchone()


def test_new_plan_exclusion_cannot_be_bypassed_by_transfer(control):
    s=control
    plan=s['work'].create_plan_version('CONSTRUCTION',base_version_id=s['plan'])
    s['work'].remove_plan_line(plan,s['item']);s['work'].approve_plan_version(plan)
    with pytest.raises(ValidationError):move(s,'120')
    assert s['db'].execute('SELECT count(*) FROM work_orders').fetchone()[0]==1
    assert not s['db'].execute('SELECT 1 FROM work_order_transfers').fetchone()


def test_control_scope_and_original_issued_quantity_cannot_be_rewritten(control):
    import sqlite3
    from app.repositories.project_repository import ProjectRepository
    from app.repositories.work_order_transfer_repository import WorkOrderTransferRepository
    s=control;fact(s,'100');confirm(s,'100');result=move(s)
    other=ProjectRepository(s['db']).create_project('Иной объект4.5','OTHER45')
    foreign=WorkOrderTransferRepository(s['db'],other)
    with pytest.raises(ValidationError):foreign.cancel_transfer(result['transfer_id'],'Чужая операция','foreign-cancel')
    with pytest.raises(ValidationError):foreign.writeoff(s['line'],'1','Чужая строка','foreign-writeoff')
    target=s['orders'].get_order(result['order_id'])['lines'][0]['id']
    with pytest.raises(sqlite3.IntegrityError):s['db'].execute("UPDATE work_order_lines SET planned_quantity='19' WHERE id=?",(target,))
    for table in ('work_order_transfers','work_order_transfer_lines','work_order_control_commands'):
        with pytest.raises(sqlite3.IntegrityError):s['db'].execute('DELETE FROM '+table)
    assert s['orders'].get_order(s['order'])['lines'][0]['assignment']==120


def test_full_overrun_has_zero_available_and_explicit_review(control):
    s=control;fact(s,'130',allow_assignment_overrun=True,overrun_reason='Обмер показал превышение');confirm(s,'130')
    row=s['orders'].get_order(s['order'])['lines'][0]
    assert row['available']==0 and row['overrun']==10 and row['confirmed_overrun']==10
    with pytest.raises(ValidationError):move(s,'1')
    action(s,'submitted')
    with pytest.raises(ValidationError):action(s,'accepted',checks={s['line']:dict(checked=True,comment='')})
    action(s,'accepted',checks={s['line']:dict(checked=True,comment='Превышение обмерено и отдельно проверено')})
    action(s,'closed')


def test_workflow_cannot_backdate_close_before_later_control_reversal(control):
    from app.time_utils import instant
    s=control;fact(s,'100');confirm(s,'100')
    resolution=s['control'].writeoff(s['line'],'20','Первое решение','decision-first')
    action(s,'submitted');action(s,'accepted');earlier=instant()
    s['control'].revoke_writeoff(resolution,'Возврат фронта в работу','decision-reversed')
    with pytest.raises(ValidationError):action(s,'closed',performed_at=earlier)
    assert s['orders'].get_order(s['order'])['status']=='accepted'
