from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
import sqlite3

import pytest

from app.calculation_engine.historical import HistoricalCalculator
from app.database import connect
from app.repositories.base import ValidationError
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.work_order_repository import WorkOrderRepository
from app.repositories.work_repository import WorkRepository


@pytest.fixture
def setup_orders(db):
    people, work, orders = PersonnelRepository(db), WorkRepository(db), WorkOrderRepository(db)
    contractor = people.save_contractor(dict(name='ООО Строй', inn='1234567890', contact_person='Контакт', phone='+7 900 123-45-67'))
    plaster = people.save_specialty('Штукатур', 'ШТ')
    tiler = people.save_specialty('Плиточник', 'ПЛ')
    issuer = people.save_employee(dict(full_name='Прораб Соколов', position='Прораб'))
    foreman = people.save_employee(dict(full_name='Бригадир Орлов', contractor_id=contractor, position='Бригадир'))
    teams = [[people.save_employee(dict(full_name=name, contractor_id=contractor, specialty_id=specialty)) for name in names]
             for specialty, names in [(plaster, ['Иванов', 'Петров', 'Сидоров', 'Смирнов']), (tiler, ['Козлов', 'Волков', 'Зайцев'])]]
    items = [work.add_work_item(building, work.add_floor(building, '3', 3), catalog) for building, catalog in [(1, 1), (2, 4)]]
    plan = work.create_plan_version('CONSTRUCTION', created_at='2026-01-01T00:00:00Z')
    for item, quantity in zip(items, ['1000', '500']):
        work.add_plan_line(plan, item, quantity, '0.8')
    work.approve_plan_version(plan, approved_at='2026-01-01T01:00:00Z')
    header = dict(shift_date='2026-10-17', shift_start='08:00', shift_end='17:00', contractor_id=contractor,
                  issued_by_employee_id=issuer, foreman_employee_id=foreman, notes='Смена 1')
    lines = [dict(work_item_id=item, planned_quantity=q, plan_version_id=plan, remarks='Замечание') for item, q in zip(items, ['120', '60'])]
    crew = [dict(specialty_id=s, planned_count=count, employee_ids=employees, comment='Бригада')
            for s, count, employees in zip([plaster, tiler], [4, 3], teams)]
    return dict(people=people, work=work, orders=orders, contractor=contractor, issuer=issuer, foreman=foreman,
                teams=teams, specialties=[plaster, tiler], items=items, plan=plan, header=header, lines=lines, crew=crew)


def draft(s, **kwargs):
    return s['orders'].save_draft(s['header'], s['lines'], s['crew'], **kwargs)


def test_create_and_edit_contractor_and_employee(db, setup_orders):
    s = setup_orders
    assert s['people'].contractor(s['contractor'])['name'] == 'ООО Строй'
    s['people'].save_contractor(dict(name='ООО Строй 2', short_name='Строй'), s['contractor'])
    s['people'].save_employee(dict(full_name='Прораб Новиков', position='Прораб'), s['issuer'])
    assert s['people'].employee(s['issuer'])['full_name'] == 'Прораб Новиков'
    assert len(s['people'].list_employees()) == 9
    assert len(s['people'].list_specialties()) == 2


def test_archive_restore_contractors_and_workers(setup_orders):
    s = setup_orders
    for kind, record in [('contractors', s['contractor']), ('employees', s['foreman'])]:
        s['people'].set_active(kind, record, False)
        with pytest.raises(ValidationError, match='архивирована'):
            draft(s)
        s['people'].set_active(kind, record, True)
    assert draft(s)


@pytest.mark.parametrize('inn', ['123', 'abcdef1234', '1234567890123'])
def test_invalid_inn_is_rejected_without_partial_record(db, inn):
    with pytest.raises(ValidationError):
        PersonnelRepository(db).save_contractor(dict(name='Организация', inn=inn))
    assert db.execute('SELECT count(*) FROM contractors').fetchone()[0] == 0


def test_draft_with_multiple_lines_and_brigade_does_not_take_number(db, setup_orders):
    s = setup_orders
    document = s['orders'].get_order(draft(s))
    assert document['order_number'] is None and document['status'] == 'draft'
    assert [line['planned_quantity'] for line in document['lines']] == ['120', '60']
    assert [c['planned_count'] for c in document['crew']] == [4, 3]
    assert document['crew'][0]['names'] == ['Иванов', 'Петров', 'Сидоров', 'Смирнов']
    assert not document['warnings']
    assert db.execute('SELECT count(*) FROM work_order_sequences').fetchone()[0] == 0


