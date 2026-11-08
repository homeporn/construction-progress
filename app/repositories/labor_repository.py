"""Append-only analytical assignments; primary journals are never written here."""
from collections import defaultdict
from decimal import Decimal
import hashlib
import json
from uuid import uuid4

from app.decimal_utils import canonical, exact_sum, subtract
from app.time_utils import day, instant, end_of_day
from .base import ValidationError, transaction
from .personnel_repository import PersonnelRepository
from .brigade_repository import BrigadeRepository
from .timesheet_repository import hours_value, decimal_hours
from .project_repository import clean


class LaborRepository:
    def __init__(self, db, project_id=1):
        self.db, self.project_id = db, project_id
        self.people = PersonnelRepository(db, project_id)

    def metadata(self):
        return [dict(r) for r in self.db.execute('SELECT w.*,c.code,c.name,c.unit,c.element,c.work_type,b.name AS building_name,b.status AS building_status,f.floor_code '
            'FROM work_items w JOIN work_catalog c ON c.id=w.catalog_id JOIN buildings b ON b.id=w.building_id '
            'JOIN floors f ON f.id=w.floor_id WHERE b.project_id=? ORDER BY w.id',(self.project_id,))]

    def plan_line(self, item_id, business_date, cutoff=None, version_id=None):
        version=self.db.execute("SELECT v.id FROM plan_versions v JOIN work_items w ON w.discipline=v.discipline "
            "JOIN buildings b ON b.id=w.building_id WHERE w.id=? AND b.project_id=? AND v.project_id=? "
            "AND v.status='APPROVED' AND v.approved_at<=? AND v.approved_at<=? AND (? IS NULL OR v.id=?) "
            "ORDER BY v.version_no DESC LIMIT 1",(item_id,self.project_id,self.project_id,instant(cutoff),end_of_day(business_date),version_id,version_id)).fetchone()
        if version is None:return None
        row=self.db.execute('SELECT * FROM plan_lines WHERE plan_version_id=? AND work_item_id=?',(version['id'],item_id)).fetchone()
        if row is None:return None
        return dict(row,labor_norm=Decimal(row['labor_norm']) if row['labor_norm'] is not None else None)

    def _memberships(self,start,end,cutoff):
        return [dict(r) for r in self.db.execute('SELECT a.*,b.contractor_id FROM brigade_assignments a JOIN brigades b ON b.id=a.brigade_id '
            'JOIN contractor_projects c ON c.contractor_id=b.contractor_id WHERE c.project_id=? AND a.start_date<=? '
            'AND a.recorded_at<=? AND (a.end_date IS NULL OR a.end_date>? OR a.closed_at>?)',
            (self.project_id,end,cutoff,start,cutoff))]

    def approved_entries(self,start,end,cutoff=None):
        start,end,cutoff=day(start),day(end),instant(cutoff)
        rows=[dict(r) for r in self.db.execute("SELECT e.*,e.id AS entry_id,t.contractor_id,tw.full_name FROM timesheet_entries e "
            "JOIN timesheet_versions v ON v.id=e.version_id JOIN timesheets t ON t.id=v.timesheet_id "
            "JOIN timesheet_workers tw ON tw.version_id=e.version_id AND tw.employee_id=e.employee_id "
            "WHERE t.project_id=? AND v.status='approved' AND v.approved_at<=? AND e.work_date BETWEEN ? AND ? "
            "AND v.version_no=(SELECT MAX(x.version_no) FROM timesheet_versions x WHERE x.timesheet_id=t.id AND x.status='approved' AND x.approved_at<=?) ORDER BY e.work_date,e.employee_id",
            (self.project_id,cutoff,start,end,cutoff))]
        members=defaultdict(list)
        for m in self._memberships(start,end,cutoff):members[m['employee_id']].append(m)
        for r in rows:
            r['hours']=decimal_hours(r['hours_nanos']) if r['counts_as_work'] else Decimal(0)
            matches=[m for m in members[r['employee_id']] if m['contractor_id']==r['contractor_id'] and m['start_date']<=r['work_date'] and (m['end_date'] is None or m['end_date']>r['work_date'] or m['closed_at']>cutoff)]
            r['brigade_id']=matches[-1]['brigade_id'] if matches else None
        return rows

    def hour_rows(self,cutoff=None,start='0001-01-01',end='9999-12-31'):
        cutoff=instant(cutoff)
        rows=self.db.execute('SELECT a.*,o.recorded_at,t.contractor_id FROM labor_hour_allocations a JOIN labor_operations o ON o.id=a.operation_id '
            'JOIN timesheet_entries e ON e.id=a.timesheet_entry_id JOIN timesheet_versions v ON v.id=e.version_id JOIN timesheets t ON t.id=v.timesheet_id '
            'WHERE o.project_id=? AND o.recorded_at<=? AND a.work_date BETWEEN ? AND ? AND NOT EXISTS('
            'SELECT 1 FROM labor_hour_allocations n JOIN labor_operations no ON no.id=n.operation_id WHERE n.employee_id=a.employee_id '
            'AND n.work_date=a.work_date AND n.work_item_id=a.work_item_id AND n.revision_no>a.revision_no AND no.recorded_at<=?) ORDER BY a.work_date,a.employee_id,a.id',
            (self.project_id,cutoff,start,end,cutoff))
        return [dict(r,hours=decimal_hours(r['hours_nanos'])) for r in rows]

    def attribution_rows(self,cutoff=None,fact_event_id=None):
        cutoff=instant(cutoff)
        return [dict(r,quantity=Decimal(r['quantity'])) for r in self.db.execute('SELECT a.*,o.recorded_at FROM labor_volume_attributions a JOIN labor_operations o ON o.id=a.operation_id '
            'WHERE o.project_id=? AND o.recorded_at<=? AND (? IS NULL OR a.fact_event_id=?) AND NOT EXISTS(SELECT 1 FROM labor_volume_attributions n JOIN labor_operations no ON no.id=n.operation_id '
            'WHERE n.fact_event_id=a.fact_event_id AND n.contractor_id=a.contractor_id AND n.brigade_id IS a.brigade_id AND n.employee_id IS a.employee_id '
            'AND n.revision_no>a.revision_no AND no.recorded_at<=?) ORDER BY a.id',(self.project_id,cutoff,fact_event_id,fact_event_id,cutoff))]

    def fact_roots(self,end_date,cutoff=None):
        rows=[dict(r) for r in self.db.execute('SELECT f.* FROM fact_events f JOIN work_items w ON w.id=f.work_item_id '
            'JOIN buildings b ON b.id=w.building_id WHERE b.project_id=? AND f.work_date<=? AND f.recorded_at<=? ORDER BY f.id',
            (self.project_id,day(end_date),instant(cutoff)))]
        byid={r['id']:r for r in rows};cache={};totals=defaultdict(list)
        for r in rows:
            cursor=r;trail=[]
            while cursor['correction_of_id'] is not None and cursor['id'] not in cache:
                trail.append(cursor['id']);cursor=byid.get(cursor['correction_of_id'])
                if cursor is None:raise ValidationError('Неполная цепочка корректировок факта')
            root=cache.get(cursor['id'],cursor['id'])
            for node in trail:cache[node]=root
            totals[root].append(Decimal(r['quantity_delta']))
        return [dict(r,quantity=Decimal(r['quantity_delta']),available=exact_sum(totals[r['id']])) for r in rows if r['correction_of_id'] is None]

    def root_capacity(self,event_id,end_date,cutoff):
        root=self.db.execute('SELECT f.* FROM fact_events f JOIN work_items w ON w.id=f.work_item_id JOIN buildings b ON b.id=w.building_id '
            'WHERE f.id=? AND b.project_id=? AND f.correction_of_id IS NULL AND f.recorded_at<=?',(event_id,self.project_id,cutoff)).fetchone()
        if root is None:raise ValidationError('Исходный факт не принадлежит объекту или недоступен')
        rows=self.db.execute('WITH RECURSIVE family(id,quantity_delta,work_date,recorded_at) AS ('
            'SELECT id,quantity_delta,work_date,recorded_at FROM fact_events WHERE id=? UNION ALL '
            'SELECT f.id,f.quantity_delta,f.work_date,f.recorded_at FROM fact_events f JOIN family p ON f.correction_of_id=p.id) '
            'SELECT quantity_delta FROM family WHERE work_date<=? AND recorded_at<=?',(event_id,end_date,cutoff))
        return dict(root),exact_sum(Decimal(r[0]) for r in rows)

    def available_hours(self,work_date,contractor_id=None,brigade_id=None,cutoff=None):
        cutoff=instant(cutoff);work_date=day(work_date)
        if contractor_id is not None:self.people.contractor(contractor_id)
        if brigade_id is not None:BrigadeRepository(self.db,self.project_id).get(brigade_id)
        used=defaultdict(list)
        for r in self.hour_rows(cutoff,work_date,work_date):used[(r['employee_id'],r['contractor_id'])].append(r['hours'])
        result=[]
        for r in self.approved_entries(work_date,work_date,cutoff):
            if contractor_id is not None and r['contractor_id']!=contractor_id:continue
            if brigade_id is not None and r['brigade_id']!=brigade_id:continue
            allocated=exact_sum(used[(r['employee_id'],r['contractor_id'])]);remaining=subtract(r['hours'],allocated)
            result.append(dict(r,allocated=allocated,remaining=remaining,conflict=remaining<0))
        return result

    def _operation(self,key,kind,payload,reason,recorded_at):
        key=clean(key or uuid4().hex,'Ключ операции',200);reason=clean(reason,'Причина',4000,False)
        fingerprint=hashlib.sha256(json.dumps([kind,payload,reason],sort_keys=True,default=str).encode()).hexdigest()
        old=self.db.execute('SELECT * FROM labor_operations WHERE id=?',(key,)).fetchone()
        if old:
            if old['project_id']!=self.project_id or old['fingerprint']!=fingerprint:raise ValidationError('Ключ операции использован для другого запроса')
            return key,True
        latest=self.db.execute('SELECT MAX(recorded_at) FROM labor_operations WHERE project_id=?',(self.project_id,)).fetchone()[0]
        if latest and recorded_at<latest:raise ValidationError('Время регистрации предшествует уже известной операции')
        self.db.execute('INSERT INTO labor_operations VALUES (?,?,?,?,?,?)',(key,self.project_id,kind,fingerprint,recorded_at,reason))
        return key,False

    def allocate(self,work_item_id,work_date,allocations,*,operation_key=None,reason='',recorded_at=None,plan_version_id=None):
        work_date=day(work_date)
        if not isinstance(allocations,list) or not 1<=len(allocations)<=500:raise ValidationError('Выберите от 1 до 500 работников')
        normalized=[dict(employee_id=int(r['employee_id']),hours_nanos=hours_value(r['hours']),expected_revision=int(r.get('expected_revision',0)),expected_timesheet_entry_id=r.get('expected_timesheet_entry_id'),expected_brigade_id=r.get('expected_brigade_id','unchecked')) for r in allocations]
        if len({r['employee_id'] for r in normalized})!=len(normalized):raise ValidationError('Повтор работника в операции')
        with transaction(self.db):
            now=instant(recorded_at)  # Sample knowledge only AFTER acquiring the SQLite writer lock.
            self.people.work._writable()
            key,replay=self._operation(operation_key,'hours',[work_item_id,work_date,normalized,plan_version_id],reason,now)
            if replay:return key
            item=self.db.execute('SELECT w.id,b.status FROM work_items w JOIN buildings b ON b.id=w.building_id WHERE w.id=? AND b.project_id=?',(work_item_id,self.project_id)).fetchone()
            if item is None:raise ValidationError('Работа другого объекта')
            line=self.plan_line(work_item_id,work_date,now,plan_version_id)
            if not line:raise ValidationError('Работа отсутствует в утверждённом плане на дату')
            available=defaultdict(list)
            for entry in self.available_hours(work_date,cutoff=now):available[entry['employee_id']].append(entry)
            current={r['employee_id']:r for r in self.hour_rows(now,work_date,work_date) if r['work_item_id']==work_item_id}
            for r in normalized:
                sources=available.get(r['employee_id'],[]);old=current.get(r['employee_id'])
                if r['expected_timesheet_entry_id'] is not None:
                    sources=[x for x in sources if x['entry_id']==int(r['expected_timesheet_entry_id'])]
                elif old:
                    sources=[x for x in sources if x['contractor_id']==old['contractor_id']]
                if len(sources)>1:raise ValidationError('Несколько утверждённых источников часов: выберите подрядчика и запись табеля')
                source=sources[0] if sources else None
                if source and r['expected_brigade_id']!='unchecked' and r['expected_brigade_id']!=source['brigade_id']:raise ValidationError('Состав бригады изменился после просмотра')
                if old and source and source['contractor_id']!=old['contractor_id']:raise ValidationError('Подрядчик старого распределения неизменяем')
                if source is None and r['expected_timesheet_entry_id'] is not None:raise ValidationError('Утверждённый табель изменился после просмотра')
                repair = old is not None and r['hours_nanos'] < old['hours_nanos'] and bool(reason.strip())
                if source is not None and r['expected_timesheet_entry_id'] is not None and int(r['expected_timesheet_entry_id'])!=source['entry_id']:
                    raise ValidationError('Утверждённый табель изменился после просмотра')
                if not source and not repair:raise ValidationError('Нет утверждённого табеля для работника на дату')
                if source is None:
                    used=exact_sum(x['hours'] for x in self.hour_rows(now,work_date,work_date) if x['employee_id']==r['employee_id'] and x['contractor_id']==old['contractor_id'])
                    source=dict(hours=Decimal(0),hours_nanos=0,counts_as_work=0,allocated=used,entry_id=old['timesheet_entry_id'],brigade_id=old['brigade_id'])
                entry_id=source['entry_id'] if source['counts_as_work'] and r['hours_nanos']<=source['hours_nanos'] else (old['timesheet_entry_id'] if repair else source['entry_id'])
                if r['expected_revision']!=(old['revision_no'] if old else 0):raise ValidationError('Распределение изменилось; обновите форму')
                if old and not reason.strip():raise ValidationError('Укажите причину корректировки')
                if not old and (r['hours_nanos']==0 or item['status']!='active'):raise ValidationError('Нулевая операция или архивный корпус')
                total=exact_sum([source['allocated'],(old['hours'] if old else Decimal(0)).copy_negate(),decimal_hours(r['hours_nanos'])])
                if total>source['hours'] and not repair:raise ValidationError('Распределение превышает утверждённые часы')
                self.db.execute('INSERT INTO labor_hour_allocations(operation_id,employee_id,work_date,work_item_id,plan_version_id,timesheet_entry_id,brigade_id,hours_nanos,revision_no,previous_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (key,r['employee_id'],work_date,work_item_id,line['plan_version_id'],entry_id,old['brigade_id'] if old else source['brigade_id'],r['hours_nanos'],r['expected_revision']+1,old['id'] if old else None))
            return key

    def attribute(self,fact_event_id,contractor_id,quantity,*,brigade_id=None,employee_id=None,expected_revision=0,operation_key=None,reason='',recorded_at=None,plan_version_id=None):
        quantity=Decimal(canonical(quantity))
        if quantity<0:raise ValidationError('Отрицательный объём запрещён')
        with transaction(self.db):
            now=instant(recorded_at)  # Sample knowledge only AFTER acquiring the SQLite writer lock.
            self.people.work._writable()
            key,replay=self._operation(operation_key,'volume',[fact_event_id,contractor_id,str(quantity),brigade_id,employee_id,expected_revision,plan_version_id],reason,now)
            if replay:return key
            self.people.contractor(contractor_id)
            root,capacity=self.root_capacity(fact_event_id,now[:10],now)
            if Decimal(root['quantity_delta'])<=0:raise ValidationError('Требуется положительный исходный факт')
            if brigade_id is not None and BrigadeRepository(self.db,self.project_id).get(brigade_id)['contractor_id']!=contractor_id:raise ValidationError('Бригада другого подрядчика')
            rows=self.attribution_rows(now,fact_event_id)
            old=next((r for r in rows if r['contractor_id']==contractor_id and r['brigade_id']==brigade_id and r['employee_id']==employee_id),None)
            if employee_id is not None and not (old is not None and quantity<old['quantity']):
                e=self.people.employee(employee_id)
                if e['contractor_id']!=contractor_id or not self.people.available(employee_id,root['work_date'],now):raise ValidationError('Работник вне контекста подрядчика или объекта')
                if brigade_id is not None:
                    member=BrigadeRepository(self.db,self.project_id).current(employee_id,root['work_date'],now)
                    if member is None or member['brigade_id']!=brigade_id:raise ValidationError('Работник не входил в бригаду на дату факта')
            line=self.plan_line(root['work_item_id'],root['work_date'],now,plan_version_id)
            if line is None:raise ValidationError('Нет утверждённой строки плана на дату факта')
            if expected_revision!=(old['revision_no'] if old else 0):raise ValidationError('Атрибуция изменилась; обновите форму')
            if old and not reason.strip():raise ValidationError('Укажите причину корректировки')
            if old is None and quantity==0:raise ValidationError('Нулевая атрибуция не создаётся')
            assigned=exact_sum(r['quantity'] for r in rows if old is None or r['id']!=old['id'])
            if exact_sum([assigned,quantity])>max(Decimal(0),capacity) and not (old is not None and quantity<old['quantity'] and reason.strip()):raise ValidationError('Распределённый объём превышает доступный факт')
            self.db.execute('INSERT INTO labor_volume_attributions(operation_id,fact_event_id,contractor_id,brigade_id,employee_id,plan_version_id,quantity,revision_no,previous_id) VALUES (?,?,?,?,?,?,?,?,?)',
                (key,fact_event_id,contractor_id,brigade_id,employee_id,line['plan_version_id'],canonical(quantity),expected_revision+1,old['id'] if old else None))
            return key

    def history(self,limit=50,offset=0,cutoff=None):
        if not isinstance(limit,int) or not 1<=limit<=100 or not isinstance(offset,int) or offset<0:raise ValidationError('Некорректная страница')
        return [dict(r) for r in self.db.execute('SELECT * FROM labor_operations WHERE project_id=? AND recorded_at<=? ORDER BY recorded_at DESC,id DESC LIMIT ? OFFSET ?',(self.project_id,instant(cutoff),limit,offset))]
