import json
from decimal import Decimal, localcontext

import pytest

from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.timesheet_repository import TimesheetRepository


@pytest.fixture
def payroll_data(db):
    from app.repositories.payroll_rules_repository import PayrollRulesRepository
    from app.services.payroll_calculator import PayrollCalculator
    people = PersonnelRepository(db)
    contractor = people.save_contractor({'name': 'Расчётный подрядчик'})
    specialty = people.save_specialty('Штукатур', 'CALC-44')
    employee = people.save_worker(dict(full_name='Расчётный Работник', contractor_id=contractor,
        specialty_id=specialty, effective_date='2020-01-01'))
    return dict(db=db, people=people, contractor=contractor, employee=employee,
        rules=PayrollRulesRepository(db), sheets=TimesheetRepository(db), calculator=PayrollCalculator(db),
        period=dict(project_id=1, contractor_id=contractor, year=2026, month=10),
        version=dict(norm_hours='160'))


def terms(data, **changes):
    values = dict(employee_id=data['employee'], contractor_id=data['contractor'],
        valid_from='2026-10-01', system='hourly', hourly_rate='350', currency='RUB')
    values.update(changes)
    return data['rules'].save_terms(values)


def approved(data, first=1, count=20, hours='8', sheet=None, repo=None):
    repo = repo or data['sheets']
    sheet = sheet or repo.create(data['contractor'], 2026, 10)
    repo.set_entries(sheet, [dict(employee_id=data['employee'], work_date='2026-10-{:02d}'.format(n),
        code='Я', hours=hours) for n in range(first, first + count)])
    repo.transition(sheet, 'submit')
    repo.transition(sheet, 'approve')
    return sheet


def calculate(data, **options):
    return data['calculator'].calculate(data['period'], data['version'], **options)


@pytest.mark.parametrize('settings,count,want', [
    ({}, 20, 5600000),
    ({'system': 'salary', 'salary': '80000'}, 15, 6000000),
    ({'system': 'combined', 'base_kind': 'salary', 'salary': '80000',
      'bonus_kind': 'percent', 'bonus_basis': 'base', 'bonus_value': '20'}, 15, 7200000),
])
def test_explicit_hourly_salary_and_combined_examples_are_exact(payroll_data, settings, count, want):
    terms(payroll_data, **settings)
    approved(payroll_data, count=count)
    before = payroll_data['db'].total_changes
    result = calculate(payroll_data)
    assert result['errors'] == []
    assert result['totals'] == {'RUB': want}
    assert payroll_data['db'].total_changes == before
    assert json.dumps(result['sources'], sort_keys=True)
    assert result == calculate(payroll_data)


def test_hourly_terms_switch_on_business_date_without_double_bonus(payroll_data):
    terms(payroll_data, valid_to='2026-10-11')
    terms(payroll_data, valid_from='2026-10-11', hourly_rate='400',
        bonus_kind='fixed', bonus_value='1000')
    approved(payroll_data)
    result = calculate(payroll_data)
    assert result['totals'] == {'RUB': 6100000}
    assert sum(line['amount_minor'] for line in result['lines'] if line['component'] == 'base_hourly') == 6000000
    assert len([line for line in result['lines'] if line['component'] == 'bonus']) == 1


def test_percentage_bonus_is_exact_with_low_calling_decimal_precision(payroll_data):
    terms(payroll_data, hourly_rate='1234567.89', bonus_kind='percent', bonus_basis='base', bonus_value='13')
    approved(payroll_data, count=1)
    with localcontext() as context:
        context.prec = 6
        result = calculate(payroll_data)
    assert result['errors'] == []
    assert result['totals'] == {'RUB': 1116049373}
    assert next(line['amount_minor'] for line in result['lines'] if line['component'] == 'bonus') == 128395061


def test_missing_approved_time_terms_and_salary_norm_are_errors(payroll_data):
    assert calculate(payroll_data)['errors']
    approved(payroll_data)
    assert calculate(payroll_data)['errors']
    terms(payroll_data, system='salary', salary='80000')
    payroll_data['version']['norm_hours'] = None
    result = calculate(payroll_data)
    assert result['errors'] and not result['totals']


