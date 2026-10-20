from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED
import hashlib
import sqlite3

import pytest

from app.database import connect
from app.repositories.base import ValidationError
from app.repositories.project_repository import ProjectRepository
from app.services.personnel_import import PersonnelImportService
from app.services.personnel_template import make_template
from app.services.personnel_xlsx import parse_workers_xlsx,HEADERS,MAX_ROWS,MAX_UPLOAD
from app.services.xlsx_export import pack_workbook,sheet
from test_personnel import personnel,worker,DATE

EXAMPLE=[['Иванов Иван Иванович','1','Каменщик'],['Петров Пётр Сергеевич','1','Каменщик'],
         ['Сидоров Алексей Олегович','2','Штукатур'],['Смирнов Дмитрий Андреевич','','Маляр']]


def book(rows=None,headers=HEADERS,names=None):
    names=names or ['Работники']
    return pack_workbook(names,[sheet(list(headers),EXAMPLE if rows is None else rows,exact_column=False,text_columns=(2,)) for _ in names])


def edit_zip(payload,path,transform):
    out=BytesIO()
    with ZipFile(BytesIO(payload)) as src,ZipFile(out,'w',ZIP_DEFLATED) as dst:
        for i in src.infolist():dst.writestr(i.filename,transform(src.read(i.filename)) if i.filename==path else src.read(i.filename))
    return out.getvalue()


def uploaded(db,s,rows=None,**kwargs):
    service=PersonnelImportService(db)
    sid=service.start(s['contractor'],DATE)
    sid=service.upload(sid,book(rows,**kwargs),'workers.xlsx',revision=1)
    return service,sid


def preview(service,sid,overrides=None):
    rows=service.rows(sid)
    choices={r['row_number']:dict(decision=r['decision'],employee_id=r['employee_id'],acknowledge=r['acknowledge'],clear_brigade=r['clear_brigade']) for r in rows}
    for n,data in (overrides or {}).items():choices[n].update(data)
    service.decide(sid,choices,revision=service.get(sid)['revision'])
    return service.preview(sid,revision=service.get(sid)['revision'])


def commit(service,sid):return service.confirm(sid,revision=service.get(sid)['revision'],confirm_new=True)


def counts(db):return {t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['employees','brigades','specialties','brigade_assignments','employee_projects']}


def test_school_import_four_workers_two_brigades_and_one_unassigned(db,personnel):
    service,sid=uploaded(db,personnel);plan=preview(service,sid)
    assert plan['counts']['new_workers']==4 and plan['counts']['new_brigades']==2 and plan['counts']['new_specialties']==2
    result=commit(service,sid)
    assert len(result['employee_ids'])==4
    assert counts(db)==dict(employees=4,brigades=2,specialties=3,brigade_assignments=3,employee_projects=4)
    assert [len(service.brigades.members(b['id'],DATE)) for b in service.brigades.list(as_of=DATE)]==[2,1]
    assert service.brigades.current(result['employee_ids'][3],DATE) is None


def test_import_one_hundred_workers(db,personnel):
    rows=[['Работник Номер'+str(i),str(i%5),'Каменщик'] for i in range(100)]
    service,sid=uploaded(db,personnel,rows);preview(service,sid);commit(service,sid)
    assert counts(db)['employees']==100 and counts(db)['brigades']==5


def test_preview_and_cancel_do_not_write_personnel_masters(db,personnel):
    before=counts(db);service,sid=uploaded(db,personnel);preview(service,sid)
    assert counts(db)==before
    service.cancel(sid)
    with pytest.raises(ValidationError):commit(service,sid)
    assert counts(db)==before


def test_normalized_names_specialties_and_leading_zero_brigades(db,personnel):
    rows=[['  Иванов   Иван  Иванович  ','01',' КАМЕНЩИК  '],['Петров Пётр Сергеевич','1','Каменщик']]
    service,sid=uploaded(db,personnel,rows);preview(service,sid);result=commit(service,sid)
    assert service.people.employee(result['employee_ids'][0])['full_name']=='Иванов Иван Иванович'
    assert counts(db)['specialties']==1 and {b['number'] for b in service.brigades.list(as_of=DATE)}=={'1','01'}


