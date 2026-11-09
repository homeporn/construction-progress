"""Populated012 upgrade includes immutable payroll and legacy partial closure."""
from pathlib import Path
import shutil
import sqlite3

import pytest

import app.database as database
from app.config import PROJECT_ROOT
from app.calculation_engine.historical import HistoricalCalculator
from app.repositories.event_repository import EventRepository
from app.repositories.payroll_repository import PayrollRepository
from app.repositories.payroll_rules_repository import PayrollRulesRepository
from app.repositories.work_order_repository import WorkOrderRepository
from app.repositories.work_order_workflow_repository import WorkOrderWorkflowRepository
from test_payroll_migration import pre44


@pytest.fixture
def pre45(pre44,tmp_path,monkeypatch):
    db,_,plan,root=pre44
    previous=tmp_path/'stage44';(previous/'migrations').mkdir(parents=True)
    for path in (PROJECT_ROOT/'migrations').glob('*.sql'):
        if path.name<'013':shutil.copyfile(path,previous/'migrations'/path.name)
    with monkeypatch.context() as patch:
        patch.setattr(database,'PROJECT_ROOT',previous);database.migrate(db)
    employee=db.execute('SELECT id,contractor_id FROM employees WHERE specialty_id IS NOT NULL ORDER BY id LIMIT 1').fetchone()
    rules=PayrollRulesRepository(db)
    rules.save_terms(dict(employee_id=employee['id'],contractor_id=employee['contractor_id'],valid_from='2020-01-01',system='hourly',hourly_rate='350'))
    payroll=PayrollRepository(db);period=payroll.create_period(employee['contractor_id'],2026,1,norm_hours='160')
    version=payroll.get_version(period)['id'];payroll.calculate(version);payroll.transition(version,'submit');payroll.transition(version,'approve')
    # This really is the pre013 policy; retain its original decision, do not rewrite it.
    order=db.execute('SELECT id FROM work_orders ORDER BY id LIMIT 1').fetchone()[0]
    line=db.execute('SELECT id,work_item_id FROM work_order_lines WHERE work_order_id=?',(order,)).fetchone()
    from app.time_utils import instant
    stamp=instant()[:10];events=EventRepository(db)
    events.add_fact(line['work_item_id'],'100',stamp,event_key='legacy45-linked-fact',work_order_line_id=line['id'])
    events.add_confirmation(line['work_item_id'],'60',stamp,event_key='legacy45-linked-confirmation',document_ref='Прежний акт',work_order_line_id=line['id'])
    flow=WorkOrderWorkflowRepository(db)
    flow.act(order,'submitted',employee['id'],event_key='legacy45-submit')
    flow.act(order,'accepted',employee['id'],checks={line['id']:dict(checked=True)},event_key='legacy45-accept')
    flow.act(order,'closed',employee['id'],comment='Историческое решение до013',event_key='legacy45-close')
    columns={r[0]:[c[1] for c in db.execute('PRAGMA table_info("'+r[0]+'")')] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='schema_migrations'")}
    return dict(db=db,columns=columns,plan=plan,root=root,period=period,order=order,line=line['id'],payroll=payroll)


def test_013_preserves_every_previous_row_snapshot_and_checked_backup(pre45):
    d=pre45;db=d['db'];before=database._fingerprints(db,d['columns'])[1]
    original=WorkOrderRepository(db).get_order(d['order'])['closed_snapshot']
    pay=d['payroll'].report(d['period']);assert str(pay['totals']['RUB'])=='2800.00'
    progress=HistoricalCalculator(db).calculate_as_of('2026-01-31','2026-02-01T00:00:00Z',d['plan'])
    database.migrate(db);database.migrate(db)
    assert database._fingerprints(db,d['columns'])[1]==before
    assert WorkOrderRepository(db).get_order(d['order'])['closed_snapshot']==original
    assert d['payroll'].report(d['period'])['totals']==pay['totals']
    assert not d['payroll'].report(d['period'])['source_changed']
    assert HistoricalCalculator(db).calculate_as_of('2026-01-31','2026-02-01T00:00:00Z',d['plan'])==progress
    assert str(progress.total.actual_progress)=='0.6' and str(progress.total.confirmed_progress)=='0.45'
    copies=list((d['root']/'backups').glob('before-work-order-control-*.sqlite3'));assert len(copies)==1
    with sqlite3.connect(copies[0]) as copy:
        copy.row_factory=sqlite3.Row
        assert copy.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not copy.execute('PRAGMA foreign_key_check').fetchall()
        assert copy.execute('SELECT count(*) FROM schema_migrations').fetchone()[0]==12
        assert database._fingerprints(copy,d['columns'])[1]==before
    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('failure',['sql','data'])
def test_failed_013_rolls_back_schema_triggers_and_data(pre45,tmp_path,monkeypatch,failure):
    d=pre45;db=d['db'];before=database._fingerprints(db,d['columns'])[1]
    old_trigger=db.execute("SELECT sql FROM sqlite_master WHERE name='work_orders_update_guard'").fetchone()[0]
    broken=tmp_path/'broken45';shutil.copytree(PROJECT_ROOT/'migrations',broken/'migrations')
    file=broken/'migrations/013_work_order_control.sql'
    file.write_text(file.read_text()+('\nINSERT INTO missing_stage45 VALUES (1);\n' if failure=='sql' else "\nUPDATE work_catalog SET name='corrupt' WHERE id=1;\n"))
    monkeypatch.setattr(database,'PROJECT_ROOT',broken)
    with pytest.raises((sqlite3.Error,RuntimeError)):database.migrate(db)
    assert database._fingerprints(db,d['columns'])[1]==before
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='work_order_transfers'").fetchone()
    assert db.execute("SELECT sql FROM sqlite_master WHERE name='work_orders_update_guard'").fetchone()[0]==old_trigger
    assert not db.execute("SELECT 1 FROM schema_migrations WHERE name='013_work_order_control.sql'").fetchone()
    assert len(list((d['root']/'backups').glob('before-work-order-control-*.sqlite3')))==1
    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_transfers_leave_time_labor_payroll_and_physical_results_unchanged(pre45):
    from app.repositories.work_order_transfer_repository import WorkOrderTransferRepository
    from app.time_utils import instant
    d=pre45;db=d['db'];database.migrate(db)
    columns={k:v for k,v in d['columns'].items() if not k.startswith('work_order') and k!='audit_log'}
    before=database._fingerprints(db,columns)[1];pay=d['payroll'].report(d['period'])
    progress=HistoricalCalculator(db).calculate_as_of(instant()[:10],instant(),d['plan'])
    old=WorkOrderRepository(db).get_order(d['order'])
    header={key:old[key] for key in ('contractor_id','issued_by_employee_id','foreman_employee_id','shift_start','shift_end')}
    header['shift_date']=instant()[:10]
    crew=[dict(specialty_id=c['specialty_id'],employee_ids=c['employee_ids']) for c in old['crew']]
    result=WorkOrderTransferRepository(db).transfer(d['order'],[dict(source_line_id=d['line'],quantity='20')],header,crew,reason='Разрешение исторического остатка',operation_key='preserve45')
    assert WorkOrderRepository(db).get_order(result['order_id'])['status']=='draft'
    assert database._fingerprints(db,columns)[1]==before
    assert d['payroll'].report(d['period'])['totals']==pay['totals']
    after=HistoricalCalculator(db).calculate_as_of(progress.report_date,progress.knowledge_cutoff,d['plan'])
    assert after==progress


def test_old_closed_order_with_revoked_confirmation_is_visible_in_discrepancies(pre45):
    from app.repositories.work_order_control_repository import WorkOrderControlRepository
    from app.time_utils import instant
    d=pre45;db=d['db'];database.migrate(db)
    original=db.execute('SELECT id,work_item_id FROM confirmation_events WHERE work_order_line_id=? AND correction_of_id IS NULL',(d['line'],)).fetchone()
    EventRepository(db).add_confirmation(original['work_item_id'],'-60',instant()[:10],document_ref='Уточнён акт',event_key='old-closed-revoke',correction_of_id=original['id'],comment='Подтверждение полностью отозвано')
    repo=WorkOrderControlRepository(db)
    rows=repo.registry({'discrepancies':True})['rows']
    assert any(row['id']==d['order'] for row in rows)
    assert any(row['order_id']==d['order'] for row in repo.analytics()['discrepancies'])
