"""Common scoped transactions/audit helpers for local managerial accruals."""
import hashlib
import json
from uuid import uuid4
from app.time_utils import instant
from .base import ValidationError
from .personnel_repository import PersonnelRepository


def encode(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)


def fingerprint(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


class PayrollBase:
    def __init__(self,db,project_id=1):
        self.db,self.project_id=db,project_id
        self.people=PersonnelRepository(db,project_id)

    def scope(self):
        self.people.work._writable()

    def require_period(self,period_id):
        r=self.db.execute('SELECT p.*,j.name AS project_name,c.name AS contractor_name FROM payroll_periods p '
            'JOIN projects j ON j.id=p.project_id JOIN contractors c ON c.id=p.contractor_id WHERE p.id=? AND p.project_id=?',
            (period_id,self.project_id)).fetchone()
        if r is None:raise ValidationError('Расчётный период не принадлежит текущему объекту')
        return dict(r)

    def require_version(self,version_id,editable=False):
        r=self.db.execute('SELECT v.*,p.project_id,p.contractor_id,p.year,p.month,j.name AS project_name,c.name AS contractor_name '
            'FROM payroll_versions v JOIN payroll_periods p ON p.id=v.period_id JOIN projects j ON j.id=p.project_id '
            'JOIN contractors c ON c.id=p.contractor_id WHERE v.id=? AND p.project_id=?',(version_id,self.project_id)).fetchone()
        if r is None:raise ValidationError('Версия начислений не принадлежит текущему объекту')
        if editable:
            self.scope()
            if r['status'] not in ('draft','calculated'):raise ValidationError('Версия начислений зафиксирована или передана на проверку')
        return dict(r)

    def replay(self,key,kind,payload):
        if not key:return False,None
        if not isinstance(key,str) or not 1<=len(key)<=200:raise ValidationError('Некорректный ключ операции')
        old=self.db.execute('SELECT * FROM payroll_operations WHERE id=?',(key,)).fetchone()
        if old is None:return False,None
        if old['project_id']!=self.project_id or old['kind']!=kind or old['fingerprint']!=fingerprint(payload):
            raise ValidationError('Ключ операции уже использован для другого запроса')
        return True,json.loads(old['result_json'])

    def remember(self,key,kind,payload,result):
        self.db.execute('INSERT INTO payroll_operations VALUES (?,?,?,?,?,?)',
            (key or uuid4().hex,self.project_id,kind,fingerprint(payload),encode(result),instant()))
        return result

    def event(self,version_id,action,reason='',old_status=None,new_status=None,details=None):
        self.require_version(version_id)
        self.db.execute('INSERT INTO payroll_events(version_id,action,old_status,new_status,recorded_at,reason,details_json) VALUES (?,?,?,?,?,?,?)',
            (version_id,action,old_status,new_status,instant(),reason,encode(details or {})))

    def dirty(self,version_id,reason=''):
        v=self.require_version(version_id,editable=True)
        self.db.execute("UPDATE payroll_versions SET status='draft',revision_no=revision_no+1,updated_at=? WHERE id=?",(instant(),version_id))
        self.event(version_id,'inputs_changed',reason,old_status=v['status'],new_status='draft')