def test_reordered_headers_and_only_workers_sheet_imported():
    payload=book([['Штукатур','Иванов Иван','01']],headers=['Специальность','ФИО','Номер бригады'],names=['Пример','Работники','Инструкция'])
    assert parse_workers_xlsx(payload)[0]['brigade_number']=='01'
    assert len(parse_workers_xlsx(payload))==1
    assert parse_workers_xlsx(make_template())==[]


@pytest.mark.parametrize('rows,fragment',[
    ([['','1','Каменщик']],'ФИО'),([['Иванов Иван','','']],'Специальность'),
    ([[123,'1','Каменщик']],'текстовое'),([['Иванов Иван','1',123]],'текстовое'),
    ([['Иванов','','Каменщик']],'два или три')])
def test_bad_rows_have_concrete_errors(rows,fragment):
    assert any(fragment in e for e in parse_workers_xlsx(book(rows))[0]['errors'])


def test_excel_boolean_brigade_is_rejected():
    payload=book([['Иванов Иван',1,'Каменщик']])
    payload=edit_zip(payload,'xl/worksheets/sheet1.xml',lambda x:x.replace(b'<c r="B2" t="n"',b'<c r="B2" t="b"'))
    assert any('текстовое' in e for e in parse_workers_xlsx(payload)[0]['errors'])


@pytest.mark.parametrize('headers',[['ФИО','Специальность'],['ФИО','Номер бригады'],['ФИО','ФИО','Специальность']])
def test_missing_or_duplicate_headers(headers):
    with pytest.raises(ValidationError):parse_workers_xlsx(book([],headers=headers))


def test_missing_workers_sheet_lists_available_sheets():
    with pytest.raises(ValidationError,match='Пример, Инструкция'):parse_workers_xlsx(book([],names=['Пример','Инструкция']))


def test_formula_with_cached_value_is_row_error_not_executed():
    payload=book([['Иванов Иван','1','Каменщик']])
    payload=edit_zip(payload,'xl/worksheets/sheet1.xml',lambda x:x.replace(b'<c r="A2" t="inlineStr" s="0">',b'<c r="A2" t="inlineStr" s="0"><f>HYPERLINK("https://example.com")</f><v>cached</v>'))
    errors=parse_workers_xlsx(payload)[0]['errors']
    assert any('формулы запрещены' in e for e in errors)


def test_numeric_brigade_with_zero_number_mask_preserves_zero():
    payload=book([['Иванов Иван',1,'Каменщик']])
    payload=edit_zip(payload,'xl/styles.xml',lambda x:x.replace(b'formatCode="0.#########"',b'formatCode="00"'))
    assert parse_workers_xlsx(payload)[0]['brigade_number']=='01'


@pytest.mark.parametrize('payload',[b'not xlsx',b'PK\x03\x04broken'])
def test_invalid_xlsx_content(payload):
    with pytest.raises(ValidationError):parse_workers_xlsx(payload)


def test_upload_size_and_row_limits():
    with pytest.raises(ValidationError,match='5 МиБ'):parse_workers_xlsx(b'PK\x03\x04'+b'x'*MAX_UPLOAD)
    payload=book([['Иванов Иван','','Каменщик']]*(MAX_ROWS+1))
    with pytest.raises(ValidationError,match='5000'):parse_workers_xlsx(payload)


@pytest.mark.parametrize('attack',['external','entity','macro','traversal'])
def test_untrusted_workbook_parts_rejected(attack):
    payload=book()
    if attack=='external':payload=edit_zip(payload,'xl/_rels/workbook.xml.rels',lambda x:x.replace(b'Target="styles.xml"',b'Target="https://example.com" TargetMode="External"'))
    elif attack=='entity':payload=edit_zip(payload,'xl/worksheets/sheet1.xml',lambda x:b'<!DOCTYPE x [<!ENTITY a "unsafe">]>'+x)
    else:
        out=BytesIO()
        with ZipFile(BytesIO(payload)) as src,ZipFile(out,'w',ZIP_DEFLATED) as dst:
            for i in src.infolist():dst.writestr(i.filename,src.read(i.filename))
            dst.writestr('xl/vbaProject.bin' if attack=='macro' else '../outside.xml',b'x')
        payload=out.getvalue()
    with pytest.raises(ValidationError):parse_workers_xlsx(payload)


