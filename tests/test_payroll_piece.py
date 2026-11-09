"""Piecework uses explicit confirmed sources and exact, reviewable distributions."""
from decimal import Decimal
import pytest

from app.repositories.base import ValidationError
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.event_repository import EventRepository


def make_period(db, contractor, year, month):
    from app.time_utils import instant
    now = instant()
    period = db.execute('INSERT INTO payroll_periods(project_id,contractor_id,year,month,created_at) VALUES (1,?,?,?,?)',
                        (contractor, year, month, now)).lastrowid
    version = db.execute('INSERT INTO payroll_versions(period_id,version_no,norm_hours,created_at,updated_at) VALUES (?,1,?,?,?)',
                         (period, '160', now, now)).lastrowid
    return period, version


@pytest.fixture
def piece_case(db, work, item, plan):
    from app.repositories.payroll_piece_repository import PayrollPieceRepository
    from app.repositories.payroll_rules_repository import PayrollRulesRepository
    people = PersonnelRepository(db)
    contractor = people.save_contractor({'name': 'Подрядчик сдельной оплаты'})
    specialty = people.save_specialty('Монтажник 44', 'P44')
    brigade = BrigadeRepository(db).save(contractor, 'P44')
    employees = [people.save_worker(dict(full_name='Сдельный работник ' + str(i), contractor_id=contractor,
                  specialty_id=specialty, effective_date='2026-01-01')) for i in range(3)]
    for employee in employees:
        BrigadeRepository(db).assign(employee, brigade, '2026-01-01')
    events = EventRepository(db)
    fact = events.add_fact(item, '100', '2026-10-01', event_key='piece-fact')
    root = events.add_confirmation(item, '100', '2026-10-01', event_key='piece-confirmation', document_ref='Акт 44')
    rules = PayrollRulesRepository(db)
    for employee in employees:
        rules.save_terms(dict(employee_id=employee, contractor_id=contractor, valid_from='2026-01-01',
            system='piece', base_kind='none', piece_enabled=True, currency='RUB'))
    catalog = db.execute('SELECT catalog_id FROM work_items WHERE id=?', (item,)).fetchone()[0]
    unit = db.execute('SELECT unit FROM work_catalog WHERE id=?', (catalog,)).fetchone()[0]
    rate = rules.save_rate(dict(catalog_id=catalog, contractor_id=contractor, amount='0.01', unit=unit,
                               currency='RUB', valid_from='2026-01-01'))
    period, version = make_period(db, contractor, 2026, 10)
    piece = PayrollPieceRepository(db)
    data = dict(confirmation_event_id=root, contractor_id=contractor, brigade_id=brigade,
                quantity='100', proof_ref='Акт распределения 44')
    claim = piece.save_claim(data)
    return dict(db=db, piece=piece, period=period, version=version, rules=rules, data=data,
                contractor=contractor, brigade=brigade, employees=employees, root=root, fact=fact,
                item=item, catalog=catalog, unit=unit, rate=rate, events=events, claim=claim)


def equal_preview(d, quantity='100', key=None):
    return d['piece'].preview(d['version'], d['claim'], quantity, 'equal',
        [dict(employee_id=i) for i in d['employees']], operation_key=key)


def test_dashboard_counts_piece_quantity_once_per_batch_and_keeps_units(piece_case):
    from app.repositories.payroll_repository import PayrollRepository
    d = piece_case
    d['piece'].confirm(d['version'], equal_preview(d)['id'])
    repo = PayrollRepository(d['db'])
    repo.calculate(d['version'])
    repo.transition(d['version'], 'submit')
    repo.transition(d['version'], 'approve')
    report = repo.dashboard()
    row = next(row for row in report['by_work'] if row['work_item_id'] == d['item'])
    assert row['quantity'] == Decimal('100')  # Three shares are still one volume.
    assert row['unit'] == d['unit']
    assert row['amount'] == Decimal('1')
    assert row['unit_cost'] == Decimal('.01')
    assert report['metrics']['RUB']['employee_count'] == 3
    assert report['metrics']['RUB']['hour_cost'] is None
    d['events'].add_confirmation(d['item'], '-10', '2026-10-01', correction_of_id=d['root'],
        event_key='dashboard-reduction', document_ref='Уточнено', comment='Уточнённый объём')
    after = repo.dashboard()
    assert after['funds'] == {'RUB': Decimal('1')}
    assert after['by_work'][0]['unit_cost'] is None
    assert after['source_conflicts'] == [d['period']]


