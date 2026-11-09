"""Independent high-risk payroll review regressions; synthetic SQLite only."""
from decimal import Decimal
import sqlite3

import pytest

from app.repositories.event_repository import EventRepository
from app.repositories.payroll_piece_repository import PayrollPieceRepository
from app.repositories.payroll_repository import PayrollRepository
from app.repositories.payroll_rules_repository import PayrollRulesRepository
from app.repositories.personnel_repository import PersonnelRepository


@pytest.fixture
def review_piece_case(db, item, plan):
    people = PersonnelRepository(db)
    contractor = people.save_contractor({'name': 'Независимая проверка начислений'})
    specialty = people.save_specialty('Работник проверки', 'PAY-REVIEW')
    employee = people.save_worker(dict(full_name='Работник без табеля', contractor_id=contractor,
        specialty_id=specialty, effective_date='2020-01-01'))
    events = EventRepository(db)
    events.add_fact(item, '100', '2026-10-01', event_key='review-fact')
    confirmation = events.add_confirmation(item, '100', '2026-10-01',
        event_key='review-confirmation', document_ref='Синтетический акт')
    rules = PayrollRulesRepository(db)
    term = rules.save_terms(dict(employee_id=employee, contractor_id=contractor,
        valid_from='2026-01-01', system='combined', base_kind='none', piece_enabled=True,
        bonus_kind='percent', bonus_basis='piece', bonus_value='20'))
    catalog = db.execute('SELECT catalog_id FROM work_items WHERE id=?', (item,)).fetchone()[0]
    unit = db.execute('SELECT unit FROM work_catalog WHERE id=?', (catalog,)).fetchone()[0]
    rate = rules.save_rate(dict(catalog_id=catalog, contractor_id=contractor, amount='1',
        unit=unit, valid_from='2026-01-01'))
    repo = PayrollRepository(db)
    period = repo.create_period(contractor, 2026, 10)
    version = repo.get_version(period)['id']
    piece = PayrollPieceRepository(db)
    claim = piece.save_claim(dict(confirmation_event_id=confirmation, contractor_id=contractor,
        employee_id=employee, quantity='100', proof_ref='Подтверждённый персональный объём'))
    preview = piece.preview(version, claim, '100', 'equal', [dict(employee_id=employee)])
    piece.confirm(version, preview['id'])
    return dict(db=db, item=item, repo=repo, period=period, version=version, rules=rules,
        piece=piece, events=events, confirmation=confirmation, employee=employee,
        contractor=contractor, term=term, catalog=catalog, unit=unit, rate=rate)


def test_piece_percentage_bonus_is_paid_without_attendance(review_piece_case):
    d = review_piece_case
    assert d['db'].execute('SELECT COUNT(*) FROM timesheets').fetchone()[0] == 0
    d['repo'].calculate(d['version'])
    report = d['repo'].report(d['period'])
    assert report['errors'] == []
    assert report['rows'][0]['piece'] == Decimal('100')
    assert report['rows'][0]['bonus'] == Decimal('20')
    assert report['totals'] == {'RUB': Decimal('120')}


def test_confirmation_drift_is_visible_on_frozen_piece_report(review_piece_case):
    d = review_piece_case
    d['repo'].calculate(d['version'])
    d['repo'].transition(d['version'], 'submit')
    d['repo'].transition(d['version'], 'approve')
    before = d['repo'].report(d['period'])
    d['events'].add_confirmation(d['item'], '-10', '2026-10-01',
        event_key='review-confirmation-reduction', document_ref='Уточнённый акт',
        correction_of_id=d['confirmation'], comment='Исправленный объём')
    assert d['piece'].validate(d['version'])['errors']
    after = d['repo'].report(d['period'])
    assert after['totals'] == before['totals']
    assert after['lines'] == before['lines']
    assert after['source_changed'] is True


def test_current_calculation_run_cannot_gain_additional_accruals(review_piece_case):
    d = review_piece_case
    run = d['repo'].calculate(d['version'])
    with pytest.raises(sqlite3.IntegrityError):
        d['db'].execute('INSERT INTO payroll_accruals(run_id,employee_id,component,amount_minor,'
            'currency,source_json) VALUES (?,?,?,?,?,?)',
            (run, d['employee'], 'adjustment', 100000, 'RUB', '{}'))