def test_existing_name_requires_explicit_candidate_selection_and_no_update(db,personnel):
    eid=worker(personnel);service,sid=uploaded(db,personnel,[EXAMPLE[0]])
    assert service.rows(sid)[0]['decision']==''
    assert preview(service,sid)['errors']
    with pytest.raises(ValidationError):commit(service,sid)
    preview(service,sid,{2:dict(decision='link',employee_id=eid)})
    commit(service,sid)
    assert counts(db)['employees']==1 and counts(db)['brigade_assignments']==0


def test_two_distinct_homonyms_can_be_created_only_after_explicit_ack(db,personnel):
    first=worker(personnel);service,sid=uploaded(db,personnel,[EXAMPLE[0]])
    assert preview(service,sid,{2:dict(decision='create')})['errors']
    preview(service,sid,{2:dict(decision='create',acknowledge=True)})
    result=commit(service,sid)
    assert first!=result['employee_ids'][0] and counts(db)['employees']==2


def test_duplicate_rows_require_decision_not_automatic_copy(db,personnel):
    service,sid=uploaded(db,personnel,[EXAMPLE[0],EXAMPLE[0]])
    assert all(r['duplicate'] and r['decision']=='' for r in service.rows(sid))
    preview(service,sid,{2:dict(decision='create',acknowledge=True),3:dict(decision='skip')});commit(service,sid)
    assert counts(db)['employees']==1


def test_duplicate_candidate_id_in_two_rows_rejected(db,personnel):
    eid=worker(personnel);service,sid=uploaded(db,personnel,[EXAMPLE[0],EXAMPLE[0]])
    plan=preview(service,sid,{n:dict(decision='link',employee_id=eid) for n in [2,3]})
    assert plan['errors']
    with pytest.raises(ValidationError):commit(service,sid)


def test_update_assignment_requires_ack_and_blank_does_not_clear(db,personnel):
    s=personnel;eid=worker(s);bid=s['brigades'].save(s['contractor'],'01');s['brigades'].assign(eid,bid,DATE)
    service,sid=uploaded(db,s,[[EXAMPLE[0][0],'','Маляр']])
    assert preview(service,sid,{2:dict(decision='update',employee_id=eid)})['errors']
    preview(service,sid,{2:dict(decision='update',employee_id=eid,acknowledge=True)});commit(service,sid)
    assert service.brigades.current(eid,DATE)['brigade_id']==bid
    assert service.people.specialty(service.people.employee(eid)['specialty_id'])['name']=='Маляр'


def test_blank_brigade_can_be_explicitly_cleared(db,personnel):
    s=personnel;eid=worker(s);bid=s['brigades'].save(s['contractor'],'01');s['brigades'].assign(eid,bid,DATE)
    service,sid=uploaded(db,s,[[EXAMPLE[0][0],'','Каменщик']])
    preview(service,sid,{2:dict(decision='update',employee_id=eid,acknowledge=True,clear_brigade=True)});commit(service,sid)
    assert service.brigades.current(eid,DATE) is None


def test_repeat_file_and_double_confirmation_do_not_duplicate_or_reset_transfer(db,personnel):
    s=personnel;service,sid=uploaded(db,s);preview(service,sid);result=commit(service,sid);before=counts(db)
    assert commit(service,sid)==result and counts(db)==before
    e=result['employee_ids'][1];second=service.db.execute("SELECT id FROM brigades WHERE number='2'").fetchone()[0]
    service.brigades.assign(e,second,'2026-10-21')
    new=service.start(s['contractor'],DATE);returned=service.upload(new,book(),'again.xlsx',revision=1)
    assert returned==sid and counts(db)['employees']==4 and service.brigades.current(e,'2026-10-21')['brigade_id']==second


def test_unknown_directories_are_created_only_after_final_confirmation(db,personnel):
    service,sid=uploaded(db,personnel);preview(service,sid);before=counts(db)
    with pytest.raises(ValidationError,match='Подтвердите создание'):service.confirm(sid,revision=service.get(sid)['revision'])
    assert counts(db)==before


