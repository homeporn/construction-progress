from app.decimal_utils import ZERO, ratio, subtract
from .labor import planned_labor, earned_labor
from .models import ItemProgress, Status
from .validation import validate


def calculate_item(item):
    issues = validate(item)
    if any(i.status == Status.INVALID for i in issues):
        return ItemProgress(status=Status.INVALID, source=item, issues=issues)
    earned = min(max(item.actual, ZERO), item.planned_quantity)
    confirmed = min(max(item.confirmed, ZERO), item.planned_quantity)
    total = planned_labor(item.planned_quantity, item.labor_norm)
    actual_labor = earned_labor(earned, item.labor_norm)
    confirmed_labor = earned_labor(confirmed, item.labor_norm)
    return ItemProgress(
        status=Status.WARNING if issues else Status.VALID,
        planned_labor=total, actual_earned_labor=actual_labor,
        confirmed_earned_labor=confirmed_labor,
        actual_progress=ratio(actual_labor, total),
        confirmed_progress=ratio(confirmed_labor, total),
        issues=issues, source=item, earned_quantity=earned,
        confirmed_earned_quantity=confirmed,
        overrun_quantity=max(subtract(item.actual, item.planned_quantity), ZERO))