def test_latest_approved_time_ignores_open_correction_and_honors_cutoff(payroll_data):
    terms(payroll_data)
    sheet = approved(payroll_data, count=1)
    original = payroll_data['sheets'].report(sheet)['version']
    payroll_data['sheets'].create_correction(sheet, 'Уточнение')
    payroll_data['sheets'].set_entries(sheet, [dict(employee_id=payroll_data['employee'],
        work_date='2026-10-01', code='Я', hours='4')])
    assert calculate(payroll_data)['totals'] == {'RUB': 280000}
    payroll_data['sheets'].transition(sheet, 'submit')
    payroll_data['sheets'].transition(sheet, 'approve')
    assert calculate(payroll_data)['totals'] == {'RUB': 140000}
    assert calculate(payroll_data, cutoff=original['approved_at'])['totals'] == {'RUB': 280000}


def test_fixed_monthly_amount_is_split_over_global_approved_hours(payroll_data):
    from app.repositories.project_repository import ProjectRepository
    from app.services.payroll_calculator import PayrollCalculator
    from app.repositories.payroll_rules_repository import PayrollRulesRepository
    data = payroll_data
    terms(data, bonus_kind='fixed', bonus_value='1000', fixed_supplement='500')
    approved(data, count=1)
    second = ProjectRepository(data['db']).create_project('Другой объект', 'PAY44B')
    data['people'].share_contractor(data['contractor'], second)
    other_people = PersonnelRepository(data['db'], second)
    other_people.assign_to_project(data['employee'], '2020-01-01')
    other_rules = PayrollRulesRepository(data['db'], second)
    other_rules.save_terms(dict(employee_id=data['employee'], contractor_id=data['contractor'],
        valid_from='2026-10-01', system='hourly', hourly_rate='350', currency='RUB',
        bonus_kind='fixed', bonus_value='1000', fixed_supplement='500'))
    approved(data, first=2, count=1, repo=TimesheetRepository(data['db'], second))
    a = calculate(data)
    b = PayrollCalculator(data['db'], second).calculate(dict(data['period'], project_id=second), data['version'])
    fixed = lambda r: sum(line['amount_minor'] for line in r['lines'] if line['component'] in ('bonus', 'supplement'))
    assert fixed(a) == 75000 and fixed(b) == 75000


def test_performance_bonus_is_suppressed_without_personal_labor_attributions(payroll_data):
    terms(payroll_data, bonus_kind='performance', bonus_basis='base', bonus_value='20')
    approved(payroll_data, count=1)
    result = calculate(payroll_data)
    assert result['totals'] == {'RUB': 280000}
    assert result['warnings']
    assert not any(line['component'] == 'bonus' for line in result['lines'])


def test_personal_performance_bonus_requires_complete_hours_and_keeps_fractional_ratio(payroll_data, item, plan):
    from app.repositories.event_repository import EventRepository
    from app.repositories.labor_repository import LaborRepository
    data = payroll_data
    terms(data, bonus_kind='performance', bonus_basis='base', bonus_value='20')
    approved(data, count=1, hours='3')
    fact = EventRepository(data['db']).add_fact(item, '10', '2026-10-01',
        event_key='pay-personal-performance', recorded_at='2026-10-02T00:00:00Z')
    labor = LaborRepository(data['db'])
    labor.allocate(item, '2026-10-01', [dict(employee_id=data['employee'], hours='3')])
    labor.attribute(fact, data['contractor'], '10', employee_id=data['employee'])
    result = calculate(data)
    assert result['totals'] == {'RUB': 126000}
    assert result['sources']['performance'][0]['complete'] is True
    assert result['sources']['performance'][0]['ratio'].startswith('2.666666')
    correction = EventRepository(data['db']).add_fact(item, '1', '2026-10-01', correction_of_id=fact,
        comment='Добавлено', event_key='pay-performance-plus')
    reverse = EventRepository(data['db']).add_fact(item, '-1', '2026-10-01', correction_of_id=correction,
        comment='Снято', event_key='pay-performance-minus')
    corrected = calculate(data)
    assert corrected['totals'] == result['totals']
    assert corrected['sources'] != result['sources']
    assert {row['id'] for row in corrected['sources']['performance'][0]['fact_events']} == {fact, correction, reverse}
    labor.allocate(item, '2026-10-01', [dict(employee_id=data['employee'], hours='2', expected_revision=1)], reason='Уточнено распределение')
    incomplete = calculate(data)
    assert incomplete['totals'] == {'RUB': 105000}
    assert incomplete['warnings']


