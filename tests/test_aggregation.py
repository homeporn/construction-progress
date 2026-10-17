from decimal import Decimal

from app.calculation_engine.aggregation import aggregate, group_by
from app.calculation_engine.models import Status
from app.calculation_engine.progress import calculate_item
from app.models import Discipline

D = Decimal


def test_weighted_800_and_200_labor_gives_60_percent(make_input):
    items = [calculate_item(make_input(planned_quantity=D("1000"), labor_norm=D("0.8"),
                                       actual=D("500"), confirmed=D("400"))),
             calculate_item(make_input(work_item_id=2, floor_id=2, planned_quantity=D("100"),
                                       labor_norm=D("2"), actual=D("100"), confirmed=D("100")))]
    result = aggregate(items)
    assert result.actual_progress == D("0.6")
    assert result.confirmed_progress == D("0.52")
    assert result.weights == {1: D("0.8"), 2: D("0.2")}
    assert result.planned_labor == D("1000")
    floors = group_by(items, "floor_id")
    assert sorted(x.actual_progress for x in floors.values()) == [D("0.5"), D("1")]


def test_empty_group_is_no_data():
    result = aggregate([])
    assert result.status == Status.NO_DATA
    assert result.actual_progress is None


def test_invalid_item_invalidates_entire_group(make_input):
    result = aggregate([calculate_item(make_input()),
                        calculate_item(make_input(work_item_id=2, labor_norm=None))])
    assert result.status == Status.INVALID
    assert result.actual_progress is None


def test_design_and_construction_never_have_a_joint_percentage(make_input):
    items = [calculate_item(make_input()),
             calculate_item(make_input(work_item_id=2, discipline=Discipline.DESIGN))]
    combined = aggregate(items)
    assert combined.status == Status.INVALID
    assert combined.actual_progress is None
    assert "MIXED_DISCIPLINES" in {x.code for x in combined.issues}
    for dimension in ("building_id", "floor_id", "element", "work_type", "discipline"):
        groups = group_by(items, dimension)
        assert len(groups) == 2
        assert {key[0] for key in groups} == {Discipline.DESIGN, Discipline.CONSTRUCTION}
        assert all(value.status == Status.VALID for value in groups.values())


def test_duplicate_position_cannot_inflate_weight(make_input):
    item = calculate_item(make_input())
    assert aggregate([item, item]).status == Status.INVALID


def test_warning_overrun_propagates_to_group(make_input):
    result = aggregate([calculate_item(make_input(actual=D("1100")))])
    assert result.status == Status.WARNING
    assert result.actual_progress == D("1")