def test_brigade_can_use_count_only_names_only_or_partial_names(setup_orders):
    s = setup_orders
    for count, employees, warnings in [(4, [], True), (None, s['teams'][0], False), (4, s['teams'][0][:2], True)]:
        crew = [dict(specialty_id=s['specialties'][0], planned_count=count, employee_ids=employees)]
        document = s['orders'].get_order(s['orders'].save_draft(s['header'], s['lines'], crew))
        assert bool(document['warnings']) == warnings
        assert document['crew'][0]['planned_count'] == count


def test_repeated_draft_submission_returns_same_document(db, setup_orders):
    first = draft(setup_orders, draft_key='form-token')
    assert draft(setup_orders, draft_key='form-token') == first
    assert db.execute('SELECT count(*) FROM work_orders').fetchone()[0] == 1
    assert db.execute('SELECT count(*) FROM work_order_lines').fetchone()[0] == 2


def test_edit_draft_and_optimistic_revision(setup_orders):
    s = setup_orders
    order = draft(s)
    changed = dict(s['lines'][0], planned_quantity='121.000000001')
    s['orders'].save_draft(s['header'], [changed], s['crew'], order_id=order, expected_revision=1)
    document = s['orders'].get_order(order)
    assert len(document['lines']) == 1 and document['lines'][0]['planned_quantity'] == '121.000000001'
    with pytest.raises(ValidationError, match='другой вкладке'):
        s['orders'].save_draft(s['header'], s['lines'], s['crew'], order_id=order, expected_revision=1)


@pytest.mark.parametrize('value', ['0', '-1', '1e99', '0.0000000001', 1.1])
def test_invalid_order_volume_rolls_back_whole_document(db, setup_orders, value):
    s = setup_orders
    with pytest.raises((ValueError, ValidationError)):
        s['orders'].save_draft(s['header'], [s['lines'][0], dict(s['lines'][1], planned_quantity=value)], s['crew'])
    assert db.execute('SELECT count(*) FROM work_orders').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM work_order_lines').fetchone()[0] == 0


def test_duplicate_accounting_position_is_rejected(db, setup_orders):
    s = setup_orders
    with pytest.raises(ValidationError, match='повторяться'):
        s['orders'].save_draft(s['header'], [s['lines'][0], s['lines'][0]], s['crew'])
    assert db.execute('SELECT count(*) FROM work_orders').fetchone()[0] == 0


@pytest.mark.parametrize('case', ['foreign_item', 'foreign_contractor', 'foreign_issuer', 'foreign_foreman', 'foreign_worker'])
def test_cross_project_references_rejected(db, setup_orders, case):
    s = setup_orders
    project = ProjectRepository(db).create_project('Другой объект', 'OTHER', buildings=[dict(code='К1', name='Корпус')])
    people, work = PersonnelRepository(db, project), WorkRepository(db, project)
    contractor = people.save_contractor(dict(name='Чужая организация'))
    employee = people.save_employee(dict(full_name='Чужой', contractor_id=contractor, specialty_id=s['specialties'][0]))
    building = work.list_buildings()[0]['id']
    item = work.add_work_item(building, work.add_floor(building, '1', 1), 1)
    header, lines, crew = dict(s['header']), list(s['lines']), [dict(c) for c in s['crew']]
    if case == 'foreign_item': lines[0] = dict(lines[0], work_item_id=item)
    if case == 'foreign_contractor': header['contractor_id'] = contractor
    if case == 'foreign_issuer': header['issued_by_employee_id'] = employee
    if case == 'foreign_foreman': header['foreman_employee_id'] = employee
    if case == 'foreign_worker': crew[0]['employee_ids'] = [employee]
    with pytest.raises(ValidationError):
        s['orders'].save_draft(header, lines, crew)
    assert db.execute('SELECT count(*) FROM work_orders').fetchone()[0] == 0


def test_draft_plan_and_design_work_cannot_enter_order(setup_orders):
    s = setup_orders
    f = s['work'].list_floors(1)[0]['id']
    item = s['work'].add_work_item(1, f, 2)
    version = s['work'].create_plan_version('CONSTRUCTION')
    s['work'].add_plan_line(version, item, '100', '1')
    with pytest.raises(ValidationError):
        s['orders'].save_draft(s['header'], [dict(work_item_id=item, planned_quantity='10')], s['crew'])
    design = s['work'].add_work_item(1, f, 1, 'DESIGN')
    v = s['work'].create_plan_version('DESIGN')
    s['work'].add_plan_line(v, design, '100', '1')
    s['work'].approve_plan_version(v)
    with pytest.raises(ValidationError):
        s['orders'].save_draft(s['header'], [dict(work_item_id=design, planned_quantity='10')], s['crew'])