def test_two_connections_confirm_competing_previews_atomically(piece_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.database import connect
    from app.repositories.payroll_piece_repository import PayrollPieceRepository
    d = piece_case
    previews = [equal_preview(d)['id'] for _ in range(2)]
    path = d['db'].execute('PRAGMA database_list').fetchone()[2]
    barrier = Barrier(2)
    def confirm(preview):
        connection = connect(path)
        try:
            barrier.wait(timeout=10)
            try:
                return PayrollPieceRepository(connection).confirm(d['version'], preview)
            except ValidationError as error:
                return error
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, previews))
    assert sum(isinstance(result, int) for result in results) == 1
    assert sum(isinstance(result, ValidationError) for result in results) == 1
    assert len(d['piece'].batches(d['version'])) == 1
    assert d['piece'].list_claims()[0]['reserved'] == Decimal('100')
    assert sum(share['amount_minor'] for share in d['piece'].batches(d['version'])[0]['shares']) == 100


def test_explicit_claim_equal_split_and_confirmation_are_idempotent(piece_case):
    d = piece_case
    assert len(d['piece'].list_claims()) == 1
    before = d['db'].execute('SELECT COUNT(*) FROM confirmation_events').fetchone()[0]
    preview = equal_preview(d, key='piece-preview')
    assert preview == equal_preview(d, key='piece-preview')
    assert preview['total_minor'] == 100
    assert [s['amount_minor'] for s in preview['shares']] == [34, 33, 33]
    batch = d['piece'].confirm(d['version'], preview['id'], operation_key='piece-confirm')
    assert batch == d['piece'].confirm(d['version'], preview['id'], operation_key='piece-confirm')
    assert len(d['piece'].batches(d['version'])) == 1
    assert d['piece'].list_claims()[0]['remaining'] == 0
    validation = d['piece'].validate(d['version'])
    assert not validation['errors']
    assert sum(line['amount_minor'] for line in validation['lines']) == 100
    assert d['db'].execute('SELECT COUNT(*) FROM confirmation_events').fetchone()[0] == before


def test_claims_need_proof_root_and_cannot_exceed_confirmed_capacity(piece_case):
    d = piece_case
    for changes in [dict(proof_ref=''), dict(quantity='101'), dict(confirmation_event_id=d['fact'] + 99999)]:
        with pytest.raises((ValidationError, ValueError)):
            d['piece'].save_claim(dict(d['data'], **changes))
    with pytest.raises(ValidationError):
        d['piece'].save_claim(dict(d['data'], brigade_id=None, quantity='1'))
    assert len(d['piece'].list_claims()) == 1


def test_new_correction_invalidates_preview_before_reserving(piece_case):
    d = piece_case
    preview = equal_preview(d)
    d['events'].add_confirmation(d['item'], '-1', '2026-10-01', event_key='piece-reduction',
        document_ref='Исправленный акт', correction_of_id=d['root'], comment='Исправление')
    with pytest.raises(ValidationError):
        d['piece'].confirm(d['version'], preview['id'])
    assert not d['piece'].batches(d['version'])


def test_claim_revision_and_rate_revision_invalidate_preview(piece_case):
    d = piece_case
    preview = equal_preview(d, '20')
    d['piece'].save_claim(dict(d['data'], quantity='90', reason='Исправление назначения'),
                           claim_id=d['claim'], expected_revision=1)
    with pytest.raises(ValidationError):
        d['piece'].confirm(d['version'], preview['id'])
    fresh = equal_preview(d, '20')
    d['rules'].save_rate(dict(catalog_id=d['catalog'], contractor_id=d['contractor'], amount='0.02',
        unit=d['unit'], currency='RUB', valid_from='2026-01-01', comment='Новая ставка'),
        rate_id=d['rate'], expected_revision=1)
    with pytest.raises(ValidationError):
        d['piece'].confirm(d['version'], fresh['id'])
    assert not d['piece'].batches(d['version'])


