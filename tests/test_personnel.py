from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3

import pytest

from app.database import connect
from app.repositories.base import ValidationError
from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.work_order_repository import WorkOrderRepository
from test_work_orders import setup_orders, draft

DATE='2026-10-20'


@pytest.fixture
def personnel(db):
    p=PersonnelRepository(db);b=BrigadeRepository(db)
    c=p.save_contractor(dict(name='ООО Строймонтаж'))
    specialty=p.save_specialty('Каменщик','КМ')
    return dict(people=p,brigades=b,contractor=c,specialty=specialty)


def worker(s,name='Иванов Иван Иванович',**fields):
    return s['people'].save_worker(dict(full_name=name,contractor_id=s['contractor'],specialty_id=s['specialty'],effective_date=DATE,**fields))


def test_manual_worker_stable_id_edit_timestamps_and_duplicate_names(personnel):
    s=personnel;e=worker(s);other=worker(s)
    assert e!=other
    before=s['people'].employee(e)
    s['people'].save_worker(dict(full_name='Иванов Иван Петрович',contractor_id=s['contractor'],specialty_id=s['specialty']),e)
    after=s['people'].employee(e)
    assert after['id']==e and after['full_name']=='Иванов Иван Петрович'
    assert before['created_at']==after['created_at'] and before['updated_at']<=after['updated_at']


@pytest.mark.parametrize('field',['full_name','contractor_id','specialty_id'])
def test_strict_worker_requires_master_fields(personnel,db,field):
    s=personnel;values=dict(full_name='Иванов Иван',contractor_id=s['contractor'],specialty_id=s['specialty']);values.pop(field)
    with pytest.raises(ValidationError):s['people'].save_worker(values)
    assert db.execute('SELECT count(*) FROM employees').fetchone()[0]==0


def test_worker_archive_restore_and_no_physical_delete(db,personnel):
    s=personnel;e=worker(s)
    s['people'].set_active('employees',e,False)
    assert not s['people'].list_employees(active_only=True,as_of=DATE)
    with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM employees WHERE id=?',(e,))
    s['people'].set_active('employees',e,True)
    assert s['people'].list_employees(active_only=True,as_of=DATE)[0]['id']==e


def test_brigade_number_string_uniqueness_per_employer(personnel):
    s=personnel;b=s['brigades'];c=s['contractor'];ids=[b.save(c,x) for x in ['1','01','Б-01','МОН-02']]
    c2=s['people'].save_contractor(dict(name='Другая организация'))
    assert b.save(c2,'1')
    assert {b.get(i)['number'] for i in ids}=={'1','01','Б-01','МОН-02'}
    with pytest.raises(ValidationError):b.save(c,'01')


def test_membership_transfer_exclusion_and_history(personnel):
    s=personnel;e=worker(s);b=s['brigades'];one=b.save(s['contractor'],'1');two=b.save(s['contractor'],'2')
    b.assign(e,one,DATE);b.assign(e,two,'2026-10-21');b.assign(e,None,'2026-10-22')
    assert b.current(e,DATE)['brigade_id']==one
    assert b.current(e,'2026-10-21')['brigade_id']==two
    assert b.current(e,'2026-10-22') is None
    assert len(b.history(employee_id=e))==2
    assert not b.members(one,'2026-10-21')


def test_membership_registration_cutoff_preserves_previous_state(personnel,monkeypatch):
    s=personnel;e=worker(s);b=s['brigades'];one=b.save(s['contractor'],'1');two=b.save(s['contractor'],'2')
    monkeypatch.setattr('app.repositories.brigade_repository.instant',lambda value=None:'2026-10-20T12:00:00.000000Z' if value is None else value)
    b.assign(e,one,DATE)
    monkeypatch.setattr('app.repositories.brigade_repository.instant',lambda value=None:'2026-10-22T12:00:00.000000Z' if value is None else value)
    b.assign(e,two,DATE)
    assert b.current(e,DATE,'2026-10-21T00:00:00.000000Z')['brigade_id']==one
    assert b.current(e,DATE,'2026-10-23T00:00:00.000000Z')['brigade_id']==two


