from decimal import Decimal
import pytest

from app.calculation_engine.historical import HistoricalCalculator
from app.calculation_engine.models import Status
from app.repositories.base import ValidationError

D = Decimal


def test_late_recorded_fact_is_absent_from_early_knowledge(db, events, item, plan):
    events.add_fact(item, "600", "2026-01-10", event_key="late", recorded_at="2026-02-10T12:00:00Z")
    calc = HistoricalCalculator(db)
    early = calc.calculate_as_of("2026-01-31", "2026-01-31T23:59:59Z", plan)
    restated = calc.calculate_as_of("2026-01-31", "2026-02-10T12:00:00Z", plan)
    assert early.total.actual_progress == D("0")
    assert restated.total.actual_progress == D("0.6")


def test_business_date_is_also_a_filter(db, events, item, plan):
    events.add_fact(item, "600", "2026-02-01", event_key="future", recorded_at="2026-02-02T12:00:00Z")
    result = HistoricalCalculator(db).calculate_as_of("2026-01-31", "2026-03-01T12:00:00Z", plan)
    assert result.total.actual_progress == 0


def test_confirmation_has_independent_business_and_registration_dates(db, events, item, plan):
    events.add_fact(item, "600", "2026-01-10", event_key="f", recorded_at="2026-01-10T12:00:00Z")
    events.add_confirmation(item, "450", "2026-01-20", event_key="c", document_ref="Акт №1",
                            recorded_at="2026-02-05T12:00:00Z")
    calc = HistoricalCalculator(db)
    assert calc.calculate_as_of("2026-01-19", "2026-02-06T12:00:00Z", plan).total.confirmed_progress == 0
    assert calc.calculate_as_of("2026-01-31", "2026-02-04T12:00:00Z", plan).total.confirmed_progress == 0
    result = calc.calculate_as_of("2026-01-31", "2026-02-05T12:00:00Z", plan)
    assert result.total.actual_progress == D("0.6")
    assert result.total.confirmed_progress == D("0.45")


def test_negative_20_correction_preserves_original_event(db, events, item, plan):
    original = events.add_fact(item, "600", "2026-01-10", event_key="f", recorded_at="2026-01-10T12:00:00Z")
    events.add_fact(item, "-20", "2026-01-10", event_key="fix", correction_of_id=original,
                    recorded_at="2026-02-01T12:00:00Z")
    calc = HistoricalCalculator(db)
    assert calc.calculate_as_of("2026-01-31", "2026-01-31T23:59:59Z", plan).items[0].source.actual == D("600")
    assert calc.calculate_as_of("2026-01-31", "2026-02-02T12:00:00Z", plan).items[0].source.actual == D("580")
    assert db.execute("SELECT quantity_delta FROM fact_events WHERE id=?", (original,)).fetchone()[0] == "600"
    assert db.execute("SELECT count(*) FROM fact_events").fetchone()[0] == 2


def test_comparing_two_dates_uses_one_baseline(db, events, item, plan):
    events.add_fact(item, "400", "2026-01-10", event_key="f1", recorded_at="2026-01-10T12:00:00Z")
    events.add_fact(item, "200", "2026-01-20", event_key="f2", recorded_at="2026-01-20T12:00:00Z")
    comparison = HistoricalCalculator(db).compare_dates("2026-01-15", "2026-01-31", "2026-02-01T12:00:00Z", plan)
    assert comparison.before.plan_version_id == comparison.after.plan_version_id == plan
    assert comparison.actual_quantity_delta[item] == D("200")
    assert comparison.actual_earned_labor_delta == D("160")
    assert comparison.before.total.actual_progress == D("0.4")
    assert comparison.after.total.actual_progress == D("0.6")


@pytest.mark.parametrize("report,cutoff", [
    ("2025-12-31", "2026-01-02T12:00:00Z"),
    ("2026-01-02", "2026-01-01T00:59:59Z"),
])
def test_unavailable_plan_is_invalid(db, plan, report, cutoff):
    result = HistoricalCalculator(db).calculate_as_of(report, cutoff, plan)
    assert result.status == Status.INVALID
    assert result.total is None
    assert result.issues[0].code == "PLAN_UNAVAILABLE"


def test_position_with_visible_events_outside_baseline_invalidates_report(db, work, events, item, plan):
    floor = db.execute("SELECT floor_id FROM work_items WHERE id=?", (item,)).fetchone()[0]
    outside = work.add_work_item(1, floor, 2)
    events.add_fact(outside, "1", "2026-01-10", event_key="outside", recorded_at="2026-01-10T12:00:00Z")
    report = HistoricalCalculator(db).calculate_as_of("2026-01-31", "2026-02-01T12:00:00Z", plan)
    assert report.status == Status.INVALID
    assert report.total.actual_progress is None
    assert any(issue.code == "MISSING_PLAN_LINE" for issue in report.issues)


def test_draft_and_missing_version_are_invalid(db, work):
    calc = HistoricalCalculator(db)
    draft = work.create_plan_version("CONSTRUCTION", created_at="2026-01-01T00:00:00Z")
    assert calc.calculate_as_of("2026-02-01", "2026-02-02T12:00:00Z", draft).status == Status.INVALID
    assert calc.calculate_as_of("2026-02-01", "2026-02-02T12:00:00Z", 999).status == Status.INVALID


def test_timezones_and_inclusive_cutoff(db, events, item, plan):
    events.add_fact(item, "1", "2026-01-10", event_key="tz", recorded_at="2026-01-10T15:00:00+03:00")
    result = HistoricalCalculator(db).calculate_as_of("2026-01-10", "2026-01-10T12:00:00Z", plan)
    assert result.items[0].source.actual == 1


def test_excess_confirmation_from_external_sql_is_invalid(db, events, item, plan):
    events.add_fact(item, "10", "2026-01-10", event_key="f", recorded_at="2026-01-10T12:00:00Z")
    db.execute("INSERT INTO confirmation_events(work_item_id,quantity_delta,confirmed_date,recorded_at,document_ref,event_key,unit) "
               "VALUES (?,'11','2026-01-10','2026-01-11T12:00:00.000000Z','import','bad','м²')", (item,))
    result = HistoricalCalculator(db).calculate_as_of("2026-01-31", "2026-02-01T12:00:00Z", plan)
    assert result.status == Status.INVALID
    assert result.total.confirmed_progress is None