def test_salary_rejects_inconsistent_explicit_month_norm_across_projects(payroll_data):
    from app.repositories.project_repository import ProjectRepository
    from app.time_utils import instant
    data = payroll_data
    terms(data, system='salary', salary='80000')
    approved(data, count=1)
    other = ProjectRepository(data['db']).create_project('Объект нормы', 'PAY44NORM')
    data['people'].share_contractor(data['contractor'], other)
    now = instant()
    period_id = data['db'].execute('INSERT INTO payroll_periods(project_id,contractor_id,year,month,created_at) VALUES (?,?,?,?,?)',
        (other, data['contractor'], 2026, 10, now)).lastrowid
    data['db'].execute('INSERT INTO payroll_versions(period_id,version_no,norm_hours,created_at,updated_at) VALUES (?,1,?,?,?)',
        (period_id, '180', now, now))
    data['db'].commit()
    result = calculate(data)
    assert result['errors'] and not result['totals']


def test_piece_percentage_uses_explicit_piece_basis_and_currency_totals_stay_separate(payroll_data):
    terms(payroll_data, system='combined', base_kind='none', piece_enabled=True,
        bonus_kind='percent', bonus_basis='piece', bonus_value='20')
    approved(payroll_data, count=1)
    result = calculate(payroll_data, piece_lines=[dict(employee_id=payroll_data['employee'],
        component='piece', amount_minor=10000, currency='RUB', source={'batch_id': 7})],
        adjustments=[dict(id=1, employee_id=payroll_data['employee'], amount_minor=100, currency='USD', reason='Доплата')])
    assert result['totals'] == {'RUB': 12000, 'USD': 100}


@pytest.mark.parametrize('attendance_day,piece_day', [(1, '2026-10-20'), (20, '2026-10-01')])
def test_combined_bonus_uses_latest_attendance_or_piece_date(payroll_data, attendance_day, piece_day):
    settings = dict(system='combined', base_kind='hourly', piece_enabled=True,
        bonus_kind='percent', bonus_basis='piece')
    terms(payroll_data, **settings, valid_to='2026-10-10', bonus_value='20')
    terms(payroll_data, **settings, valid_from='2026-10-10', bonus_value='40')
    approved(payroll_data, first=attendance_day, count=1)
    result = calculate(payroll_data, piece_lines=[dict(employee_id=payroll_data['employee'],
        component='piece', amount_minor=10000, currency='RUB', source={
            'snapshot': {'dependencies': {'root': {'confirmed_date': piece_day}}}})])
    assert result['errors'] == []
    assert result['totals'] == {'RUB': 294000}
    bonus = next(line for line in result['lines'] if line['component'] == 'bonus')
    assert bonus['amount_minor'] == 4000
    assert bonus['source']['last_worked_date'] == '2026-10-20'


def test_pure_piece_payment_does_not_require_attendance(payroll_data):
    terms(payroll_data, system='piece')
    result = calculate(payroll_data, piece_lines=[dict(employee_id=payroll_data['employee'],
        component='piece', amount_minor=12345, currency='RUB', full_name='Снимок Исполнителя', source={'batch_id': 9})])
    assert result['errors'] == []
    assert result['totals'] == {'RUB': 12345}
    assert result['workers'][0]['full_name'] == 'Снимок Исполнителя'


@pytest.mark.parametrize('settings,want', [
    ({'bonus_kind': 'percent', 'bonus_basis': 'piece', 'bonus_value': '10'}, 110000),
    ({'fixed_supplement': '1000'}, 200000),
    ({'bonus_kind': 'fixed', 'bonus_value': '1000'}, 200000),
])
def test_pure_piece_monthly_bonus_uses_confirmation_date_without_attendance(payroll_data, settings, want):
    terms(payroll_data, system='piece', valid_to='2026-10-16', **settings)
    result = calculate(payroll_data, piece_lines=[dict(employee_id=payroll_data['employee'],
        component='piece', amount_minor=100000, currency='RUB', source={
            'snapshot': {'dependencies': {'root': {'confirmed_date': '2026-10-10'}}}})])
    assert result['errors'] == []
    assert result['totals'] == {'RUB': want}


