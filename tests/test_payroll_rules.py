from decimal import Decimal
import sqlite3

import pytest

from app.repositories.base import ValidationError


@pytest.fixture
def payroll_staff(db):
    from app.repositories.personnel_repository import PersonnelRepository
    from app.repositories.payroll_rules_repository import PayrollRulesRepository
    people = PersonnelRepository(db)
    contractor = people.save_contractor({'name': 'Расчётный подрядчик'})
    specialty = people.save_specialty('Монтажник', 'PAY-44')
    employee = people.save_worker(dict(full_name='Расчётный Работник', contractor_id=contractor,
        specialty_id=specialty, effective_date='2020-01-01'))
    return db, PayrollRulesRepository(db), contractor, employee


def term_data(contractor, employee, **options):
    result = dict(employee_id=employee, contractor_id=contractor, valid_from='2026-10-01',
        valid_to=None, system='hourly', hourly_rate='350', currency='RUB')
    return dict(result, **options)


def test_terms_revision_is_immutable_half_open_and_preserves_cutoff(payroll_staff):
    db, repo, contractor, employee = payroll_staff
    root = repo.save_terms(term_data(contractor, employee, valid_to='2026-10-16'), operation_key='terms-first')
    original = repo.terms_on(employee, contractor, '2026-10-15')
    assert original['hourly_rate'] == '350'
    assert repo.terms_on(employee, contractor, '2026-10-16') is None
    repo.save_terms(term_data(contractor, employee, valid_to='2026-10-16', hourly_rate='400'),
                    root, expected_revision=1)
    assert repo.terms_on(employee, contractor, '2026-10-15')['hourly_rate'] == '400'
    assert repo.terms_on(employee, contractor, '2026-10-15', original['recorded_at'])['id'] == original['id']
    assert repo.save_terms(term_data(contractor, employee, valid_to='2026-10-16'), operation_key='terms-first') == root
    with pytest.raises(ValidationError):
        repo.save_terms(term_data(contractor, employee, hourly_rate='500'), root, expected_revision=1)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute('UPDATE payroll_terms SET hourly_rate=? WHERE id=?', ('999', original['id']))


def test_terms_overlap_archive_and_explicit_combined_basis(payroll_staff):
    _, repo, contractor, employee = payroll_staff
    root = repo.save_terms(term_data(contractor, employee, valid_to='2026-10-16'))
    with pytest.raises(ValidationError):
        repo.save_terms(term_data(contractor, employee, valid_from='2026-10-15'))
    next_root = repo.save_terms(term_data(contractor, employee, valid_from='2026-10-16'))
    latest = next(r for r in repo.list_terms() if r['root_id'] == next_root)
    repo.save_terms(dict(latest, status='archived'), next_root, expected_revision=1)
    assert repo.terms_on(employee, contractor, '2026-10-17') is None
    with pytest.raises(ValidationError):
        repo.save_terms(term_data(contractor, employee, valid_from='2026-11-01',
            system='combined', base_kind='salary', salary='60000', bonus_kind='percent', bonus_value='20'))
    assert root != next_root


def test_rates_prefer_contractor_then_global_with_no_whole_rate_for_operation(payroll_staff):
    _, repo, contractor, _ = payroll_staff
    base = dict(catalog_id=1, valid_from='2026-10-01', amount='100', currency='RUB')
    global_root = repo.save_rate(base)
    specific = repo.save_rate(dict(base, contractor_id=contractor, amount='125'))
    assert repo.rate_on(1, contractor, '2026-10-01')['root_id'] == specific
    operation = repo.save_operation(1, 'MIX', 'Приготовление')
    assert repo.rate_on(1, contractor, '2026-10-01', operation) is None
    op_rate = repo.save_rate(dict(base, operation_id=operation, amount='20'))
    assert repo.rate_on(1, contractor, '2026-10-01', operation)['root_id'] == op_rate
    assert repo.rate_on(1, None, '2026-10-01')['root_id'] == global_root
    with pytest.raises(ValidationError):
        repo.save_rate(dict(base, contractor_id=contractor, amount='130'))
    with pytest.raises(ValidationError):
        repo.save_rate(dict(base, catalog_id=2, operation_id=operation))


def test_money_rounds_half_up_and_rejects_inexact_input_types():
    from app.services.payroll_money import from_minor, to_minor
    assert [to_minor(v) for v in ('1.005', '-1.005', '0.004')] == [101, -101, 0]
    assert from_minor(101) == Decimal('1.01')
    for value in (1.1, True, 'NaN', 'Infinity', None):
        with pytest.raises((ValueError, TypeError, ArithmeticError)):
            to_minor(value)


def test_money_split_conserves_signed_total_with_employee_id_tie_break():
    from app.services.payroll_money import split_money
    weights = {30: Decimal(1), 10: Decimal(1), 20: Decimal(1)}
    assert split_money(100, weights) == {10: 34, 20: 33, 30: 33}
    assert split_money(-100, weights) == {10: -34, 20: -33, 30: -33}
    assert split_money(1, {1: Decimal('0.01'), 2: Decimal('0.99')}) == {1: 0, 2: 1}
    for total, invalid in ((10, {1: Decimal(0)}), (10, {1: Decimal(-1)}), (10, {}), (True, weights)):
        with pytest.raises((ValueError, TypeError, ArithmeticError)):
            split_money(total, invalid)