def test_cancel_requires_reason_and_keeps_history(piece_case):
    d = piece_case
    batch = d['piece'].confirm(d['version'], equal_preview(d)['id'])
    with pytest.raises(ValidationError):
        d['piece'].cancel_batch(d['version'], batch, '')
    d['piece'].cancel_batch(d['version'], batch, 'Ошибочное распределение', operation_key='piece-cancel')
    d['piece'].cancel_batch(d['version'], batch, 'Ошибочное распределение', operation_key='piece-cancel')
    assert d['piece'].batches(d['version'])[0]['status'] == 'cancelled'
    assert d['piece'].list_claims()[0]['remaining'] == Decimal(100)
    assert not d['piece'].validate(d['version'])['lines']


def test_manual_and_ktu_splits_are_exact_and_validate_inputs(piece_case):
    d = piece_case
    a, b, c = d['employees']
    manual = d['piece'].preview(d['version'], d['claim'], '100', 'manual',
        [dict(employee_id=a, amount='0.20'), dict(employee_id=b, amount='0.30'), dict(employee_id=c, amount='0.50')])
    assert [s['amount_minor'] for s in manual['shares']] == [20, 30, 50]
    ktu = d['piece'].preview(d['version'], d['claim'], '100', 'ktu',
        [dict(employee_id=a, weight='1'), dict(employee_id=b, weight='2'), dict(employee_id=c, weight='3')])
    assert [s['amount_minor'] for s in ktu['shares']] == [17, 33, 50]
    for method, recipients in [('manual', [dict(employee_id=a, amount='0.99')]),
        ('ktu', [dict(employee_id=a, weight='0')]), ('equal', [dict(employee_id=a), dict(employee_id=a)])]:
        with pytest.raises((ValidationError, ValueError)):
            d['piece'].preview(d['version'], d['claim'], '100', method, recipients)


def test_hours_use_only_allocated_hours_for_same_work_and_confirmation_date(piece_case, work):
    from app.repositories.timesheet_repository import TimesheetRepository
    from app.repositories.labor_repository import LaborRepository
    d = piece_case
    a, b, _ = d['employees']
    timesheets = TimesheetRepository(d['db'])
    sheet = timesheets.create(d['contractor'], 2026, 10)
    timesheets.set_entries(sheet, [dict(employee_id=i, work_date='2026-10-01', code='Я', hours='8') for i in [a, b]])
    timesheets.transition(sheet, 'submit')
    timesheets.transition(sheet, 'approve')
    labor = LaborRepository(d['db'])
    labor.allocate(d['item'], '2026-10-01', [dict(employee_id=a, hours='2'), dict(employee_id=b, hours='6')])
    preview = d['piece'].preview(d['version'], d['claim'], '100', 'hours', [dict(employee_id=i) for i in [a, b]])
    assert [s['amount_minor'] for s in preview['shares']] == [25, 75]
    labor.allocate(d['item'], '2026-10-01', [dict(employee_id=a, hours='4', expected_revision=1)], reason='Уточнение времени')
    with pytest.raises(ValidationError):
        d['piece'].confirm(d['version'], preview['id'])


def test_technical_operation_is_explicit_and_excludes_whole_source_claim(piece_case):
    d = piece_case
    op = d['rules'].save_operation(d['catalog'], 'tech44', 'Операция монтажа')
    with pytest.raises(ValidationError):
        d['piece'].save_claim(dict(d['data'], quantity='50', operation_id=op))
    d['piece'].save_claim(dict(d['data'], quantity='0', reason='Перейти на операции'),
        claim_id=d['claim'], expected_revision=1)
    first = d['piece'].save_claim(dict(d['data'], quantity='60', operation_id=op))
    with pytest.raises(ValidationError):
        d['piece'].save_claim(dict(d['data'], brigade_id=None, quantity='41', operation_id=op))
    assert next(r for r in d['piece'].list_claims() if r['root_id'] == first)['quantity'] == Decimal(60)
    assert d['db'].execute('SELECT COUNT(*) FROM confirmation_events').fetchone()[0] == 1


def test_recipients_require_explicit_claim_context_and_enabled_terms(piece_case):
    d = piece_case
    employee = d['employees'][0]
    own_claim = d['piece'].save_claim(dict(d['data'], quantity='0', reason='Снять бригадное назначение'),
        claim_id=d['claim'], expected_revision=1)
    d['claim'] = d['piece'].save_claim(dict(d['data'], brigade_id=None, employee_id=employee, quantity='100'))
    with pytest.raises(ValidationError):
        equal_preview(d)
    term = next(r for r in d['rules'].list_terms() if r['employee_id'] == employee)
    d['rules'].save_terms(dict(term, piece_enabled=False, system='hourly', base_kind='hourly', hourly_rate='100'),
        term_id=term['root_id'], expected_revision=term['revision_no'])
    with pytest.raises(ValidationError):
        d['piece'].preview(d['version'], d['claim'], '100', 'equal', [dict(employee_id=employee)])


