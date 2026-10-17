from decimal import Decimal
import sqlite3
import pytest

from app.calculation_engine.historical import HistoricalCalculator
from app.repositories.base import ValidationError

D = Decimal


def test_v1_60_percent_v2_50_percent_and_old_plan_unchanged(db, work, events, item, plan):
    old_lines = [tuple(row) for row in db.execute("SELECT * FROM plan_lines WHERE plan_version_id=?", (plan,))]
    events.add_fact(item, "600", "2026-01-10", event_key="actual", recorded_at="2026-01-10T12:00:00Z")
    version2 = work.create_plan_version("CONSTRUCTION", base_version_id=plan, created_at="2026-02-01T00:00:00Z")
    work.update_plan_line(version2, item, "1200", "0.8")
    work.approve_plan_version(version2, approved_at="2026-02-01T01:00:00Z")
    calculator = HistoricalCalculator(db)
    assert calculator.calculate_as_of("2026-02-10", "2026-02-10T12:00:00Z", plan).total.actual_progress == D("0.6")
    assert calculator.calculate_as_of("2026-02-10", "2026-02-10T12:00:00Z", version2).total.actual_progress == D("0.5")
    assert [tuple(row) for row in db.execute("SELECT * FROM plan_lines WHERE plan_version_id=?", (plan,))] == old_lines
    changes = work.compare_plan_versions(plan, version2)[item]
    assert changes["planned_quantity_delta"] == D("200")
    assert changes["planned_labor_delta"] == D("160")


def test_approved_plan_rejects_repository_edits(work, item, plan):
    with pytest.raises(ValidationError):
        work.update_plan_line(plan, item, "1200", "1")
    with pytest.raises(ValidationError):
        work.add_plan_line(plan, item, "1200", "1")
    with pytest.raises(ValidationError):
        work.approve_plan_version(plan)


@pytest.mark.parametrize("sql", [
    "UPDATE plan_versions SET status='DRAFT',approved_at=NULL WHERE id=?",
    "UPDATE plan_versions SET version_no=50 WHERE id=?",
    "DELETE FROM plan_versions WHERE id=?",
    "UPDATE plan_lines SET planned_quantity='1200' WHERE plan_version_id=?",
    "DELETE FROM plan_lines WHERE plan_version_id=?",
])
def test_approved_plan_is_protected_from_raw_sql(db, plan, sql):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, (plan,))


def test_active_version_respects_date_and_knowledge(work, item, plan):
    version2 = work.create_plan_version("CONSTRUCTION", base_version_id=plan, created_at="2026-02-01T00:00:00Z")
    work.approve_plan_version(version2, approved_at="2026-02-01T01:00:00Z")
    assert work.get_active_plan_version("CONSTRUCTION")["id"] == version2
    assert work.get_active_plan_version("CONSTRUCTION", report_date="2026-01-31")["id"] == plan
    assert work.get_active_plan_version("CONSTRUCTION", knowledge_cutoff="2026-01-31T23:59:59Z")["id"] == plan
    assert work.get_active_plan_version("DESIGN") is None


@pytest.mark.parametrize("quantity,norm", [("0", "1"), ("100", None), ("100", "0")])
def test_incomplete_draft_cannot_be_approved(work, item, quantity, norm):
    version = work.create_plan_version("CONSTRUCTION")
    work.add_plan_line(version, item, quantity, norm)
    with pytest.raises(ValidationError):
        work.approve_plan_version(version)


def test_empty_draft_cannot_be_approved(work):
    version = work.create_plan_version("CONSTRUCTION")
    with pytest.raises(ValidationError):
        work.approve_plan_version(version)


def test_duplicate_lines_and_mixed_disciplines_rejected(work, item):
    version = work.create_plan_version("CONSTRUCTION")
    work.add_plan_line(version, item, "1000", "1")
    with pytest.raises(ValidationError):
        work.add_plan_line(version, item, "1000", "1")
    design = work.create_plan_version("DESIGN")
    with pytest.raises(ValidationError):
        work.add_plan_line(design, item, "1000", "1")
    with pytest.raises(ValidationError):
        work.compare_plan_versions(version, design)


def test_plan_unit_mismatch_rejected(work, item):
    version = work.create_plan_version("CONSTRUCTION")
    with pytest.raises(ValidationError):
        work.add_plan_line(version, item, "1000", "1", unit="шт")


def test_labor_norm_change_is_fixed_in_new_version(work, item, plan):
    version2 = work.create_plan_version("CONSTRUCTION", base_version_id=plan)
    work.update_plan_line(version2, item, "1000", "1.2")
    changes = work.compare_plan_versions(plan, version2)[item]
    assert changes["labor_norm_delta"] == D("0.4")
    assert changes["planned_labor_delta"] == D("400")
