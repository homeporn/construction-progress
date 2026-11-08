"""Writer ordering must also order the knowledge timestamps used for validation."""
from contextlib import contextmanager
from decimal import Decimal
from threading import Event, Thread, current_thread

import pytest

from app.database import connect
from app.models import Discipline
from app.repositories.base import ValidationError
from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.event_repository import EventRepository
from app.repositories.labor_repository import LaborRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.timesheet_repository import TimesheetRepository
import app.repositories.labor_repository as labor_module


@pytest.fixture
def concurrent_labor_case(db, work, item):
    other_item = work.add_work_item(1, work.add_floor(1, '2', 2), 1)
    version = work.create_plan_version(
        Discipline.CONSTRUCTION, created_at='2026-01-01T00:00:00Z')
    for work_item in (item, other_item):
        work.add_plan_line(version, work_item, '1000', '0.8')
    work.approve_plan_version(version, approved_at='2026-01-01T01:00:00Z')
    people = PersonnelRepository(db)
    contractor = people.save_contractor({'name': 'Concurrent labor contractor'})
    specialty = people.save_specialty('Concurrent labor specialty', 'CL43')
    employee = people.save_worker(dict(
        full_name='Concurrent labor worker', contractor_id=contractor,
        specialty_id=specialty, effective_date='2026-01-01'))
    brigade = BrigadeRepository(db).save(contractor, '01')
    timesheets = TimesheetRepository(db)
    sheet = timesheets.create(contractor, 2026, 10)
    timesheets.set_entries(sheet, [dict(
        employee_id=employee, work_date='2026-10-01', code='Я', hours='8')])
    timesheets.transition(sheet, 'submit')
    timesheets.transition(sheet, 'approve')
    root = EventRepository(db).add_fact(
        item, '100', '2026-10-01', event_key='concurrent-root',
        recorded_at='2026-10-02T00:00:00Z')
    return dict(db=db, item=item, other_item=other_item, employee=employee,
                contractor=contractor, brigade=brigade, root=root,
                path=db.execute('PRAGMA database_list').fetchone()[2])


@pytest.mark.parametrize('kind', ['hours', 'volume'])
def test_automatic_knowledge_time_is_sampled_after_writer_lock(
        concurrent_labor_case, monkeypatch, kind):
    case = concurrent_labor_case
    paused, resume = Event(), Event()
    failures, results = [], []

    def pause_first_writer():
        if current_thread().name == 'labor-first-writer':
            paused.set()
            if not resume.wait(10):
                raise RuntimeError('Second writer did not finish')

    if kind == 'hours':
        original_hours = labor_module.hours_value

        def prepare_hours(value):
            pause_first_writer()
            return original_hours(value)

        monkeypatch.setattr(labor_module, 'hours_value', prepare_hours)
    else:
        original_transaction = labor_module.transaction

        @contextmanager
        def wait_before_transaction(connection):
            pause_first_writer()
            with original_transaction(connection):
                yield

        monkeypatch.setattr(labor_module, 'transaction', wait_before_transaction)

    def assign(repository, first):
        if kind == 'hours':
            return repository.allocate(
                case['item'] if first else case['other_item'], '2026-10-01',
                [dict(employee_id=case['employee'], hours='8')],
                operation_key='first' if first else 'second')
        return repository.attribute(
            case['root'], case['contractor'], '100',
            brigade_id=None if first else case['brigade'],
            operation_key='first' if first else 'second')

    def first_writer():
        connection = connect(case['path'])
        try:
            results.append(assign(LaborRepository(connection), True))
        except Exception as exc:
            failures.append(exc)
        finally:
            connection.close()

    thread = Thread(target=first_writer, name='labor-first-writer', daemon=True)
    thread.start()
    try:
        assert paused.wait(10), 'First writer never reached the scheduling barrier'
        assert assign(LaborRepository(case['db']), False) == 'second'
    finally:
        resume.set()
        thread.join(10)
    assert not thread.is_alive(), 'First writer did not finish'
    assert not results, 'The delayed writer consumed capacity already assigned by the second writer'
    assert len(failures) == 1 and isinstance(failures[0], ValidationError)
    repository = LaborRepository(case['db'])
    if kind == 'hours':
        assert sum(row['hours'] for row in repository.hour_rows()) == Decimal('8')
    else:
        assert sum(row['quantity'] for row in repository.attribution_rows()) == Decimal('100')
    assert len(repository.history()) == 1