def test_cross_month_requires_explicit_reason_and_bank_is_global(piece_case):
    d = piece_case
    other, other_version = make_period(d['db'], d['contractor'], 2026, 11)
    with pytest.raises(ValidationError):
        d['piece'].preview(other_version, d['claim'], '10', 'equal', [dict(employee_id=d['employees'][0])])
    first = equal_preview(d, '70')
    d['piece'].confirm(d['version'], first['id'])
    with pytest.raises(ValidationError):
        d['piece'].preview(other_version, d['claim'], '31', 'equal', [dict(employee_id=d['employees'][0])],
                           reason='Начисление за ранее подтверждённую работу')
    preview = d['piece'].preview(other_version, d['claim'], '30', 'equal', [dict(employee_id=d['employees'][0])],
                           reason='Начисление за ранее подтверждённую работу')
    d['piece'].confirm(other_version, preview['id'])
    assert d['piece'].list_claims()[0]['reserved'] == 100


def test_whole_source_reservation_blocks_technical_claim_even_after_claim_revoked(piece_case):
    d = piece_case
    d['piece'].confirm(d['version'], equal_preview(d, '10')['id'])
    d['piece'].save_claim(dict(d['data'], quantity='0', reason='Снять назначение'),
                           claim_id=d['claim'], expected_revision=1)
    op = d['rules'].save_operation(d['catalog'], 'reserved44', 'Операция по занятому источнику')
    with pytest.raises(ValidationError):
        d['piece'].save_claim(dict(d['data'], quantity='100', operation_id=op))


def test_correction_copy_reserves_max_per_period_and_old_versions_do_not_accumulate(piece_case):
    from app.time_utils import instant
    d = piece_case
    batch = d['piece'].confirm(d['version'], equal_preview(d, '70')['id'])
    for status in ['calculated', 'submitted', 'approved']:
        d['db'].execute('UPDATE payroll_versions SET status=?,approved_at=? WHERE id=?',
                        (status, instant() if status == 'approved' else None, d['version']))
    now = instant()
    new = d['db'].execute('INSERT INTO payroll_versions(period_id,version_no,base_version_id,correction_reason,norm_hours,created_at,updated_at) '
        'VALUES (?,2,?,?,?, ?,?)', (d['period'], d['version'], 'Корректировка', '160', now, now)).lastrowid
    d['db'].execute('BEGIN IMMEDIATE')
    try:
        d['piece'].copy_to_version(d['version'], new)
        d['piece'].copy_to_version(d['version'], new)
        d['db'].commit()
    except Exception:
        d['db'].rollback()
        raise
    assert len(d['piece'].batches(new)) == 1
    assert d['piece'].list_claims()[0]['reserved'] == 70
    assert not d['piece'].validate(new)['errors']
    copied = d['piece'].batches(new)[0]['id']
    d['piece'].cancel_batch(new, copied, 'Уменьшить начисление')
    assert d['piece'].list_claims()[0]['reserved'] == 70
    preview = d['piece'].preview(new, d['claim'], '30', 'equal', [dict(employee_id=i) for i in d['employees']])
    d['piece'].confirm(new, preview['id'])
    assert d['piece'].list_claims()[0]['reserved'] == 70
    for status in ['calculated', 'submitted', 'approved']:
        d['db'].execute('UPDATE payroll_versions SET status=?,approved_at=? WHERE id=?',
                        (status, instant() if status == 'approved' else None, new))
    assert d['piece'].list_claims()[0]['reserved'] == 30
    assert d['piece'].batches(d['version'])[0]['id'] == batch
    with pytest.raises(ValidationError):
        d['piece'].cancel_batch(d['version'], batch, 'История должна сохраниться')