def test_database_rejects_overlapping_membership_and_history_mutation(db,personnel):
    s=personnel;e=worker(s);b=s['brigades'];one=b.save(s['contractor'],'1');two=b.save(s['contractor'],'2');b.assign(e,one,DATE)
    with pytest.raises(sqlite3.IntegrityError):db.execute('INSERT INTO brigade_assignments(employee_id,brigade_id,start_date,recorded_at) VALUES (?,?,?,?)',(e,two,DATE,'2026-10-20T23:00:00Z'))
    with pytest.raises(sqlite3.IntegrityError):db.execute("UPDATE brigade_assignments SET start_date='2026-10-19'")
    with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM brigade_assignments')
    assert len(b.history(employee_id=e))==1


def test_brigade_archive_requires_member_confirmation_and_restore(personnel):
    s=personnel;e=worker(s);b=s['brigades'];one=b.save(s['contractor'],'1');b.assign(e,one,DATE)
    with pytest.raises(ValidationError):b.set_active(one,False,effective_date='2026-10-21')
    b.set_active(one,False,effective_date='2026-10-21',confirm_members=True)
    assert not b.get(one)['is_active'] and b.get(one)['archived_at']
    assert b.current(e,DATE)['brigade_id']==one and b.current(e,'2026-10-21') is None
    b.set_active(one,True);assert b.get(one)['is_active'] and b.get(one)['archived_at'] is None
    assert len(b.history(brigade_id=one))==1


def test_specialty_normalization_and_alias_preserve_id(personnel):
    p=personnel['people'];sid=personnel['specialty']
    assert p.specialty_by_name('  КАМЕНЩИК  ')['id']==sid
    with pytest.raises(ValidationError):p.save_specialty(' каменщик ','ДРУГОЙ')
    p.save_specialty('Каменщик строительный','КМ',sid)
    assert p.specialty_by_name('Каменщик')['id']==p.specialty_by_name('Каменщик строительный')['id']==sid


def test_worker_filters_by_name_employer_specialty_brigade(personnel):
    s=personnel;e=worker(s);worker(s,'Петров Пётр Сергеевич');b=s['brigades'].save(s['contractor'],'01');s['brigades'].assign(e,b,DATE)
    for args in [dict(search='ИВАН'),dict(brigade_id=b)]:assert [r['id'] for r in s['people'].list_employees(as_of=DATE,**args)]==[e]
    assert len(s['people'].list_employees(contractor_id=s['contractor'],specialty_id=s['specialty']))==2


def test_same_worker_on_two_projects_without_copy_and_period_end(db,personnel):
    s=personnel;e=worker(s);other=ProjectRepository(db).create_project('Другой объект','OTHER')
    p=PersonnelRepository(db,other)
    with pytest.raises(ValidationError):p.assign_to_project(e,DATE)
    s['people'].share_contractor(s['contractor'],other)
    p.assign_to_project(e,DATE)
    assert p.employee(e)['id']==e and db.execute('SELECT count(*) FROM employees').fetchone()[0]==1
    p.end_project_assignment(e,'2026-10-21')
    assert p.available(e,DATE) and not p.available(e,'2026-10-21')
    assert s['people'].available(e,DATE)
    assert len(p.project_history(e))==2


def test_other_project_or_employer_cannot_mutate_or_read_personnel(db,personnel):
    s=personnel;e=worker(s);b=s['brigades'].save(s['contractor'],'1')
    other=ProjectRepository(db).create_project('Другой','OTHER');p=PersonnelRepository(db,other);br=BrigadeRepository(db,other)
    for operation in [lambda:p.employee(e),lambda:br.get(b),lambda:br.current(e),lambda:p.set_active('employees',e,False),lambda:br.assign(e,b,DATE)]:
        with pytest.raises(ValidationError):operation()
    c2=s['people'].save_contractor(dict(name='Другой подрядчик'));b2=s['brigades'].save(c2,'1')
    with pytest.raises(ValidationError):s['brigades'].assign(e,b2,DATE)


