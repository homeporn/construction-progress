from decimal import Decimal
import pytest

from app.database import initialize
from app.models import Discipline
from app.calculation_engine.models import WorkInput
from app.repositories.event_repository import EventRepository
from app.repositories.work_repository import WorkRepository


@pytest.fixture
def db(tmp_path):
    connection = initialize(tmp_path / "test.sqlite3")
    yield connection
    connection.close()


@pytest.fixture
def work(db):
    return WorkRepository(db)


@pytest.fixture
def events(db):
    return EventRepository(db)


@pytest.fixture
def item(work):
    floor = work.add_floor(1, "1", 1)
    return work.add_work_item(1, floor, 1)


@pytest.fixture
def plan(work, item):
    version = work.create_plan_version(Discipline.CONSTRUCTION, created_at="2026-01-01T00:00:00Z")
    work.add_plan_line(version, item, "1000", "0.8")
    work.approve_plan_version(version, approved_at="2026-01-01T01:00:00Z")
    return version


@pytest.fixture
def make_input():
    def make(**overrides):
        values = dict(work_item_id=1, discipline=Discipline.CONSTRUCTION,
                      planned_quantity=Decimal("1000"), labor_norm=Decimal("0.8"),
                      actual=Decimal("600"), confirmed=Decimal("450"),
                      building_id=1, floor_id=1, element="Стены", work_type="штукатурка")
        values.update(overrides)
        return WorkInput(**values)
    return make