def test_two_period_confirmations_cannot_race_past_source_bank(piece_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.database import connect
    from app.repositories.payroll_piece_repository import PayrollPieceRepository
    d = piece_case
    first = equal_preview(d, '70')
    other, new_version = make_period(d['db'], d['contractor'], 2026, 11)
    second = d['piece'].preview(new_version, d['claim'], '70', 'equal',
        [dict(employee_id=i) for i in d['employees']], reason='Перенос начисления')
    path = d['db'].execute('PRAGMA database_list').fetchone()[2]
    barrier = Barrier(2)
    def confirm(version_id, preview_id):
        connection = connect(path)
        try:
            barrier.wait(timeout=10)
            try:
                return PayrollPieceRepository(connection).confirm(version_id, preview_id)
            except ValidationError:
                return None
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda args: confirm(*args), [(d['version'], first['id']), (new_version, second['id'])]))
    assert sum(result is not None for result in results) == 1
    assert d['db'].execute('SELECT COUNT(*) FROM payroll_piece_batches').fetchone()[0] == 1
    assert d['piece'].list_claims()[0]['reserved'] == 70


@pytest.mark.parametrize('value', ['', 'bad', None, True, 0.1, 'NaN', 'Infinity', '-1'])
def test_invalid_preview_decimal_is_a_validation_error(piece_case, value):
    d = piece_case
    with pytest.raises((ValidationError, ValueError)):
        equal_preview(d, value)


@pytest.mark.parametrize('method,field', [('ktu', 'weight'), ('manual', 'amount')])
def test_missing_distribution_input_is_a_validation_error(piece_case, method, field):
    d = piece_case
    with pytest.raises((ValidationError, ValueError)):
        d['piece'].preview(d['version'], d['claim'], '100', method, [dict(employee_id=d['employees'][0])])


def test_piece_amount_respects_storage_boundary_before_any_preview_write(piece_case):
    d = piece_case
    d['rules'].save_rate(dict(catalog_id=d['catalog'], contractor_id=d['contractor'], amount='10000000000000',
        unit=d['unit'], currency='RUB', valid_from='2026-01-01'), rate_id=d['rate'], expected_revision=1)
    with pytest.raises(ValidationError):
        equal_preview(d)
    assert d['db'].execute('SELECT COUNT(*) FROM payroll_piece_previews').fetchone()[0] == 0


def test_batch_names_preserve_snapshot_and_personnel_change_requires_review(piece_case):
    d = piece_case
    batch_id = d['piece'].confirm(d['version'], equal_preview(d)['id'])
    before = d['piece'].batches(d['version'])[0]['shares'][0]['full_name']
    d['db'].execute('UPDATE employees SET full_name=? WHERE id=?', ('Новое имя после распределения', d['employees'][0]))
    assert d['piece'].batches(d['version'])[0]['shares'][0]['full_name'] == before
    assert d['piece'].validate(d['version'])['errors']