def test_issue_assigns_number_approval_and_status_history_without_changing_progress(db, setup_orders):
    s = setup_orders
    before = HistoricalCalculator(db).calculate_as_of('2026-10-17', '2099-01-01T00:00:00Z', s['plan'])
    quantities = [tuple(r) for r in db.execute('SELECT * FROM plan_lines ORDER BY id')]
    order = draft(s)
    assert s['orders'].issue(order) == 'НЗ-0001'
    document = s['orders'].get_order(order)
    assert document['status'] == 'issued' and document['issued_at']
    assert [r['new_status'] for r in document['history']] == ['draft', 'issued']
    assert len(document['approvals']) == 1 and document['approvals'][0]['employee_id'] == s['issuer']
    assert HistoricalCalculator(db).calculate_as_of('2026-10-17', '2099-01-01T00:00:00Z', s['plan']) == before
    assert before.total.actual_progress == Decimal(0)
    assert quantities == [tuple(r) for r in db.execute('SELECT * FROM plan_lines ORDER BY id')]
    assert db.execute('SELECT count(*) FROM fact_events').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM confirmation_events').fetchone()[0] == 0


def test_snapshots_use_issue_time_and_survive_all_directory_changes(db, setup_orders):
    s = setup_orders
    order = draft(s)
    s['work'].update_building(1, 'УЧ', 'Учебный на момент выдачи', 1)
    s['orders'].issue(order)
    before = s['orders'].get_order(order)
    assert before['lines'][0]['location']['building_name'] == 'Учебный на момент выдачи'
    s['work'].update_building(1, 'НОВ', 'Переименован', 1)
    s['people'].save_contractor(dict(name='Новый подрядчик'), s['contractor'])
    s['people'].save_employee(dict(full_name='Новое ФИО'), s['issuer'])
    s['people'].save_employee(dict(full_name='Новый штукатур'), s['teams'][0][0])
    s['people'].save_specialty('Новая специальность', 'НОВ', s['specialties'][0])
    ProjectRepository(db).update_project(1, 'Переименованный объект', 'NEW')
    after = s['orders'].get_order(order)
    assert (after['header'], after['lines'], after['crew']) == (before['header'], before['lines'], before['crew'])
    assert s['orders'].list_orders()[0]['contractor_name'] == 'ООО Строй'


def test_new_approved_version_requires_resaving_draft_before_issue(db, setup_orders):
    s = setup_orders
    order = draft(s)
    new = s['work'].create_plan_version('CONSTRUCTION', base_version_id=s['plan'])
    s['work'].approve_plan_version(new)
    with pytest.raises(ValidationError, match='Версия плана'):
        s['orders'].issue(order)
    assert db.execute('SELECT count(*) FROM work_order_sequences').fetchone()[0] == 0
    lines = [dict(line, plan_version_id=new) for line in s['lines']]
    s['orders'].save_draft(s['header'], lines, s['crew'], order_id=order)
    assert s['orders'].issue(order) == 'НЗ-0001'


@pytest.mark.parametrize('missing', ['lines', 'crew', 'issuer', 'foreman'])
def test_incomplete_draft_cannot_be_issued_or_consume_number(db, setup_orders, missing):
    s = setup_orders
    header = dict(s['header'])
    if missing == 'issuer': header['issued_by_employee_id'] = None
    if missing == 'foreman': header['foreman_employee_id'] = None
    order = s['orders'].save_draft(header, [] if missing == 'lines' else s['lines'], [] if missing == 'crew' else s['crew'])
    with pytest.raises(ValidationError):
        s['orders'].issue(order)
    assert db.execute('SELECT count(*) FROM work_order_sequences').fetchone()[0] == 0
    assert s['orders'].get_order(order)['status'] == 'draft'


