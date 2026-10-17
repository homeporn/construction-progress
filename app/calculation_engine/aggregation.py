from collections import defaultdict

from app.decimal_utils import exact_sum, ratio
from .models import GroupProgress, Issue, Status

DIMENSIONS = ("building_id", "floor_id", "element", "work_type", "discipline")


def aggregate(items):
    items = list(items)
    if not items:
        return GroupProgress(status=Status.NO_DATA)
    issues = tuple(issue for item in items for issue in item.issues)
    disciplines = {item.source.discipline for item in items}
    ids = [item.source.work_item_id for item in items]
    if len(disciplines) != 1:
        issues += (Issue("MIXED_DISCIPLINES", "Проектирование и СМР рассчитываются отдельно"),)
    if len(ids) != len(set(ids)):
        issues += (Issue("DUPLICATE_ITEM", "Позиция повторно включена в группу"),)
    if any(item.status == Status.INVALID for item in items) or any(i.status == Status.INVALID for i in issues):
        return GroupProgress(status=Status.INVALID, issues=issues)
    total = exact_sum(item.planned_labor for item in items)
    if total <= 0:
        return GroupProgress(status=Status.INVALID, issues=(Issue("ZERO_PLAN", "Нулевая трудоёмкость группы"),))
    actual = exact_sum(item.actual_earned_labor for item in items)
    confirmed = exact_sum(item.confirmed_earned_labor for item in items)
    return GroupProgress(
        status=Status.WARNING if issues else Status.VALID,
        planned_labor=total, actual_earned_labor=actual,
        confirmed_earned_labor=confirmed, actual_progress=ratio(actual, total),
        confirmed_progress=ratio(confirmed, total), issues=issues,
        weights={item.source.work_item_id: ratio(item.planned_labor, total) for item in items})


def group_by(items, dimension):
    """Keys always include discipline, and floors additionally include building."""
    if dimension not in DIMENSIONS:
        raise ValueError("Unsupported aggregation dimension")
    buckets = defaultdict(list)
    for item in items:
        source = item.source
        key = (source.discipline,)
        if dimension == "floor_id":
            key += (source.building_id, source.floor_id)
        elif dimension != "discipline":
            key += (getattr(source, dimension),)
        buckets[key].append(item)
    return {key: aggregate(values) for key, values in buckets.items()}