def test_later_settlement_uses_piece_bonus_terms_on_original_confirmation_date(payroll_data):
    terms(payroll_data, system='piece', valid_to='2026-11-01',
        bonus_kind='percent', bonus_basis='piece', bonus_value='10')
    payroll_data['period']['month'] = 11
    result = calculate(payroll_data, piece_lines=[dict(employee_id=payroll_data['employee'],
        component='piece', amount_minor=100000, currency='RUB', source={
            'snapshot': {'dependencies': {'root': {'confirmed_date': '2026-10-20'}}}})])
    assert result['errors'] == []
    assert result['totals'] == {'RUB': 110000}
    bonus = next(line for line in result['lines'] if line['component'] == 'bonus')
    assert bonus['source']['term']['valid_to'] == '2026-11-01'
    assert bonus['source']['last_worked_date'] == '2026-10-20'
    assert bonus['source']['bonus_value'] == '10'


@pytest.mark.parametrize('settings', [
    {'bonus_kind': 'fixed', 'bonus_value': '20'},
    {'fixed_supplement': '20'},
])
def test_later_piece_settlement_warns_and_omits_expired_monthly_fixed_amount(payroll_data, settings):
    terms(payroll_data, system='piece', valid_to='2026-11-01', **settings)
    payroll_data['period']['month'] = 11
    result = calculate(payroll_data, piece_lines=[dict(employee_id=payroll_data['employee'],
        component='piece', amount_minor=100000, currency='RUB', source={
            'snapshot': {'dependencies': {'root': {'confirmed_date': '2026-10-20'}}}})])
    assert result['errors'] == []
    assert result['totals'] == {'RUB': 100000}
    assert all(line['component'] == 'piece' for line in result['lines'])
    assert result['warnings']


def test_fixed_amount_without_hours_is_ambiguous_on_multiple_projects(payroll_data):
    from app.repositories.payroll_rules_repository import PayrollRulesRepository
    from app.repositories.project_repository import ProjectRepository
    data = payroll_data
    terms(data, system='piece', fixed_supplement='1000')
    other = ProjectRepository(data['db']).create_project('Объект фиксированной доплаты', 'PAY44FIX')
    data['people'].share_contractor(data['contractor'], other)
    PersonnelRepository(data['db'], other).assign_to_project(data['employee'], '2020-01-01')
    PayrollRulesRepository(data['db'], other).save_terms(dict(employee_id=data['employee'], contractor_id=data['contractor'],
        system='piece', valid_from='2026-10-01', fixed_supplement='1000'))
    result = calculate(data, piece_lines=[dict(employee_id=data['employee'],
        component='piece', amount_minor=100000, currency='RUB', source={})])
    assert result['errors']
    assert not any(line['component'] == 'supplement' for line in result['lines'])


def test_performance_projection_reads_are_bounded_for_multiple_workers(payroll_data, item, plan):
    from app.repositories.event_repository import EventRepository
    from app.repositories.labor_repository import LaborRepository
    data = payroll_data
    second = data['people'].save_worker(dict(full_name='Второй Исполнитель', contractor_id=data['contractor'],
        specialty_id=data['people'].employee(data['employee'])['specialty_id'], effective_date='2020-01-01'))
    terms(data, bonus_kind='performance', bonus_basis='base', bonus_value='20')
    terms(dict(data, employee=second), bonus_kind='performance', bonus_basis='base', bonus_value='20')
    sheet = data['sheets'].create(data['contractor'], 2026, 10)
    data['sheets'].set_entries(sheet, [dict(employee_id=employee, work_date='2026-10-01', code='Я', hours='1')
                                    for employee in (data['employee'], second)])
    data['sheets'].transition(sheet, 'submit')
    data['sheets'].transition(sheet, 'approve')
    fact = EventRepository(data['db']).add_fact(item, '10', '2026-10-01', event_key='pay-two-performance', recorded_at='2026-10-02T00:00:00Z')
    labor = LaborRepository(data['db'])
    labor.allocate(item, '2026-10-01', [dict(employee_id=employee, hours='1') for employee in (data['employee'], second)])
    for employee in (data['employee'], second):
        labor.attribute(fact, data['contractor'], '5', employee_id=employee)
    queries = []
    data['db'].set_trace_callback(queries.append)
    try:
        result = calculate(data)
    finally:
        data['db'].set_trace_callback(None)
    assert result['totals'] == {'RUB': 84000}
    assert sum('FROM labor_hour_allocations a JOIN labor_operations' in query for query in queries) == 1
    assert sum('FROM labor_volume_attributions a JOIN labor_operations' in query for query in queries) == 1
    assert sum('SELECT f.* FROM fact_events' in query for query in queries) == 1