@pytest.mark.parametrize('table, assignment', [
    ('work_orders', "notes='Изменено'"), ('work_orders', "order_number='НЗ-9999'"),
    ('work_order_lines', "planned_quantity='999'"), ('work_order_crew', 'planned_count=99'),
    ('work_order_crew_members', 'employee_id=999'), ('work_order_approvals', "comment='Подмена'"),
    ('work_order_status_history', "reason='Подмена'"),
])
def test_issued_document_and_history_are_protected_in_sql(db, setup_orders, table, assignment):
    s = setup_orders
    order = draft(s)
    s['orders'].issue(order)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute('UPDATE {} SET {}'.format(table, assignment))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute('DELETE FROM {}'.format(table))


def test_issued_document_cannot_be_edited_via_repository(setup_orders):
    s = setup_orders
    order = draft(s)
    s['orders'].issue(order)
    with pytest.raises(ValidationError, match='черновик'):
        draft(s, order_id=order)


@pytest.mark.parametrize('status', ['in_progress', 'submitted', 'accepted', 'closed'])
def test_stage33_status_transitions_are_disabled(db, setup_orders, status):
    order = draft(setup_orders)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute('UPDATE work_orders SET status=? WHERE id=?', (status, order))


def test_cancel_preserves_number_does_not_reuse_it_and_requires_reason(db, setup_orders):
    s = setup_orders
    cancelled_draft = draft(s)
    s['orders'].cancel(cancelled_draft, 'Черновик отменён')
    assert s['orders'].get_order(cancelled_draft)['order_number'] is None
    first = draft(s)
    s['orders'].issue(first)
    with pytest.raises(ValidationError): s['orders'].cancel(first, ' ')
    s['orders'].cancel(first, 'Перенос смены')
    document = s['orders'].get_order(first)
    assert document['order_number'] == 'НЗ-0001' and document['history'][-1]['reason'] == 'Перенос смены'
    ProjectRepository(db).set_archived(1, True)
    ProjectRepository(db).set_archived(1, False)
    assert s['orders'].issue(draft(s)) == 'НЗ-0002'
    with pytest.raises(sqlite3.IntegrityError): db.execute('DELETE FROM work_order_sequences')
    with pytest.raises(sqlite3.IntegrityError): db.execute('UPDATE work_order_sequences SET last_number=1')


@pytest.mark.parametrize('same_document', [False, True])
def test_concurrent_issue_serializes_numbers_and_is_idempotent(db, setup_orders, same_document):
    s = setup_orders
    ids = [draft(s) for _ in range(4)]
    if same_document: ids = [ids[0]] * 4
    filename = db.execute('PRAGMA database_list').fetchone()['file']
    barrier = Barrier(4)
    def issue(order):
        connection = connect(filename)
        try:
            barrier.wait(timeout=5)
            return WorkOrderRepository(connection).issue(order)
        finally: connection.close()
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(issue, ids))
    assert set(results) == ({'НЗ-0001'} if same_document else {'НЗ-0001', 'НЗ-0002', 'НЗ-0003', 'НЗ-0004'})
    assert db.execute('SELECT count(*) FROM work_order_approvals').fetchone()[0] == (1 if same_document else 4)


def test_numbering_is_independent_for_each_project(db, setup_orders):
    s = setup_orders
    project = ProjectRepository(db).create_project('Школа', 'ШК', buildings=[dict(code='УЧ', name='Учебный')])
    people, work, orders = PersonnelRepository(db, project), WorkRepository(db, project), WorkOrderRepository(db, project)
    contractor = people.save_contractor(dict(name='ООО Строй'))
    employee = people.save_employee(dict(full_name='Бригадир', contractor_id=contractor, specialty_id=s['specialties'][0]))
    building = work.list_buildings()[0]['id']
    item = work.add_work_item(building, work.add_floor(building, '3', 3), 1)
    version = work.create_plan_version('CONSTRUCTION')
    work.add_plan_line(version, item, '1000', '0.8')
    work.approve_plan_version(version)
    order = orders.save_draft(dict(s['header'], contractor_id=contractor, issued_by_employee_id=employee, foreman_employee_id=employee),
             [dict(work_item_id=item, planned_quantity='120')], [dict(specialty_id=s['specialties'][0], employee_ids=[employee])])
    assert orders.issue(order) == 'НЗ-0001'
    assert s['orders'].issue(draft(s)) == 'НЗ-0001'
    assert [o['id'] for o in orders.list_orders()] == [order]
    with pytest.raises(ValidationError): s['orders'].get_order(order)
    with pytest.raises(ValidationError): s['orders'].issue(order)
    with pytest.raises(ValidationError): s['orders'].cancel(order, 'Чужой объект')