def test_pure_piece_period_approval_and_primary_correction_pipeline(piece_case):
    from app.repositories.payroll_repository import PayrollRepository
    d = piece_case
    people = PersonnelRepository(d['db'])
    specialty = people.employee(d['employees'][0])['specialty_id']
    for index in range(2):
        employee = people.save_worker(dict(full_name='Дополнительный сдельщик ' + str(index),
            contractor_id=d['contractor'], specialty_id=specialty, effective_date='2026-01-01'))
        BrigadeRepository(d['db']).assign(employee, d['brigade'], '2026-01-01')
        d['rules'].save_terms(dict(employee_id=employee, contractor_id=d['contractor'], valid_from='2026-01-01',
            system='piece', piece_enabled=True, currency='RUB'))
        d['employees'].append(employee)
    d['rules'].save_rate(dict(catalog_id=d['catalog'], contractor_id=d['contractor'], amount='300',
        unit=d['unit'], currency='RUB', valid_from='2026-01-01'), rate_id=d['rate'], expected_revision=1)
    batch = d['piece'].confirm(d['version'], equal_preview(d)['id'])
    periods = PayrollRepository(d['db'])
    periods.calculate(d['version'])
    initial = periods.report(d['period'])
    assert not initial['errors'] and not initial['source_changed']
    assert initial['totals'] == {'RUB': Decimal('30000')}
    assert len(initial['rows']) == 5
    assert {row['full_name'] for row in initial['rows']} == {people.employee(i)['full_name'] for i in d['employees']}
    assert all(row['piece'] == Decimal('6000') and row['base'] == 0 for row in initial['rows'])
    periods.transition(d['version'], 'submit')
    periods.transition(d['version'], 'approve')
    assert not periods.report(d['period'])['source_changed']
    d['events'].add_confirmation(d['item'], '-10', '2026-10-01', event_key='approved-piece-reduction',
        document_ref='Исправленный акт', correction_of_id=d['root'], comment='Уменьшение объёма')
    frozen = periods.report(d['period'])
    assert frozen['totals'] == initial['totals'] and frozen['source_changed'] and frozen['warnings']
    assert d['piece'].validate(d['version'])['errors']
    with pytest.raises(ValidationError):
        d['piece'].cancel_batch(d['version'], batch, 'Нельзя менять утверждённое')
    correction = periods.create_correction(d['period'], 'Пересчитать по исправленному акту')
    periods.calculate(correction)
    assert periods.report(d['period'])['errors']
    with pytest.raises(ValidationError):
        periods.transition(correction, 'submit')
    copied = d['piece'].batches(correction)[0]['id']
    d['piece'].cancel_batch(correction, copied, 'Заменить исправленным распределением')
    d['piece'].save_claim(dict(d['data'], quantity='90', reason='Исправление акта'),
        claim_id=d['claim'], expected_revision=1)
    preview = d['piece'].preview(correction, d['claim'], '90', 'equal', [dict(employee_id=i) for i in d['employees']])
    d['piece'].confirm(correction, preview['id'])
    assert d['piece'].list_claims()[0]['reserved'] == 100  # frozen baseline is still reserved until approval
    periods.calculate(correction)
    revised = periods.report(d['period'])
    assert not revised['errors'] and not revised['source_changed']
    assert revised['totals'] == {'RUB': Decimal('27000')}
    assert revised['delta'] == {'RUB': Decimal('-3000')}
    periods.transition(correction, 'submit')
    periods.transition(correction, 'approve')
    assert d['piece'].list_claims()[0]['reserved'] == 90
    assert periods.report(d['period'], d['version'])['totals'] == initial['totals']


def test_period_input_change_rejects_unconfirmed_preview_and_replay_survives_later_changes(piece_case):
    from app.repositories.payroll_repository import PayrollRepository
    d = piece_case
    periods = PayrollRepository(d['db'])
    stale = equal_preview(d, '20')
    periods.update_settings(d['version'], '170', comment='Уточнение нормы')
    with pytest.raises(ValidationError):
        d['piece'].confirm(d['version'], stale['id'])
    assert not d['piece'].batches(d['version'])
    preview = equal_preview(d, '20', key='final-preview')
    batch = d['piece'].confirm(d['version'], preview['id'], operation_key='final-confirm')
    periods.calculate(d['version'])
    periods.transition(d['version'], 'submit')
    periods.transition(d['version'], 'approve')
    assert d['piece'].confirm(d['version'], preview['id'], operation_key='final-confirm') == batch
    assert d['piece'].confirm(d['version'], preview['id']) == batch
    assert equal_preview(d, '20', key='final-preview') == preview
    assert len(d['piece'].batches(d['version'])) == 1
    with pytest.raises(ValidationError):
        equal_preview(d, '21', key='final-preview')


def test_correction_chain_net_capacity_and_historical_claim_revisions(piece_case):
    from app.time_utils import instant
    d = piece_case
    before = instant()
    correction = d['events'].add_confirmation(d['item'], '-10', '2026-10-01', event_key='chain-minus',
        document_ref='Исправление 1', correction_of_id=d['root'], comment='Уменьшение')
    d['events'].add_confirmation(d['item'], '5', '2026-10-01', event_key='chain-plus',
        document_ref='Исправление 2', correction_of_id=correction, comment='Уточнение уменьшения')
    d['piece'].save_claim(dict(d['data'], quantity='95', reason='Цепочка исправлений'),
        claim_id=d['claim'], expected_revision=1)
    historical = d['piece'].list_claims(before)[0]
    current = d['piece'].list_claims()[0]
    assert historical['revision_no'] == 1 and historical['sourceavailable'] == 100
    assert current['revision_no'] == 2 and current['quantity'] == current['sourceavailable'] == 95
    with pytest.raises(ValidationError):
        d['piece'].save_claim(dict(d['data'], confirmation_event_id=correction, quantity='1'))
    preview = equal_preview(d, '95')
    d['piece'].confirm(d['version'], preview['id'])
    assert d['piece'].list_claims()[0]['remaining'] == 0


