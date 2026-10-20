"""Persistent five-step import. Preview is read-only for personnel masters.

BEGIN IMMEDIATE spans final stale-data validation and every master/membership
write. Session ID is the idempotency key. A name match never selects a person.
"""
import hashlib
import json
from pathlib import PurePath
from uuid import uuid4

from app.repositories.base import ValidationError, transaction
from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.personnel_repository import PersonnelRepository, normalized
from app.time_utils import day, instant
from .personnel_xlsx import parse_workers_xlsx


def encode(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(value):return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


class PersonnelImportService:
    def __init__(self, db, project_id=1):
        self.db,self.project_id=db,project_id
        self.people=PersonnelRepository(db,project_id)
        self.brigades=BrigadeRepository(db,project_id)

    def get(self, session_id):
        row=self.db.execute('SELECT s.*,c.name AS contractor_name,p.name AS project_name FROM personnel_import_sessions s '
            'JOIN contractors c ON c.id=s.contractor_id JOIN projects p ON p.id=s.project_id WHERE s.id=? AND s.project_id=?',(session_id,self.project_id)).fetchone()
        if row is None:raise ValidationError('Импорт не принадлежит текущему объекту')
        self.people.contractor(row['contractor_id'])
        result=dict(row);result['report']=json.loads(row['report_json']);return result

    def _log(self, session_id, action, counts=None):
        # Operational log contains IDs and counts only, never employee names.
        self.db.execute('INSERT INTO personnel_import_log(session_id,action,recorded_at,details) VALUES (?,?,?,?)',
                        (session_id,action,instant(),encode(counts or {})))

    def start(self, contractor_id, effective_date, *, session_id=None):
        session_id=session_id or uuid4().hex
        if len(session_id)>100 or not session_id:raise ValidationError('Недопустимый ключ импорта')
        when=day(effective_date)
        with transaction(self.db):
            self.people.work._writable();self.people.contractor(contractor_id,active=True)
            existing=self.db.execute('SELECT id FROM personnel_import_sessions WHERE id=?',(session_id,)).fetchone()
            if existing:
                row=self.get(session_id)
                if row['contractor_id']!=contractor_id or row['effective_date']!=when:raise ValidationError('Контекст формы импорта изменился')
                return session_id
            now=instant()
            self.db.execute('INSERT INTO personnel_import_sessions(id,project_id,contractor_id,effective_date,status,created_at,updated_at) VALUES (?,?,?,?,\'context\',?,?)',
                            (session_id,self.project_id,contractor_id,when,now,now))
            self._log(session_id,'context')
        return session_id

    def upload(self, session_id, payload, filename, *, revision):
        self.get(session_id)
        rows=parse_workers_xlsx(payload,filename)
        sha=hashlib.sha256(payload).hexdigest()
        filename=PurePath(filename.replace('\\','/')).name[:200]
        with transaction(self.db):
            self.people.work._writable();s=self.get(session_id)
            self.people.contractor(s['contractor_id'],active=True)
            if s['status'] not in ('context','uploaded') or s['revision']!=revision:raise ValidationError('Импорт изменён; обновите страницу')
            existing=self.db.execute("SELECT id FROM personnel_import_sessions WHERE project_id=? AND contractor_id=? AND file_digest=? AND status='committed' ORDER BY confirmed_at DESC LIMIT 1",
                                     (self.project_id,s['contractor_id'],sha)).fetchone()
            if existing:
                self.db.execute("UPDATE personnel_import_sessions SET status='cancelled',filename=?,file_digest=?,updated_at=? WHERE id=?",(filename,sha,instant(),session_id))
                self._log(session_id,'already_imported',{'previous_session':existing['id']})
                return existing['id']
            self.db.execute('DELETE FROM personnel_import_rows WHERE session_id=?',(session_id,))
            candidates=self.people.candidates(s['contractor_id']);names={normalized(c['full_name']) for c in candidates}
            occurrences={}
            for row in rows:occurrences[normalized(row['full_name'])]=occurrences.get(normalized(row['full_name']),0)+1
            for r in rows:
                fp=digest([r['full_name'],r['brigade_number'],r['specialty_name']]);decision='';employee_id=None
                mapping=self.db.execute('SELECT * FROM personnel_import_mappings WHERE contractor_id=? AND file_digest=? AND row_number=? AND fingerprint=?',
                                        (s['contractor_id'],sha,r['row_number'],fp)).fetchone()
                if not r['errors']:
                    if mapping and mapping['confirmed_signature']==self._employee_signature(mapping['employee_id']):decision,employee_id='link',mapping['employee_id']
                    elif normalized(r['full_name']) not in names and occurrences[normalized(r['full_name'])]==1:decision='create'
                self.db.execute('INSERT INTO personnel_import_rows(session_id,row_number,full_name,brigade_number,specialty_name,fingerprint,errors_json,decision,employee_id) VALUES (?,?,?,?,?,?,?,?,?)',
                    (session_id,r['row_number'],r['full_name'],r['brigade_number'],r['specialty_name'],fp,encode(r['errors']),decision,employee_id))
            self.db.execute("UPDATE personnel_import_sessions SET filename=?,file_digest=?,status='uploaded',revision=revision+1,updated_at=? WHERE id=?",(filename,sha,instant(),session_id))
            self._log(session_id,'uploaded',{'rows':len(rows),'errors':sum(bool(r['errors']) for r in rows)})
        return session_id

    def rows(self, session_id):
        s=self.get(session_id);result=[]
        candidates=self.people.candidates(s['contractor_id'])
        all_names={}
        for row in self.db.execute('SELECT full_name FROM personnel_import_rows WHERE session_id=?',(session_id,)):
            n=normalized(row[0]);all_names[n]=all_names.get(n,0)+1
        for row in self.db.execute('SELECT * FROM personnel_import_rows WHERE session_id=? ORDER BY row_number',(session_id,)):
            r=dict(row);r['errors']=json.loads(r['errors_json']);r['candidates']=[c for c in candidates if normalized(c['full_name'])==normalized(r['full_name'])]
            r['specialty']=self.people.specialty_by_name(r['specialty_name'])
            r['brigade']=self.db.execute('SELECT * FROM brigades WHERE contractor_id=? AND number=?',(s['contractor_id'],r['brigade_number'])).fetchone() if r['brigade_number'] else None
            r['duplicate']=all_names[normalized(r['full_name'])]>1
            result.append(r)
        return result

    def _employee_signature(self, employee_id):
        e=self.db.execute('SELECT * FROM employees WHERE id=?',(employee_id,)).fetchone()
        if e is None:return ''
        memberships=[tuple(r) for r in self.db.execute('SELECT * FROM brigade_assignments WHERE employee_id=? ORDER BY id',(employee_id,))]
        periods=[tuple(r) for r in self.db.execute('SELECT p.* FROM employee_project_periods p JOIN employee_projects ep ON ep.id=p.employee_project_id WHERE ep.employee_id=? ORDER BY p.id',(employee_id,))]
        return digest([dict(e),memberships,periods])

    def decide(self, session_id, decisions, *, revision):
        with transaction(self.db):
            self.people.work._writable();s=self.get(session_id)
            if s['status'] not in ('uploaded','reviewed','preview') or s['revision']!=revision:raise ValidationError('Импорт изменён; обновите страницу')
            rows=self.rows(session_id)
            allowed={r['row_number'] for r in rows}
            if set(decisions)-allowed:raise ValidationError('Строка решения не принадлежит импорту')
            for row in rows:
                data=decisions.get(row['row_number'],{})
                action=data.get('decision','');employee_id=data.get('employee_id')
                if action not in ('','create','link','update','skip'):raise ValidationError('Недопустимое действие строки')
                if action in ('create','skip',''):employee_id=None
                if employee_id is not None and not any(c['id']==employee_id for c in row['candidates']):raise ValidationError('Выберите кандидата с совпадающим ФИО у текущего подрядчика')
                self.db.execute('UPDATE personnel_import_rows SET decision=?,employee_id=?,clear_brigade=?,acknowledge=? WHERE id=?',
                                (action,employee_id,int(bool(data.get('clear_brigade'))),int(bool(data.get('acknowledge'))),row['id']))
            self.db.execute("UPDATE personnel_import_sessions SET status='reviewed',revision=revision+1,preview_signature=NULL,updated_at=? WHERE id=?",(instant(),session_id))
            self._log(session_id,'reviewed',{'rows':len(rows)})

    def _plan(self, session_id):
        s=self.get(session_id);self.people.contractor(s['contractor_id'],active=True)
        rows=self.rows(session_id);errors=[];seen=set();specialties={};brigades=set();states=[]
        counts=dict(new_workers=0,updated_workers=0,linked_workers=0,new_brigades=0,new_specialties=0,skipped=0,error_rows=sum(bool(r['errors']) for r in rows),total=len(rows))
        for r in rows:
            prefix='Строка '+str(r['row_number'])+': '
            action=r['decision'];eid=r['employee_id'];states.append([r['id'],action,eid,r['acknowledge'],r['clear_brigade']])
            if action=='skip':counts['skipped']+=1;continue
            if r['errors']:errors.append(prefix+'исправьте данные в файле или пропустите строку');continue
            if not action:errors.append(prefix+'выберите действие');continue
            if action in ('link','update'):
                if eid is None or not any(c['id']==eid for c in r['candidates']):errors.append(prefix+'выберите ID работника');continue
                if eid in seen:errors.append(prefix+'один работник выбран в нескольких строках');continue
                seen.add(eid);e=self.db.execute('SELECT * FROM employees WHERE id=?',(eid,)).fetchone()
                if not e['is_active']:errors.append(prefix+'работник в архиве; восстановите его отдельно');continue
                states.append(self._employee_signature(eid))
                periods=self.db.execute('SELECT p.start_date,p.end_date FROM employee_project_periods p JOIN employee_projects ep ON ep.id=p.employee_project_id '
                    'WHERE ep.employee_id=? AND ep.project_id=?',(eid,self.project_id)).fetchall()
                if any(p['end_date'] is None and p['start_date'] and p['start_date']>s['effective_date'] for p in periods) or (
                    periods and not any(p['end_date'] is None for p in periods) and max(p['end_date'] for p in periods)>s['effective_date']):
                    errors.append(prefix+'дата начала доступности конфликтует с существующими периодами объекта');continue
                if action=='update' and not r['acknowledge']:errors.append(prefix+'подтвердите изменение специальности / назначения');continue
                if action=='link':
                    counts['linked_workers']+=1
                    # Linking only establishes project availability. It never
                    # changes names, specialties or permanent brigade membership.
                    continue
                counts['updated_workers']+=1
            elif action=='create':
                if (r['candidates'] or r['duplicate']) and not r['acknowledge']:
                    errors.append(prefix+'есть совпадающие ФИО; явно подтвердите создание отдельного человека');continue
                counts['new_workers']+=1
            specialty=r['specialty']
            states.append(['specialty',normalized(r['specialty_name']),dict(specialty) if specialty else None])
            if specialty is None:specialties[normalized(r['specialty_name'])]=r['specialty_name']
            if r['brigade_number']:
                b=r['brigade'];states.append(['brigade',r['brigade_number'],dict(b) if b else None])
                if b is not None and not b['is_active']:errors.append(prefix+'бригада архивирована; восстановите её отдельно')
                if b is None:brigades.add(r['brigade_number'])
            if eid and action=='update' and (r['brigade_number'] or r['clear_brigade']):
                opened=self.db.execute('SELECT start_date FROM brigade_assignments WHERE employee_id=? AND end_date IS NULL',(eid,)).fetchone()
                if opened and opened[0]>s['effective_date']:errors.append(prefix+'дата импорта предшествует открытому назначению')
        counts.update(new_specialties=len(specialties),new_brigades=len(brigades))
        counts['unresolved']=len(errors)
        signature=digest([s['contractor_id'],s['effective_date'],s['file_digest'],states,counts])
        return dict(counts=counts,errors=errors,specialties=specialties,brigades=sorted(brigades),signature=signature),rows

    def preview(self, session_id, *, revision):
        with transaction(self.db):
            self.people.work._writable();s=self.get(session_id)
            if s['status'] not in ('reviewed','preview') or s['revision']!=revision:raise ValidationError('Сначала сохраните решения строк; обновите страницу при изменении импорта')
            plan,_=self._plan(session_id)
            self.db.execute("UPDATE personnel_import_sessions SET status='preview',revision=revision+1,report_json=?,preview_signature=?,updated_at=? WHERE id=?",
                            (encode(plan),plan['signature'],instant(),session_id))
            self._log(session_id,'preview',plan['counts'])
        return plan

    def confirm(self, session_id, *, revision, confirm_new=False):
        with transaction(self.db):
            self.people.work._writable();s=self.get(session_id)
            if s['status']=='committed':return s['report']
            if s['status']!='preview' or s['revision']!=revision:raise ValidationError('Сначала сформируйте предварительный просмотр')
            plan,rows=self._plan(session_id)
            if plan['errors']:raise ValidationError('Есть нерешённые строки. Вернитесь к сопоставлению')
            if plan['signature']!=s['preview_signature']:raise ValidationError('Данные изменились после проверки; обновите предварительный просмотр')
            if (plan['counts']['new_brigades'] or plan['counts']['new_specialties']) and not confirm_new:
                raise ValidationError('Подтвердите создание новых бригад и специальностей')
            for name in plan['specialties'].values():self.people._save_specialty(name,'ИМП-'+uuid4().hex)
            for number in plan['brigades']:self.brigades._save(s['contractor_id'],number)
            applied=[]
            for r in rows:
                if r['decision']=='skip':continue
                eid=r['employee_id'];action=r['decision']
                if action=='link':self.people._project_assignment(eid,s['effective_date'])
                else:
                    spec=self.people.specialty_by_name(r['specialty_name'])
                    if action=='update':self.people._project_assignment(eid,s['effective_date'])
                    # Updating an assignment is not permission to respell a
                    # person's name using the workbook's capitalization.
                    name=self.people.employee(eid)['full_name'] if action=='update' else r['full_name']
                    data=dict(full_name=name,contractor_id=s['contractor_id'],specialty_id=spec['id'],effective_date=s['effective_date'])
                    eid=self.people._save_employee(data,eid if action=='update' else None,strict=True)
                    if r['brigade_number']:
                        bid=self.db.execute('SELECT id FROM brigades WHERE contractor_id=? AND number=?',(s['contractor_id'],r['brigade_number'])).fetchone()[0]
                        self.brigades._assign(eid,bid,s['effective_date'])
                    elif r['clear_brigade'] and action=='update':self.brigades._assign(eid,None,s['effective_date'])
                applied.append((r,eid))
            now=instant()
            # Record final signatures only after every assignment is complete.
            for row,eid in applied:
                self.db.execute('INSERT INTO personnel_import_mappings(contractor_id,file_digest,row_number,fingerprint,employee_id,confirmed_signature,session_id,confirmed_at) '
                    'VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(contractor_id,file_digest,row_number,fingerprint) DO UPDATE SET employee_id=excluded.employee_id,confirmed_signature=excluded.confirmed_signature,session_id=excluded.session_id,confirmed_at=excluded.confirmed_at',
                    (s['contractor_id'],s['file_digest'],row['row_number'],row['fingerprint'],eid,self._employee_signature(eid),session_id,now))
                self.db.execute('UPDATE personnel_import_rows SET employee_id=? WHERE id=?',(eid,row['id']))
            report=dict(plan,employee_ids=[eid for _,eid in applied])
            self._log(session_id,'committed',plan['counts'])
            self.db.execute("UPDATE personnel_import_sessions SET status='committed',confirmed_at=?,updated_at=?,report_json=?,revision=revision+1 WHERE id=?",(now,now,encode(report),session_id))
            return report

    def cancel(self, session_id):
        with transaction(self.db):
            self.people.work._writable();s=self.get(session_id)
            if s['status']=='cancelled':return
            if s['status']=='committed':raise ValidationError('Подтверждённый импорт не отменяется; исправьте назначения отдельной операцией')
            self._log(session_id,'cancelled')
            self.db.execute("UPDATE personnel_import_sessions SET status='cancelled',updated_at=?,revision=revision+1 WHERE id=?",(instant(),session_id))

    def history(self):
        return self.db.execute('SELECT s.*,c.name AS contractor_name FROM personnel_import_sessions s JOIN contractors c ON c.id=s.contractor_id WHERE s.project_id=? ORDER BY s.created_at DESC,s.id DESC',(self.project_id,)).fetchall()