def test_registry_filters(setup_orders):
    s = setup_orders
    first, second = draft(s), draft(s)
    s['orders'].issue(first)
    assert [r['id'] for r in s['orders'].list_orders(search='нз-0001')] == [first]
    assert [r['id'] for r in s['orders'].list_orders(status='draft')] == [second]
    assert len(s['orders'].list_orders(contractor_id=s['contractor'], building_id=1, date_from='2026-10-17', date_to='2026-10-17')) == 2
    assert s['orders'].list_orders(building_id=3) == []
    assert s['orders'].list_orders(date_from='2026-10-18') == []


def test_archiving_building_preserves_orders_and_prevents_new_assignments(setup_orders):
    s = setup_orders
    order = draft(s)
    s['orders'].issue(order)
    document = s['orders'].get_order(order)
    with pytest.raises(ValidationError): s['work'].delete_building(1, delete_empty_floors=True)
    s['work'].archive_building(1)
    assert 1 not in {r['building_id'] for r in s['orders'].available_items()}
    assert s['orders'].get_order(order)['lines'] == document['lines']
    assert s['orders'].list_orders(building_id=1)[0]['id'] == order
    with pytest.raises(ValidationError): draft(s)
    s['work'].archive_building(1, False)
    assert len(s['orders'].available_items()) == 2


def test_archived_project_refuses_all_new_module_mutations(db, setup_orders):
    s = setup_orders
    order = draft(s)
    ProjectRepository(db).set_archived(1, True)
    for action in (lambda: draft(s), lambda: s['orders'].issue(order), lambda: s['orders'].cancel(order, 'Причина'),
                   lambda: s['people'].save_contractor(dict(name='Новый')),
                   lambda: s['people'].set_active('employees', s['issuer'], False)):
        with pytest.raises(ValidationError): action()
    assert s['orders'].get_order(order)['status'] == 'draft'


@pytest.mark.parametrize('quantity', ['00.1', '0', '1.0', '1\x00', '1e3'])
def test_order_quantity_database_constraint_is_exact_canonical_text(db, setup_orders, quantity):
    document = setup_orders['orders'].get_order(draft(setup_orders))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute('UPDATE work_order_lines SET planned_quantity=? WHERE id=?', (quantity, document['lines'][0]['id']))


def test_number_expands_past_four_digits_without_reset(db, setup_orders):
    s = setup_orders
    s['orders'].issue(draft(s))
    db.execute('UPDATE work_order_sequences SET last_number=9999')
    assert s['orders'].issue(draft(s)) == 'НЗ-10000'


def test_existing_manual_fact_and_confirmation_do_not_become_order_events(db, setup_orders):
    from app.repositories.event_repository import EventRepository
    s = setup_orders
    events = EventRepository(db)
    events.add_fact(s['items'][0], '600', '2026-01-10', event_key='manual-fact')
    events.add_confirmation(s['items'][0], '450', '2026-01-10', event_key='manual-confirm', document_ref='Акт')
    before = {table: [tuple(r) for r in db.execute('SELECT * FROM ' + table)] for table in ('fact_events', 'confirmation_events')}
    report = HistoricalCalculator(db).calculate_as_of('2026-10-17', '2099-01-01T00:00:00Z', s['plan'])
    s['orders'].issue(draft(s))
    assert {table: [tuple(r) for r in db.execute('SELECT * FROM ' + table)] for table in before} == before
    assert HistoricalCalculator(db).calculate_as_of('2026-10-17', '2099-01-01T00:00:00Z', s['plan']) == report


def test_document_counter_and_deleted_empty_building_survive_application_initialization(db, setup_orders):
    from app.database import initialize
    s = setup_orders
    order = draft(s)
    s['orders'].issue(order)
    s['work'].delete_building(3)
    filename = db.execute('PRAGMA database_list').fetchone()['file']
    reopened = initialize(filename)
    try:
        repo = WorkOrderRepository(reopened)
        assert repo.get_order(order)['order_number'] == 'НЗ-0001'
        assert repo.issue(order) == 'НЗ-0001'
        assert reopened.execute('SELECT last_number FROM work_order_sequences').fetchone()[0] == 1
        assert reopened.execute('SELECT count(*) FROM work_order_approvals').fetchone()[0] == 1
        assert 3 not in [row['id'] for row in repo.work.list_buildings()]
    finally:
        reopened.close()
