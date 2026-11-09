"""Independent review regressions for control decisions and ancestor safety."""
import pytest

from app.repositories.base import ValidationError
from app.repositories.work_order_transfer_repository import WorkOrderTransferRepository
from app.services.work_order_control import WorkOrderControlProjection
from app.time_utils import instant
from test_work_orders import setup_orders
from test_work_order_flow import flow, fact, confirm, action


def completed_child(db, flow):
    source = flow
    original_fact = fact(source, '100')
    confirm(source, '100')
    result = WorkOrderTransferRepository(db).transfer(
        source['order'], [dict(source_line_id=source['line'], quantity='20')],
        source['header'], source['crew'][:1], reason='Следующая смена',
        operation_key='review-transfer')
    source['orders'].issue(result['order_id'])
    child_line = db.execute('SELECT id FROM work_order_lines WHERE work_order_id=?',
                            (result['order_id'],)).fetchone()[0]
    child = dict(source, order=result['order_id'], line=child_line)
    fact(child, '20')
    confirm(child, '20')
    action(child, 'submitted')
    action(child, 'accepted')
    return source, child, original_fact


def test_transferred_child_can_close_after_one_unchanged_acceptance(db, flow):
    source, child, _ = completed_child(db, flow)
    action(child, 'closed')
    assert source['orders'].get_order(child['order'])['status'] == 'closed'


def test_backdated_child_close_cannot_hide_later_ancestor_allocation_conflict(db, flow):
    source, child, original_fact = completed_child(db, flow)
    # Repeat acceptance avoids masking this independent historical safety check
    # with the incoming-transfer status/signature defect.
    action(child, 'accepted')
    before_correction = instant()
    source['events'].add_fact(source['item'], '1', source['date'],
        correction_of_id=original_fact, comment='Уточнение исходного обмера',
        event_key='review-ancestor-correction')
    assert WorkOrderControlProjection(db).closure_errors(child['order'])
    with pytest.raises(ValidationError):
        action(child, 'closed', performed_at=before_correction)
    assert source['orders'].get_order(child['order'])['status'] == 'accepted'


def test_historical_reservation_does_not_gain_future_target_number_or_signature(db, flow):
    source = flow
    result = WorkOrderTransferRepository(db).transfer(
        source['order'], [dict(source_line_id=source['line'], quantity='20')],
        source['header'], source['crew'][:1], reason='Следующая смена',
        operation_key='review-historical-transfer')
    cutoff = instant()
    projection = WorkOrderControlProjection(db)
    before_signature = projection.metrics(source['order'], knowledge_cutoff=cutoff)[1]
    assert projection.report(source['order'], knowledge_cutoff=cutoff)['transfers'][0]['target_order_number'] is None
    source['orders'].issue(result['order_id'])
    historical = projection.report(source['order'], knowledge_cutoff=cutoff)
    assert historical['transfers'][0]['status'] == 'draft'
    assert historical['transfers'][0]['target_order_number'] is None
    assert projection.metrics(source['order'], knowledge_cutoff=cutoff)[1] == before_signature