def test_invalid_row_can_only_be_skipped(db,personnel):
    service,sid=uploaded(db,personnel,[['','1','Каменщик']])
    assert preview(service,sid,{2:dict(decision='create')})['errors']
    preview(service,sid,{2:dict(decision='skip')});commit(service,sid)
    assert counts(db)['employees']==0


def test_transaction_rolls_back_all_directories_people_and_mappings(db,personnel,monkeypatch):
    service,sid=uploaded(db,personnel);preview(service,sid);before=counts(db)
    original=service.people._save_employee;called=[]
    def failure(*args,**kwargs):
        result=original(*args,**kwargs);called.append(result)
        if len(called)==2:raise sqlite3.OperationalError('injected failure')
        return result
    monkeypatch.setattr(service.people,'_save_employee',failure)
    with pytest.raises(sqlite3.OperationalError):commit(service,sid)
    assert counts(db)==before and service.get(sid)['status']=='preview'
    assert db.execute('SELECT count(*) FROM personnel_import_mappings').fetchone()[0]==0


def test_stale_preview_rechecks_employee_inside_transaction(db,personnel):
    eid=worker(personnel);service,sid=uploaded(db,personnel,[EXAMPLE[0]])
    preview(service,sid,{2:dict(decision='update',employee_id=eid,acknowledge=True)})
    personnel['people'].save_employee(dict(position='Изменено отдельно'),eid)
    with pytest.raises(ValidationError,match='Данные изменились'):commit(service,sid)
    assert counts(db)['brigades']==0


def test_concurrent_confirmation_same_session_is_idempotent(db,personnel):
    service,sid=uploaded(db,personnel);preview(service,sid);rev=service.get(sid)['revision']
    path=db.execute('PRAGMA database_list').fetchone()[2];barrier=Barrier(2)
    def run(_):
        con=connect(path)
        try:barrier.wait();return PersonnelImportService(con).confirm(sid,revision=rev,confirm_new=True)
        finally:con.close()
    with ThreadPoolExecutor(2) as p:results=list(p.map(run,range(2)))
    assert results[0]==results[1] and counts(db)['employees']==4


def test_concurrent_duplicate_sessions_cannot_automatically_copy_names(db,personnel):
    service,sid=uploaded(db,personnel);other=service.start(personnel['contractor'],DATE);service.upload(other,book(),'other.xlsx',revision=1)
    preview(service,sid);preview(service,other);commit(service,sid)
    with pytest.raises(ValidationError):commit(service,other)
    assert counts(db)['employees']==4


def test_import_cannot_use_other_project_or_employer(db,personnel):
    s=personnel;service,sid=uploaded(db,s);other=ProjectRepository(db).create_project('Чужой','OTHER')
    bad=PersonnelImportService(db,other)
    with pytest.raises(ValidationError):bad.get(sid)
    with pytest.raises(ValidationError):bad.start(s['contractor'],DATE)
    other_c=s['people'].save_contractor(dict(name='Иной подрядчик'))
    other_e=s['people'].save_worker(dict(full_name=EXAMPLE[0][0],contractor_id=other_c,specialty_id=s['specialty']))
    with pytest.raises(ValidationError):service.decide(sid,{2:dict(decision='link',employee_id=other_e)},revision=service.get(sid)['revision'])


def test_employee_absent_from_new_file_not_deleted_or_unassigned(db,personnel):
    service,sid=uploaded(db,personnel);preview(service,sid);result=commit(service,sid)
    service,second=uploaded(db,personnel,[['Другой Работник','','Каменщик']]);preview(service,second);commit(service,second)
    assert counts(db)['employees']==5 and service.brigades.current(result['employee_ids'][0],DATE) is not None


def test_import_operational_log_has_no_employee_names(db,personnel):
    service,sid=uploaded(db,personnel);preview(service,sid);commit(service,sid)
    logs=' '.join(str(tuple(r)) for r in db.execute('SELECT * FROM personnel_import_log'))
    assert all(row[0] not in logs for row in EXAMPLE)


