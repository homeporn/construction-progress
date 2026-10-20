"""Permanent memberships use [start, end) dates and registration-time history."""
from app.time_utils import day, instant
from .base import ValidationError, transaction
from .personnel_repository import PersonnelRepository
from .project_repository import clean


class BrigadeRepository:
    def __init__(self, db, project_id=1):
        self.db,self.project_id=db,project_id
        self.people=PersonnelRepository(db,project_id)

    def get(self, brigade_id, *, active=False):
        row=self.db.execute('SELECT b.*,c.name AS contractor_name FROM brigades b JOIN contractors c ON c.id=b.contractor_id '
            'JOIN contractor_projects cp ON cp.contractor_id=c.id WHERE b.id=? AND cp.project_id=?',(brigade_id,self.project_id)).fetchone()
        if row is None:raise ValidationError('Бригада не принадлежит подрядчику текущего объекта')
        if active and not row['is_active']:raise ValidationError('Бригада архивирована')
        return row

    def list(self, *, contractor_id=None, active_only=False, as_of=None):
        if contractor_id is not None:self.people.contractor(contractor_id)
        rows=self.db.execute('SELECT b.*,c.name AS contractor_name FROM brigades b JOIN contractors c ON c.id=b.contractor_id '
            'JOIN contractor_projects cp ON cp.contractor_id=b.contractor_id WHERE cp.project_id=? '
            'AND (? IS NULL OR b.contractor_id=?) AND (?=0 OR b.is_active=1) ORDER BY c.name,b.number,b.id',
            (self.project_id,contractor_id,contractor_id,active_only)).fetchall()
        return [dict(r,member_count=len(self.members(r['id'],as_of))) for r in rows]

    def _save(self, contractor_id, number, brigade_id=None):
        self.people.contractor(contractor_id,active=True)
        number=clean(number,'Номер бригады',80)
        now=instant()
        if brigade_id is None:
            return self.db.execute('INSERT INTO brigades(contractor_id,number,created_at,updated_at) VALUES (?,?,?,?)',(contractor_id,number,now,now)).lastrowid
        old=self.get(brigade_id)
        if contractor_id!=old['contractor_id']:raise ValidationError('Подрядчик бригады неизменяем')
        self.db.execute('UPDATE brigades SET number=?,updated_at=? WHERE id=?',(number,now,brigade_id))
        return brigade_id

    def save(self, contractor_id, number, brigade_id=None):
        with transaction(self.db):
            self.people.work._writable()
            return self._save(contractor_id,number,brigade_id)

    def current(self, employee_id, as_of=None, cutoff=None):
        self.people.employee(employee_id)
        when,known=day(as_of or instant()[:10]),instant(cutoff)
        return self.db.execute('SELECT a.*,b.number,b.contractor_id FROM brigade_assignments a JOIN brigades b ON b.id=a.brigade_id '
            'WHERE a.employee_id=? AND a.start_date<=? AND a.recorded_at<=? AND (a.end_date IS NULL OR a.end_date>? OR a.closed_at>?) ORDER BY a.id DESC LIMIT 1',
            (employee_id,when,known,when,known)).fetchone()

    def members(self, brigade_id, as_of=None, cutoff=None):
        self.get(brigade_id)
        when,known=day(as_of or instant()[:10]),instant(cutoff)
        rows=self.db.execute('SELECT a.*,e.full_name,e.specialty_id,e.is_active,s.name AS specialty_name FROM brigade_assignments a '
            'JOIN employees e ON e.id=a.employee_id LEFT JOIN specialties s ON s.id=e.specialty_id WHERE a.brigade_id=? '
            'AND a.start_date<=? AND a.recorded_at<=? AND (a.end_date IS NULL OR a.end_date>? OR a.closed_at>?) ORDER BY e.full_name,e.id',
            (brigade_id,when,known,when,known)).fetchall()
        return [dict(r) for r in rows if self.people.available(r['employee_id'],when,known)]

    def history(self, *, brigade_id=None, employee_id=None):
        if brigade_id is not None:self.get(brigade_id)
        if employee_id is not None:self.people.employee(employee_id)
        if brigade_id is None and employee_id is None:raise ValidationError('Укажите работника или бригаду')
        return self.db.execute('SELECT a.*,e.full_name,b.number,c.name AS contractor_name FROM brigade_assignments a '
            'JOIN employees e ON e.id=a.employee_id JOIN brigades b ON b.id=a.brigade_id JOIN contractors c ON c.id=b.contractor_id '
            'WHERE (? IS NULL OR a.brigade_id=?) AND (? IS NULL OR a.employee_id=?) ORDER BY a.start_date DESC,a.id DESC',
            (brigade_id,brigade_id,employee_id,employee_id)).fetchall()

    def _assign(self, employee_id, brigade_id, effective_date):
        when=day(effective_date)
        employee=self.people.employee(employee_id,active=True,as_of=when)
        if employee['contractor_id'] is None:raise ValidationError('Сначала укажите подрядчика работника')
        self.people.contractor(employee['contractor_id'],active=True)
        if brigade_id is not None:
            brigade=self.get(brigade_id,active=True)
            if brigade['contractor_id']!=employee['contractor_id']:raise ValidationError('Работник и бригада должны относиться к одному подрядчику')
        opened=self.db.execute('SELECT * FROM brigade_assignments WHERE employee_id=? AND end_date IS NULL',(employee_id,)).fetchone()
        if opened and when<opened['start_date']:raise ValidationError('Перевод не может предшествовать открытому назначению')
        if opened and opened['brigade_id']==brigade_id:return opened['id']
        now=instant()
        if opened:self.db.execute('UPDATE brigade_assignments SET end_date=?,closed_at=? WHERE id=?',(when,now,opened['id']))
        if brigade_id is not None:
            return self.db.execute('INSERT INTO brigade_assignments(employee_id,brigade_id,start_date,recorded_at) VALUES (?,?,?,?)',(employee_id,brigade_id,when,now)).lastrowid

    def assign(self, employee_id, brigade_id, effective_date):
        with transaction(self.db):
            self.people.work._writable()
            return self._assign(employee_id,brigade_id,effective_date)

    def remove(self, employee_id, brigade_id, effective_date):
        with transaction(self.db):
            self.people.work._writable();self.get(brigade_id);self.people.employee(employee_id)
            opened=self.db.execute('SELECT brigade_id FROM brigade_assignments WHERE employee_id=? AND end_date IS NULL',(employee_id,)).fetchone()
            if opened is None or opened[0]!=brigade_id:
                raise ValidationError('Назначение изменилось; обновите состав бригады')
            self._assign(employee_id,None,effective_date)

    def set_active(self, brigade_id, active, *, effective_date=None, confirm_members=False):
        with transaction(self.db):
            self.people.work._writable();self.get(brigade_id)
            when=day(effective_date or instant()[:10]);now=instant()
            if not active:
                opened=self.db.execute('SELECT * FROM brigade_assignments WHERE brigade_id=? AND end_date IS NULL',(brigade_id,)).fetchall()
                if opened and not confirm_members:raise ValidationError('Подтвердите завершение назначений всех работников при архивировании')
                for row in opened:
                    if when<row['start_date']:raise ValidationError('Дата архивирования предшествует открытому назначению')
                    self.db.execute('UPDATE brigade_assignments SET end_date=?,closed_at=? WHERE id=?',(when,now,row['id']))
            self.db.execute('UPDATE brigades SET is_active=?,archived_at=?,updated_at=? WHERE id=?',(int(active),None if active else now,now,brigade_id))