def test_order_accepts_multiple_brigades_and_deduplicates_individual_selection(db,setup_orders):
    s=setup_orders;b=BrigadeRepository(db);b1=b.save(s['contractor'],'01');b2=b.save(s['contractor'],'02')
    for group,bid in zip(s['teams'],[b1,b2]):
        for e in group:b.assign(e,bid,'2026-10-17')
    crew=[dict(specialty_id=s['specialties'][0],employee_ids=[s['teams'][0][0]],planned_count=None)]
    oid=s['orders'].save_draft(s['header'],s['lines'],crew,brigade_ids=[b1,b2])
    order=s['orders'].get_order(oid)
    assert sum(len(c['employee_ids']) for c in order['crew'])==7
    assert {tuple(c['brigades']) for c in order['crew']}=={('01',),('02',)}


def test_issued_order_personnel_snapshot_unchanged_after_transfer_and_rename(db,setup_orders):
    s=setup_orders;b=BrigadeRepository(db);b1=b.save(s['contractor'],'01');b2=b.save(s['contractor'],'02')
    for e in s['teams'][0]:b.assign(e,b1,'2026-10-17')
    oid=s['orders'].save_draft(s['header'],s['lines'],brigade_ids=[b1]);s['orders'].issue(oid)
    before=s['orders'].get_order(oid)['crew']
    b.assign(s['teams'][0][1],b2,'2026-10-18');b.save(s['contractor'],'НОВЫЙ',b1)
    s['people'].save_employee(dict(full_name='Другое ФИО'),s['teams'][0][0])
    assert s['orders'].get_order(oid)['crew']==before
    assert before[0]['brigades']==['01']


def test_shared_employee_can_be_order_responsible_and_crew_in_new_project(db,setup_orders):
    s=setup_orders;other=ProjectRepository(db).create_project('Школа 2','OTHER',buildings=[dict(code='1',name='Учебный')])
    s['people'].share_contractor(s['contractor'],other);p=PersonnelRepository(db,other)
    eid=s['teams'][0][0];p.assign_to_project(eid,'2026-10-17')
    o=WorkOrderRepository(db,other)
    oid=o.save_draft(dict(s['header'],issued_by_employee_id=eid,foreman_employee_id=eid),crew=[dict(specialty_id=s['specialties'][0],employee_ids=[eid],planned_count=None)])
    assert o.get_order(oid)['crew'][0]['employee_ids']==[eid]
    assert db.execute('SELECT count(*) FROM employees WHERE id=?',(eid,)).fetchone()[0]==1


def test_concurrent_membership_transfers_leave_one_open_period(db,personnel):
    s=personnel;e=worker(s);bs=[s['brigades'].save(s['contractor'],str(i)) for i in (1,2)]
    path=db.execute('PRAGMA database_list').fetchone()[2];barrier=Barrier(2)
    def assign(b):
        conn=connect(path)
        try:barrier.wait();BrigadeRepository(conn).assign(e,b,DATE)
        finally:conn.close()
    with ThreadPoolExecutor(2) as pool:list(pool.map(assign,bs))
    assert db.execute('SELECT count(*) FROM brigade_assignments WHERE end_date IS NULL').fetchone()[0]==1


def test_stale_brigade_removal_does_not_remove_new_membership(personnel):
    s=personnel;e=worker(s);b=s['brigades'];one=b.save(s['contractor'],'1');two=b.save(s['contractor'],'2')
    b.assign(e,one,DATE);b.assign(e,two,DATE)
    with pytest.raises(ValidationError,match='Назначение изменилось'):b.remove(e,one,DATE)
    assert b.current(e,DATE)['brigade_id']==two