def test_replaced_calculation_run_cannot_gain_additional_accruals(review_piece_case):
    d = review_piece_case
    old_run = d['repo'].calculate(d['version'])
    d['repo'].calculate(d['version'])
    with pytest.raises(sqlite3.IntegrityError):
        d['db'].execute('INSERT INTO payroll_accruals(run_id,employee_id,component,amount_minor,'
            'currency,source_json) VALUES (?,?,?,?,?,?)',
            (old_run, d['employee'], 'adjustment', 100000, 'RUB', '{}'))


def test_prior_month_piece_can_settle_after_source_terms_end(review_piece_case):
    d = review_piece_case
    batch = d['piece'].batches(d['version'])[0]['id']
    d['piece'].cancel_batch(d['version'], batch, 'Начислить в следующем месяце')
    term = next(row for row in d['rules'].list_terms() if row['root_id'] == d['term'])
    d['rules'].save_terms(dict(term, valid_to='2026-11-01'),
        term_id=d['term'], expected_revision=term['revision_no'])
    period = d['repo'].create_period(d['contractor'], 2026, 11)
    version = d['repo'].get_version(period)['id']
    claim = d['piece'].list_claims()[0]['claim_id']
    preview = d['piece'].preview(version, claim, '100', 'equal',
        [dict(employee_id=d['employee'])], reason='Позднее начисление по октябрьскому акту')
    d['piece'].confirm(version, preview['id'])
    d['repo'].calculate(version)
    report = d['repo'].report(period)
    assert report['errors'] == []
    assert report['rows'][0]['piece'] == Decimal('100')


def test_reduced_confirmation_correction_can_replace_recipient_claim(review_piece_case):
    d = review_piece_case
    d['repo'].calculate(d['version'])
    d['repo'].transition(d['version'], 'submit')
    d['repo'].transition(d['version'], 'approve')
    d['events'].add_confirmation(d['item'], '-10', '2026-10-01',
        event_key='review-reassign-reduction', document_ref='Уточнённый акт',
        correction_of_id=d['confirmation'], comment='Исправленный объём')
    old_claim = d['piece'].list_claims()[0]
    d['piece'].save_claim(dict(quantity='0', proof_ref='Отмена прежнего исполнителя', reason='Уточнить исполнителя'),
        claim_id=old_claim['claim_id'], expected_revision=old_claim['revision_no'])
    people = PersonnelRepository(d['db'])
    other = people.save_worker(dict(full_name='Правильный получатель', contractor_id=d['contractor'],
        specialty_id=people.employee(d['employee'])['specialty_id'], effective_date='2020-01-01'))
    d['rules'].save_terms(dict(employee_id=other, contractor_id=d['contractor'],
        valid_from='2026-01-01', system='piece'))
    new_claim = d['piece'].save_claim(dict(confirmation_event_id=d['confirmation'],
        contractor_id=d['contractor'], employee_id=other, quantity='90', proof_ref='Исправленный исполнитель'))
    version = d['repo'].create_correction(d['period'], 'Уточнить объём и исполнителя')
    copied = d['piece'].batches(version)[0]['id']
    d['piece'].cancel_batch(version, copied, 'Заменить ошибочное назначение')
    preview = d['piece'].preview(version, new_claim, '90', 'equal', [dict(employee_id=other)])
    d['piece'].confirm(version, preview['id'])
    d['repo'].calculate(version)
    assert d['repo'].report(d['period'])['errors'] == []
    d['repo'].transition(version, 'submit')
    d['repo'].transition(version, 'approve')
    assert d['repo'].report(d['period'])['totals'] == {'RUB': Decimal('90')}
    assert d['repo'].report(d['period'], version_id=d['version'])['totals'] == {'RUB': Decimal('120')}
    assert next(row for row in d['piece'].list_claims() if row['claim_id'] == new_claim)['reserved'] == Decimal('90')
