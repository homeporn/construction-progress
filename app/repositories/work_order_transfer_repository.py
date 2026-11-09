"""Atomic append-only decisions over existing work orders; no primary writes."""
import hashlib
import json
from decimal import Decimal, DecimalException
from uuid import uuid4

from app.decimal_utils import ZERO, canonical, exact_sum, subtract
from app.services.work_order_control import WorkOrderControlProjection
from app.time_utils import instant
from .base import ValidationError, transaction
from .project_repository import clean
from .work_order_repository import WorkOrderRepository


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)


def number(value, *, zero=False):
    try:
        text=canonical(value)
        if Decimal(text)<ZERO or (not zero and Decimal(text)==ZERO):raise ValueError()
        return text
    except (ValueError,TypeError,DecimalException):
        raise ValidationError('Количество должно быть положительным точным десятичным числом; до9 дробных знаков') from None


class WorkOrderTransferRepository:
    def __init__(self,db,project_id=1):
        self.db,self.project_id=db,project_id
        self.orders=WorkOrderRepository(db,project_id)
        self.projection=WorkOrderControlProjection(db,project_id)

    def _perform(self,kind,key,payload,apply):
        if not self.db.in_transaction:raise RuntimeError('Control command requires a writer transaction')
        self.orders.work._writable()
        key=clean(key,'Идентификатор команды',100)
        digest=hashlib.sha256(encoded(payload).encode('utf-8')).hexdigest()
        old=self.db.execute('SELECT * FROM work_order_control_commands WHERE id=?',(key,)).fetchone()
        if old:
            if (old['project_id'],old['kind'],old['fingerprint'])!=(self.project_id,kind,digest):
                raise ValidationError('Конфликт: идентификатор команды использован с другими параметрами')
            return json.loads(old['result_json'])
        self.db.execute('INSERT INTO work_order_control_commands VALUES (?,?,?,?,?,?)',(key,self.project_id,kind,digest,'null',instant()))
        result=apply(key)
        self.db.execute('UPDATE work_order_control_commands SET result_json=? WHERE id=?',(encoded(result),key))
        return result

    def _run(self,kind,key,payload,apply):
        with transaction(self.db):return self._perform(kind,key,payload,apply)

    def _line(self,line_id):
        row=self.db.execute('SELECT l.*,o.status,o.order_number,o.project_id,o.shift_date FROM work_order_lines l JOIN work_orders o ON o.id=l.work_order_id WHERE l.id=? AND o.project_id=?',(line_id,self.project_id)).fetchone()
        if row is None:raise ValidationError('Строка наряда не принадлежит текущему объекту')
        return dict(row)

    def _metrics(self,line_id):
        line=self._line(line_id)
        return line,next(r for r in self.projection.metrics(line['work_order_id'])[0] if r['line_id']==line_id)

    def _executor(self,order_id):
        order=self.orders._order(order_id)
        header=json.loads(order['header_snapshot']) if order['header_snapshot'] else self.orders._header_snapshot(order)
        crew=[{k:c[k] for k in ('specialty_id','specialty_name_snapshot','planned_count','employee_ids','names','brigades')} for c in self.orders._read_crew(order_id)]
        return encoded(dict(contractor_id=order['contractor_id'],contractor_name=header['contractor_name'],foreman=header['foreman'],issued_by=header['issued_by'],crew=crew,initiator='Локальная регистрация; личность не подтверждена'))

    def transfer(self,source_order_id,lines,header,crew=(),brigade_ids=(),*,reason,comment='',operation_key):
        reason=clean(reason,'Основание переноса',4000);comment=clean(comment,'Комментарий',4000,False)
        if not isinstance(lines,(list,tuple)) or not lines or len(lines)>1000:raise ValidationError('Выберите от1 до1000 строк переноса')
        normalized=[dict(source_line_id=r.get('source_line_id'),quantity=number(r.get('quantity'))) for r in lines]
        if len({r['source_line_id'] for r in normalized})!=len(normalized):raise ValidationError('Исходная строка не может повторяться')
        payload=dict(source_order_id=source_order_id,lines=normalized,header=header,crew=crew,brigade_ids=brigade_ids,reason=reason,comment=comment)
        def apply(key):
            source=self.orders._order(source_order_id)
            if source['order_number'] is None or source['status']=='cancelled':raise ValidationError('Перенос доступен только из выданного неотменённого наряда')
            values={r['line_id']:r for r in self.projection.metrics(source_order_id)[0]}
            new_lines=[]
            for line in normalized:
                row=values.get(line['source_line_id'])
                if row is None:raise ValidationError('Исходная строка не относится к выбранному наряду')
                if row['conflicts']:raise ValidationError('Разрешите расхождения исходной строки: '+'; '.join(row['conflicts']))
                if Decimal(line['quantity'])>row['available']:raise ValidationError('Недостаточный доступный остаток для переноса')
                new_lines.append(dict(work_item_id=row['work_item_id'],planned_quantity=line['quantity'],remarks='Наследуемый остаток; '+reason[:1800]))
            if header.get('shift_date','')<source['shift_date']:raise ValidationError('Новая смена не может предшествовать исходной смене')
            target=self.orders._save_draft(header,new_lines,crew,draft_key='transfer-'+uuid4().hex,brigade_ids=brigade_ids)
            now=instant()
            transfer_id=self.db.execute('INSERT INTO work_order_transfers(project_id,source_order_id,target_order_id,command_id,reason,comment,created_at,target_shift_date,source_executor_snapshot,target_executor_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (self.project_id,source_order_id,target,key,reason,comment,now,header['shift_date'],self._executor(source_order_id),self._executor(target))).lastrowid
            targets={r['work_item_id']:dict(r) for r in self.db.execute('SELECT * FROM work_order_lines WHERE work_order_id=?',(target,))}
            ids=[]
            for row in normalized:
                original=values[row['source_line_id']];child=targets[original['work_item_id']]
                if child['unit_snapshot']!=original['unit_snapshot']:raise ValidationError('Единица новой версии работы несовместима с исходной строкой')
                ids.append(self.db.execute('INSERT INTO work_order_transfer_lines(transfer_id,source_line_id,target_line_id,quantity,unit) VALUES (?,?,?,?,?)',
                    (transfer_id,row['source_line_id'],child['id'],row['quantity'],child['unit_snapshot'])).lastrowid)
            return dict(transfer_id=transfer_id,order_id=target,line_ids=ids)
        return self._run('transfer',operation_key,payload,apply)

    def _cancel(self,transfer_id,reason,key):
        transfer=self.db.execute('SELECT t.*,o.status FROM work_order_transfers t JOIN work_orders o ON o.id=t.target_order_id WHERE t.id=? AND t.project_id=?',(transfer_id,self.project_id)).fetchone()
        if transfer is None:raise ValidationError('Перенос не принадлежит текущему объекту')
        if self.db.execute('SELECT 1 FROM work_order_transfer_cancellations WHERE transfer_id=?',(transfer_id,)).fetchone():raise ValidationError('Перенос уже отменён')
        if transfer['status']!='draft':raise ValidationError('Дочерний наряд уже выдан или отменён; безопасная отмена переноса невозможна')
        target=transfer['target_order_id']
        dependent=self.db.execute('SELECT 1 FROM work_order_transfers WHERE source_order_id=? UNION ALL SELECT 1 FROM fact_events f JOIN work_order_lines l ON l.id=f.work_order_line_id WHERE l.work_order_id=? UNION ALL SELECT 1 FROM confirmation_events c JOIN work_order_lines l ON l.id=c.work_order_line_id WHERE l.work_order_id=? UNION ALL SELECT 1 FROM work_order_resolutions r JOIN work_order_lines l ON l.id=r.line_id WHERE l.work_order_id=? LIMIT 1',(target,target,target,target)).fetchone()
        if dependent:raise ValidationError('Дочерний наряд имеет зависимые операции; отмена заблокирована')
        now=instant()
        self.db.execute('INSERT INTO work_order_transfer_cancellations(transfer_id,command_id,reason,recorded_at) VALUES (?,?,?,?)',(transfer_id,key,reason,now))
        self.db.execute("UPDATE work_orders SET status='cancelled',cancellation_reason=?,updated_at=? WHERE id=?",(reason,now,target))
        return True

    def cancel_transfer(self,transfer_id,reason,operation_key):
        reason=clean(reason,'Основание отмены переноса',4000)
        return self._run('cancel_transfer',operation_key,[transfer_id,reason],lambda key:self._cancel(transfer_id,reason,key))

    def cancel_target_in_transaction(self,order_id,reason):
        row=self.db.execute('SELECT t.id FROM work_order_transfers t WHERE t.target_order_id=? AND t.project_id=? AND NOT EXISTS(SELECT 1 FROM work_order_transfer_cancellations c WHERE c.transfer_id=t.id)',(order_id,self.project_id)).fetchone()
        if row is None:return False
        return self._perform('cancel_transfer',uuid4().hex,[row['id'],reason],lambda key:self._cancel(row['id'],reason,key))

    def writeoff(self,line_id,quantity,reason,operation_key):
        quantity=number(quantity);reason=clean(reason,'Основание списания',4000)
        def apply(key):
            line,row=self._metrics(line_id)
            if not line['order_number'] or line['status']=='cancelled':raise ValidationError('Списание доступно для выданной неотменённой строки')
            if row['conflicts'] or Decimal(quantity)>row['available']:raise ValidationError('Списание превышает свободный невыполненный остаток или строка содержит конфликт')
            return self.db.execute("INSERT INTO work_order_resolutions(project_id,line_id,kind,quantity,reason,recorded_at,command_id) VALUES (?,?,'writeoff',?,?,?,?)",(self.project_id,line_id,quantity,reason,instant(),key)).lastrowid
        return self._run('writeoff',operation_key,[line_id,quantity,reason],apply)

    def revoke_writeoff(self,resolution_id,reason,operation_key):
        reason=clean(reason,'Причина отмены списания',4000)
        def apply(key):
            row=self.db.execute("SELECT * FROM work_order_resolutions WHERE id=? AND project_id=? AND kind='writeoff'",(resolution_id,self.project_id)).fetchone()
            if row is None:raise ValidationError('Списание не принадлежит текущему объекту')
            self._line(row['line_id'])
            if self.db.execute('SELECT 1 FROM work_order_resolutions WHERE reverses_id=?',(resolution_id,)).fetchone():raise ValidationError('Списание уже отменено')
            return self.db.execute("INSERT INTO work_order_resolutions(project_id,line_id,kind,quantity,reason,recorded_at,command_id,reverses_id) VALUES (?,?,'revoke',?,?,?,?,?)",(self.project_id,row['line_id'],row['quantity'],reason,instant(),key,resolution_id)).lastrowid
        return self._run('revoke_writeoff',operation_key,[resolution_id,reason],apply)

    def match(self,line_id,fact_event_id,confirmation_event_id,quantity,reason,expected_revision=0,operation_key=None):
        quantity=number(quantity,zero=True);reason=clean(reason,'Основание сопоставления',4000)
        payload=[line_id,fact_event_id,confirmation_event_id,quantity,reason,expected_revision]
        def apply(key):
            line,row=self._metrics(line_id)
            if line['status']=='cancelled':raise ValidationError('Сопоставление отменённого наряда недоступно')
            previous=self.db.execute('SELECT * FROM work_order_volume_matches WHERE line_id=? AND fact_event_id=? AND confirmation_event_id=? ORDER BY revision_no DESC LIMIT 1',(line_id,fact_event_id,confirmation_event_id)).fetchone()
            revision=previous['revision_no'] if previous else 0
            if revision!=expected_revision:raise ValidationError('Редакция сопоставления изменилась; обновите страницу')
            if previous is None and quantity=='0':raise ValidationError('Нулевое сопоставление может только отменять прежнюю запись')
            roots_f,roots_c=row['fact_roots'],row['confirmation_roots']
            if fact_event_id not in roots_f or confirmation_event_id not in roots_c:raise ValidationError('Выберите корни факта и подтверждения, достоверно связанные с этой строкой')
            others=[dict(r) for r in self.db.execute('SELECT m.* FROM work_order_volume_matches m WHERE m.line_id=? AND NOT EXISTS(SELECT 1 FROM work_order_volume_matches n WHERE n.line_id=m.line_id AND n.fact_event_id=m.fact_event_id AND n.confirmation_event_id=m.confirmation_event_id AND n.revision_no>m.revision_no)',(line_id,)) if (r['fact_event_id'],r['confirmation_event_id'])!=(fact_event_id,confirmation_event_id)]
            available_f=subtract(roots_f[fact_event_id],exact_sum(Decimal(r['quantity']) for r in others if r['fact_event_id']==fact_event_id))
            available_c=subtract(roots_c[confirmation_event_id],exact_sum(Decimal(r['quantity']) for r in others if r['confirmation_event_id']==confirmation_event_id))
            if Decimal(quantity)>ZERO and Decimal(quantity)>min(available_f,available_c):raise ValidationError('Сопоставляемое количество превышает доступную ёмкость исходных событий')
            return self.db.execute('INSERT INTO work_order_volume_matches(project_id,line_id,fact_event_id,confirmation_event_id,quantity,revision_no,previous_id,reason,recorded_at,command_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (self.project_id,line_id,fact_event_id,confirmation_event_id,quantity,revision+1,previous['id'] if previous else None,reason,instant(),key)).lastrowid
        return self._run('match',operation_key,payload,apply)