def test_saved_mapping_links_same_canonical_employee_on_another_shared_project(db,personnel):
    s=personnel;service,sid=uploaded(db,s);preview(service,sid);result=commit(service,sid)
    other=ProjectRepository(db).create_project('Другой','OTHER');service.people.share_contractor(s['contractor'],other)
    target=PersonnelImportService(db,other);new=target.start(s['contractor'],DATE);target.upload(new,book(),'workers.xlsx',revision=1)
    assert all(r['decision']=='link' for r in target.rows(new))
    preview(target,new);commit(target,new)
    assert counts(db)['employees']==4 and counts(db)['employee_projects']==8
    assert {e['id'] for e in target.people.list_employees(as_of=DATE)}==set(result['employee_ids'])


def test_conflicting_saved_mapping_requires_new_explicit_decision(db,personnel):
    s=personnel;service,sid=uploaded(db,s);preview(service,sid);result=commit(service,sid)
    other=ProjectRepository(db).create_project('Другой','OTHER');service.people.share_contractor(s['contractor'],other)
    service.people.save_employee(dict(position='Изменена карточка'),result['employee_ids'][0])
    target=PersonnelImportService(db,other);new=target.start(s['contractor'],DATE);target.upload(new,book(),'workers.xlsx',revision=1)
    assert target.rows(new)[0]['decision']==''


def test_conflicting_assignment_date_reported_before_commit(db,personnel):
    s=personnel;eid=worker(s);b=s['brigades'].save(s['contractor'],'01');s['brigades'].assign(eid,b,'2026-10-25')
    service,sid=uploaded(db,s,[[EXAMPLE[0][0],'1','Каменщик']])
    report=preview(service,sid,{2:dict(decision='update',employee_id=eid,acknowledge=True)})
    assert any('Строка 2' in e and 'назначению' in e for e in report['errors'])
    with pytest.raises(ValidationError):commit(service,sid)


def test_new_file_does_not_silently_choose_between_two_homonyms(db,personnel):
    first=worker(personnel);second=worker(personnel)
    service,sid=uploaded(db,personnel,[EXAMPLE[0]])
    row=service.rows(sid)[0]
    assert row['decision']=='' and {c['id'] for c in row['candidates']}=={first,second}
    preview(service,sid,{2:dict(decision='link',employee_id=second)});result=commit(service,sid)
    assert result['employee_ids']==[second] and counts(db)['employees']==2


def test_archive_brigade_blocks_import_assignment_and_allows_no_silent_restore(db,personnel):
    b=personnel['brigades'].save(personnel['contractor'],'1');personnel['brigades'].set_active(b,False)
    service,sid=uploaded(db,personnel,[EXAMPLE[0]]);report=preview(service,sid)
    assert any('архивирована' in e for e in report['errors'])
    with pytest.raises(ValidationError):commit(service,sid)


def test_committed_rows_cannot_be_inserted_updated_or_deleted(db,personnel):
    service,sid=uploaded(db,personnel);preview(service,sid);commit(service,sid)
    for sql,args in [('DELETE FROM personnel_import_rows WHERE session_id=?',(sid,)),
                     ('UPDATE personnel_import_rows SET decision=\'skip\' WHERE session_id=?',(sid,)),
                     ('INSERT INTO personnel_import_rows(session_id,row_number,full_name,brigade_number,specialty_name,fingerprint) VALUES (?,999,\'Тестовый Работник\',\'\',\'Каменщик\',\'test\')',(sid,))]:
        with pytest.raises(sqlite3.IntegrityError):db.execute(sql,args)


def test_update_specialty_keeps_original_name_spelling(db,personnel):
    s=personnel;eid=worker(s)
    service,sid=uploaded(db,s,[['ИВАНОВ ИВАН ИВАНОВИЧ','','Маляр']])
    preview(service,sid,{2:dict(decision='update',employee_id=eid,acknowledge=True)});commit(service,sid)
    assert s['people'].employee(eid)['full_name']=='Иванов Иван Иванович'


@pytest.mark.parametrize('value', ['date','percentage'])
def test_numeric_date_or_percentage_is_not_a_brigade_number(value):
    from datetime import date
    payload=book([['Иванов Иван',date(2026,10,20) if value=='date' else 1,'Каменщик']])
    if value=='percentage':payload=edit_zip(payload,'xl/worksheets/sheet1.xml',lambda x:x.replace(b'<c r="B2" t="n" s="3"',b'<c r="B2" t="n" s="4"'))
    assert any('текстовое' in e for e in parse_workers_xlsx(payload)[0]['errors'])
