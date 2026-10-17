from decimal import Decimal
import pytest

from app.calculation_engine.models import Status
from app.calculation_engine.progress import calculate_item

D = Decimal


def test_1000_plan_600_actual_and_450_confirmed(make_input):
    result = calculate_item(make_input())
    assert result.status == Status.VALID
    assert result.actual_progress == D("0.6")
    assert result.confirmed_progress == D("0.45")
    assert result.planned_labor == D("800")
    assert result.actual_earned_labor == D("480")
    assert result.confirmed_earned_labor == D("360")


def test_overrun_is_saved_but_progress_is_capped(make_input):
    result = calculate_item(make_input(planned_quantity=D("100"), actual=D("115"), confirmed=D("110")))
    assert result.status == Status.WARNING
    assert result.source.actual == D("115")
    assert result.overrun_quantity == D("15")
    assert result.earned_quantity == D("100")
    assert result.actual_progress == result.confirmed_progress == D("1")


@pytest.mark.parametrize("overrides,code", [
    ({"labor_norm": None}, "MISSING_NORM"),
    ({"labor_norm": D("0")}, "INVALID_NORM"),
    ({"planned_quantity": D("0")}, "ZERO_PLAN"),
    ({"actual": D("-1"), "confirmed": D("0")}, "NEGATIVE_ACTUAL"),
    ({"confirmed": D("-1")}, "NEGATIVE_CONFIRMED"),
    ({"confirmed": D("601")}, "CONFIRMED_EXCEEDS_ACTUAL"),
    ({"planned_quantity": None, "in_plan": False}, "MISSING_PLAN_LINE"),
    ({"unit": "м", "plan_unit": "м²"}, "INCOMPATIBLE_UNITS"),
    ({"actual": 0.1}, "DECIMAL"),
    ({"actual": D("NaN")}, "DECIMAL"),
    ({"actual": None}, "DECIMAL"),
])
def test_invalid_input_returns_reason_and_no_percentage(make_input, overrides, code):
    result = calculate_item(make_input(**overrides))
    assert result.status == Status.INVALID
    assert result.actual_progress is None
    assert result.confirmed_progress is None
    assert code in {issue.code for issue in result.issues}


def test_no_events_means_valid_zero_progress(make_input):
    result = calculate_item(make_input(actual=D("0"), confirmed=D("0")))
    assert result.status == Status.VALID
    assert result.actual_progress == result.confirmed_progress == D("0")
