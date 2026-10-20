"""Canonical people, with explicit employer and project availability.

``project_id`` in the legacy master rows is the immutable origin. Since 009,
project associations determine access; an employee is never copied to a project.
The legacy save_employee API accepts incomplete office personnel for old data.
New worker forms and imports use the strict save_worker API instead.
"""
import re

from app.time_utils import day, instant
from .base import ValidationError, transaction
from .project_repository import clean
from .work_repository import WorkRepository


def normalized(value):
    return ' '.join(value.split()).casefold()


class PersonnelRepository:
    def __init__(self, db, project_id=1):
        self.db, self.project_id = db, project_id
        self.work = WorkRepository(db, project_id)
        self.extended = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='employee_projects'").fetchone())

    def _row(self, table, record_id, *, active=False, as_of=None):
        if self.extended:
            link, field = ('contractor_projects','contractor_id') if table=='contractors' else ('employee_projects','employee_id')
            row = self.db.execute('SELECT r.* FROM '+table+' r JOIN '+link+' p ON p.'+field+'=r.id WHERE r.id=? AND p.project_id=?',
                                  (record_id,self.project_id)).fetchone()
        else:
            row = self.db.execute('SELECT * FROM '+table+' WHERE id=? AND project_id=?',(record_id,self.project_id)).fetchone()
        if row is None:
            raise ValidationError('Запись справочника не принадлежит текущему объекту')
        if active and not row['is_active']:
            raise ValidationError('Запись справочника архивирована')
        if self.extended and active and table=='employees' and not self.available(record_id,as_of):
            raise ValidationError('Работник недоступен на объекте на выбранную дату')
        return row

    def contractor(self, record_id, *, active=False):
        return self._row('contractors',record_id,active=active)

    def employee(self, record_id, *, active=False, as_of=None):
        return self._row('employees',record_id,active=active,as_of=as_of)

    def specialty(self, record_id):
        row=self.db.execute('SELECT * FROM specialties WHERE id=?',(record_id,)).fetchone()
        if row is None: raise ValidationError('Специальность не найдена')
        return row

    def specialty_by_name(self, name):
        if self.extended:
            return self.db.execute('SELECT s.* FROM specialties s JOIN specialty_name_keys k ON k.specialty_id=s.id WHERE k.normalized_name=?',(normalized(name),)).fetchone()
        return next((s for s in self.list_specialties() if normalized(s['name'])==normalized(name)),None)

    def list_contractors(self, *, active_only=False):
        scope='EXISTS(SELECT 1 FROM contractor_projects cp WHERE cp.contractor_id=c.id AND cp.project_id=?)' if self.extended else 'c.project_id=?'
        return self.db.execute('SELECT c.* FROM contractors c WHERE '+scope+' AND (?=0 OR c.is_active=1) ORDER BY c.name,c.id',
                               (self.project_id,active_only)).fetchall()

    def available(self, employee_id, as_of=None, cutoff=None):
        when,known=day(as_of or instant()[:10]),instant(cutoff)
        return self.db.execute('SELECT 1 FROM employee_projects ep JOIN employee_project_periods p ON p.employee_project_id=ep.id '
            'WHERE ep.employee_id=? AND ep.project_id=? AND COALESCE(p.recorded_at,\'\')<=? '
            'AND COALESCE(p.start_date,\'0001-01-01\')<=? AND (p.end_date IS NULL OR p.end_date>? OR p.closed_at>?)',
            (employee_id,self.project_id,known,when,when,known)).fetchone() is not None

    def list_employees(self, *, active_only=False, as_of=None, search='', contractor_id=None, specialty_id=None, brigade_id=None):
        if contractor_id is not None: self.contractor(contractor_id)
        if specialty_id is not None: self.specialty(specialty_id)
        if brigade_id is not None:
            from .brigade_repository import BrigadeRepository
            BrigadeRepository(self.db,self.project_id).get(brigade_id)
        scope='EXISTS(SELECT 1 FROM employee_projects ep WHERE ep.employee_id=e.id AND ep.project_id=?)' if self.extended else 'e.project_id=?'
        rows=self.db.execute('SELECT e.*,s.name AS specialty_name,c.name AS contractor_name FROM employees e '
            'LEFT JOIN specialties s ON s.id=e.specialty_id LEFT JOIN contractors c ON c.id=e.contractor_id WHERE '+scope+
            ' AND (?=0 OR e.is_active=1) AND (? IS NULL OR e.contractor_id=?) AND (? IS NULL OR e.specialty_id=?) ORDER BY e.full_name,e.id',
            (self.project_id,active_only,contractor_id,contractor_id,specialty_id,specialty_id)).fetchall()
        result=[]
        for row in rows:
            if normalized(search) not in normalized(row['full_name']): continue
            item=dict(row)
            if self.extended:
                from .brigade_repository import BrigadeRepository
                item['available']=self.available(row['id'],as_of)
                if active_only and not item['available']: continue
                membership=BrigadeRepository(self.db,self.project_id).current(row['id'],as_of)
                item['brigade_id']=membership['brigade_id'] if membership else None
                item['brigade_number']=membership['number'] if membership else ''
                if brigade_id is not None and item['brigade_id']!=brigade_id: continue
            result.append(item)
        return result

    def list_specialties(self):
        return self.db.execute('SELECT * FROM specialties ORDER BY name,id').fetchall()

    def save_contractor(self, data, contractor_id=None):
        values=[clean(data.get(field,''),label,limit,field=='name') for field,label,limit in
                [('name','Название организации',200),('short_name','Краткое название',200),('inn','ИНН',12),('contact_person','Контактное лицо',200),('phone','Телефон',80)]]
        if values[2] and not re.fullmatch(r'[0-9]{10}|[0-9]{12}',values[2]):
            raise ValidationError('ИНН должен содержать 10 или 12 цифр либо оставаться пустым')
        with transaction(self.db):
            self.work._writable(); now=instant()
            if contractor_id is None:
                contractor_id=self.db.execute('INSERT INTO contractors(project_id,name,short_name,inn,contact_person,phone,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                    [self.project_id]+values+[now,now]).lastrowid
                if self.extended:self.db.execute('INSERT INTO contractor_projects(contractor_id,project_id,registered_at) VALUES (?,?,?)',(contractor_id,self.project_id,now))
            else:
                self.contractor(contractor_id)
                self.db.execute('UPDATE contractors SET name=?,short_name=?,inn=?,contact_person=?,phone=?,updated_at=? WHERE id=?',values+[now,contractor_id])
            return contractor_id

    def share_contractor(self, contractor_id, target_project_id):
        with transaction(self.db):
            self.work._writable();self.contractor(contractor_id,active=True)
            WorkRepository(self.db,target_project_id)._writable()
            self.db.execute('INSERT INTO contractor_projects(contractor_id,project_id,registered_at) VALUES (?,?,?) ON CONFLICT(contractor_id,project_id) DO NOTHING',
                            (contractor_id,target_project_id,instant()))

    def _project_assignment(self, employee_id, effective_date=None):
        # Called only within a validated write transaction. A NULL date denotes
        # an unknown legacy beginning, never a fabricated historical date.
        now=instant()
        self.db.execute('INSERT INTO employee_projects(employee_id,project_id,registered_at) VALUES (?,?,?) ON CONFLICT(employee_id,project_id) DO NOTHING',
                        (employee_id,self.project_id,now))
        link=self.db.execute('SELECT id FROM employee_projects WHERE employee_id=? AND project_id=?',(employee_id,self.project_id)).fetchone()[0]
        when=day(effective_date) if effective_date else None
        opened=self.db.execute('SELECT start_date FROM employee_project_periods WHERE employee_project_id=? AND end_date IS NULL',(link,)).fetchone()
        if opened:
            if when and opened[0] and opened[0]>when:
                raise ValidationError('Дата импорта предшествует началу доступности работника на объекте')
            return
        self.db.execute('INSERT INTO employee_project_periods(employee_project_id,start_date,recorded_at) VALUES (?,?,?)',(link,when,now))

    def assign_to_project(self, employee_id, effective_date):
        with transaction(self.db):
            self.work._writable()
            employee=self.db.execute('SELECT * FROM employees WHERE id=?',(employee_id,)).fetchone()
            if employee is None or employee['contractor_id'] is None:raise ValidationError('Сначала укажите подрядчика работника')
            self.contractor(employee['contractor_id'],active=True)
            if not employee['is_active']:raise ValidationError('Работник архивирован')
            self._project_assignment(employee_id,day(effective_date))

    def end_project_assignment(self, employee_id, effective_date):
        with transaction(self.db):
            self.work._writable();self.employee(employee_id)
            when=day(effective_date)
            row=self.db.execute('SELECT p.* FROM employee_project_periods p JOIN employee_projects ep ON ep.id=p.employee_project_id '
                                'WHERE ep.employee_id=? AND ep.project_id=? AND p.end_date IS NULL',(employee_id,self.project_id)).fetchone()
            if row is None:raise ValidationError('Открытое назначение на объект не найдено')
            self.db.execute('UPDATE employee_project_periods SET end_date=?,closed_at=? WHERE id=?',(when,instant(),row['id']))

    def project_history(self, employee_id):
        self.employee(employee_id)
        return self.db.execute('SELECT p.*,j.name AS project_name,ep.project_id FROM employee_project_periods p JOIN employee_projects ep ON ep.id=p.employee_project_id '
            'JOIN projects j ON j.id=ep.project_id WHERE ep.employee_id=? ORDER BY p.id DESC',(employee_id,)).fetchall()

    def candidates(self, contractor_id, name=''):
        self.contractor(contractor_id)
        rows=self.db.execute('SELECT e.*,s.name AS specialty_name FROM employees e LEFT JOIN specialties s ON s.id=e.specialty_id WHERE e.contractor_id=? ORDER BY e.id',(contractor_id,)).fetchall()
        return [dict(r) for r in rows if not name or normalized(r['full_name'])==normalized(name)]

    def _save_employee(self, data, employee_id=None, *, strict=False):
        if employee_id is not None:
            # A partial edit must not silently clear employer or specialty.
            data=dict(dict(self.employee(employee_id)),**data)
        name=' '.join(clean(data.get('full_name',''),'ФИО').split())
        position=clean(data.get('position',''),'Должность',required=False)
        contractor,specialty=data.get('contractor_id'),data.get('specialty_id')
        if strict and (contractor is None or specialty is None): raise ValidationError('Для работника обязательны подрядчик и специальность')
        if contractor is not None:self.contractor(contractor,active=True)
        if specialty is not None:self.specialty(specialty)
        now=instant()
        if employee_id is None:
            employee_id=self.db.execute('INSERT INTO employees(project_id,contractor_id,full_name,specialty_id,position) VALUES (?,?,?,?,?)',
                                       (self.project_id,contractor,name,specialty,position)).lastrowid
            if self.extended:
                self._project_assignment(employee_id,data.get('effective_date') if strict else None)
                self.db.execute('UPDATE employees SET created_at=?,updated_at=?,needs_details=? WHERE id=?',(now,now,int(contractor is None or specialty is None),employee_id))
        else:
            self.employee(employee_id)
            self.db.execute('UPDATE employees SET contractor_id=?,full_name=?,specialty_id=?,position=? WHERE id=?',(contractor,name,specialty,position,employee_id))
            if self.extended:self.db.execute('UPDATE employees SET updated_at=?,needs_details=? WHERE id=?',(now,int(contractor is None or specialty is None),employee_id))
        return employee_id

    def save_employee(self, data, employee_id=None):
        with transaction(self.db):
            self.work._writable()
            return self._save_employee(data,employee_id)

    def save_worker(self, data, employee_id=None):
        data=dict(data,effective_date=day(data.get('effective_date') or instant()[:10]))
        with transaction(self.db):
            self.work._writable()
            employee_id=self._save_employee(data,employee_id,strict=True)
            if 'brigade_id' in data:
                from .brigade_repository import BrigadeRepository
                BrigadeRepository(self.db,self.project_id)._assign(employee_id,data['brigade_id'],data['effective_date'])
            return employee_id

    def _save_specialty(self, name, code, specialty_id=None):
        values=(' '.join(clean(name,'Специальность').split()),clean(code,'Код специальности',80))
        existing=self.specialty_by_name(values[0])
        if existing and existing['id']!=specialty_id:raise ValidationError('Специальность с таким названием уже существует')
        if specialty_id is None:
            specialty_id=self.db.execute('INSERT INTO specialties(name,code) VALUES (?,?)',values).lastrowid
        else:
            self.specialty(specialty_id)
            self.db.execute('UPDATE specialties SET name=?,code=? WHERE id=?',values+(specialty_id,))
        if self.extended:self.db.execute('INSERT INTO specialty_name_keys(normalized_name,specialty_id) VALUES (?,?) ON CONFLICT(normalized_name) DO NOTHING',(normalized(values[0]),specialty_id))
        return specialty_id

    def save_specialty(self, name, code, specialty_id=None):
        with transaction(self.db):
            self.work._writable()
            return self._save_specialty(name,code,specialty_id)

    def set_active(self, kind, record_id, active):
        if kind not in ('contractors','employees'):raise ValidationError('Неизвестный справочник')
        with transaction(self.db):
            self.work._writable();self._row(kind,record_id)
            field=',updated_at=?' if kind=='contractors' or self.extended else ''
            args=(int(active),instant(),record_id) if field else (int(active),record_id)
            self.db.execute('UPDATE '+kind+' SET is_active=?'+field+' WHERE id=?',args)