def test_cross_month_rate_uses_confirmation_business_date_and_historical_brigade(piece_case):
    d = piece_case
    rate = next(r for r in d['rules'].list_rates() if r['root_id'] == d['rate'])
    d['rules'].save_rate(dict(rate, valid_to='2026-11-01'), rate_id=d['rate'], expected_revision=1)
    d['rules'].save_rate(dict(catalog_id=d['catalog'], contractor_id=d['contractor'], amount='1000',
        unit=d['unit'], currency='RUB', valid_from='2026-11-01'))
    employee = d['employees'][0]
    other_brigade = BrigadeRepository(d['db']).save(d['contractor'], 'P44-new')
    BrigadeRepository(d['db']).assign(employee, other_brigade, '2026-10-02')
    period, version = make_period(d['db'], d['contractor'], 2026, 11)
    preview = d['piece'].preview(version, d['claim'], '100', 'equal', [dict(employee_id=employee)],
                                reason='Начислить акт октября в ноябре')
    assert preview['total_minor'] == 100  # October price, not the November 1000 rate
    assert preview['source']['participants'][0]['membership']['brigade_id'] == d['brigade']
    d['piece'].confirm(version, preview['id'])
    assert not d['piece'].validate(version)['errors']


def test_historical_bank_includes_batch_that_was_unapproved_at_cutoff(piece_case):
    from app.time_utils import instant
    from app.repositories.payroll_repository import PayrollRepository
    d = piece_case
    d['piece'].confirm(d['version'], equal_preview(d, '70')['id'])
    cutoff = instant()
    periods = PayrollRepository(d['db'])
    periods.calculate(d['version'])
    periods.transition(d['version'], 'submit')
    periods.transition(d['version'], 'approve')
    assert d['piece'].list_claims(cutoff)[0]['reserved'] == 70


def test_source_reduction_correction_can_reassign_volume_to_new_personal_claim(piece_case):
    from app.repositories.payroll_repository import PayrollRepository
    d = piece_case
    periods = PayrollRepository(d['db'])
    d['piece'].confirm(d['version'], equal_preview(d)['id'])
    periods.calculate(d['version'])
    periods.transition(d['version'], 'submit')
    periods.transition(d['version'], 'approve')
    frozen = periods.report(d['period'])
    d['events'].add_confirmation(d['item'], '-10', '2026-10-01', event_key='reassign-source-reduction',
        document_ref='Уточнённый акт исполнителя', correction_of_id=d['root'], comment='Исправлен объём и исполнитель')
    correction = periods.create_correction(d['period'], 'Переназначить объём по исправленному акту')
    copied = d['piece'].batches(correction)[0]['id']
    d['piece'].cancel_batch(correction, copied, 'Персональное начисление вместо бригадного')
    d['piece'].save_claim(dict(d['data'], quantity='0', reason='Снять прежнее бригадное назначение'),
                           claim_id=d['claim'], expected_revision=1)
    employee = d['employees'][1]
    claim = d['piece'].save_claim(dict(d['data'], brigade_id=None, employee_id=employee, quantity='90',
        proof_ref='Исправленный акт с персональным исполнителем'))
    preview = d['piece'].preview(correction, claim, '90', 'equal', [dict(employee_id=employee)])
    d['piece'].confirm(correction, preview['id'])
    periods.calculate(correction)
    revised = periods.report(d['period'])
    assert not revised['errors'] and not revised['source_changed']
    assert revised['totals'] == {'RUB': Decimal('0.90')}
    assert len(revised['rows']) == 1 and revised['rows'][0]['employee_id'] == employee
    period, other_version = make_period(d['db'], d['contractor'], 2026, 11)
    with pytest.raises(ValidationError):
        d['piece'].preview(other_version, claim, '1', 'equal', [dict(employee_id=employee)],
                           reason='Не разрешать повторно использовать объём до утверждения корректировки')
    periods.transition(correction, 'submit')
    periods.transition(correction, 'approve')
    claims = {row['claim_id']: row for row in d['piece'].list_claims()}
    assert claims[d['claim']]['reserved'] == 0 and claims[claim]['reserved'] == 90
    assert periods.report(d['period'], d['version'])['totals'] == frozen['totals']
