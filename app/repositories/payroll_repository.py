"""Versioned managerial accruals. Approval freezes a calculation, never a payment."""
import calendar
from collections import defaultdict
from decimal import Decimal
import json

from app.decimal_utils import canonical, decimal_value, exact_sum, ratio, subtract
from app.time_utils import day, instant
from app.services.payroll_money import from_minor,to_minor
from .base import ValidationError,transaction
from .payroll_base import PayrollBase,encode,fingerprint
from .project_repository import clean


class PayrollRepository(PayrollBase):
    def create_period(self,contractor_id,year,month,norm_hours=None,comment='',operation_key=None):
        if isinstance(year,bool) or isinstance(month,bool) or not isinstance(year,int) or not isinstance(month,int) or not 1900<=year<=9999 or not 1<=month<=12:
            raise ValidationError('Выберите год и календарный месяц')
        norm=self._norm_value(norm_hours);comment=clean(comment,'Комментарий',4000,False)
        payload=[contractor_id,year,month,norm,comment]
        with transaction(self.db):
            replay,result=self.replay(operation_key,'period_create',payload)
            if replay:return result
            self.scope();self.people.contractor(contractor_id)
            if self.db.execute('SELECT id FROM payroll_periods WHERE project_id=? AND contractor_id=? AND year=? AND month=?',(self.project_id,contractor_id,year,month)).fetchone():raise ValidationError('Расчётный период уже существует')
            now=instant();pid=self.db.execute('INSERT INTO payroll_periods(project_id,contractor_id,year,month,created_at) VALUES (?,?,?,?,?)',(self.project_id,contractor_id,year,month,now)).lastrowid
            vid=self.db.execute('INSERT INTO payroll_versions(period_id,version_no,norm_hours,comment,created_at,updated_at) VALUES (?,1,?,?,?,?)',(pid,norm,comment,now,now)).lastrowid
            self.event(vid,'created');return self.remember(operation_key,'period_create',payload,pid)

    @staticmethod
    def _norm_value(value):
        if value in (None,''):return None
        try:
            number=decimal_value(value)
            if not 0<number<=744:raise ValueError()
            return canonical(number)
        except (ValueError,TypeError,ArithmeticError):raise ValidationError('Норма часов: явно задайте положительное значение до744') from None

    def list_periods(self):
        return [dict(r) for r in self.db.execute('SELECT p.*,c.name AS contractor_name,v.id AS version_id,v.version_no,v.status,v.revision_no '
            'FROM payroll_periods p JOIN contractors c ON c.id=p.contractor_id JOIN payroll_versions v ON v.period_id=p.id '
            'WHERE p.project_id=? AND v.version_no=(SELECT MAX(x.version_no) FROM payroll_versions x WHERE x.period_id=p.id) ORDER BY p.year DESC,p.month DESC,p.id DESC',(self.project_id,))]

    def get_period(self,period_id):return self.require_period(period_id)

    def get_version(self,period_id,version_id=None):
        self.require_period(period_id)
        if version_id is None:
            r=self.db.execute('SELECT id FROM payroll_versions WHERE period_id=? ORDER BY version_no DESC LIMIT 1',(period_id,)).fetchone()
            version_id=r['id'] if r else None
        v=self.require_version(version_id)
        if v['period_id']!=period_id:raise ValidationError('Версия другого расчётного периода')
        return v

    def update_settings(self,version_id,norm_hours,comment='',expected_revision=None,operation_key=None):
        norm=self._norm_value(norm_hours);comment=clean(comment,'Комментарий',4000,False);payload=[version_id,norm,comment,expected_revision]
        with transaction(self.db):
            replay,result=self.replay(operation_key,'period_settings',payload)
            if replay:return result
            v=self.require_version(version_id,editable=True)
            if expected_revision is not None and v['revision_no']!=int(expected_revision):raise ValidationError('Версия изменена; обновите страницу')
            self.dirty(version_id,'Изменены настройки периода')
            self.db.execute('UPDATE payroll_versions SET norm_hours=?,comment=? WHERE id=?',(norm,comment,version_id))
            return self.remember(operation_key,'period_settings',payload,True)

    def add_adjustment(self,version_id,employee_id,amount,kind='adjustment',currency='RUB',effective_date=None,reason='',operation_key=None):
        from .payroll_rules_repository import CURRENCIES
        minor=to_minor(amount);reason=clean(reason,'Основание',4000);payload=[version_id,employee_id,minor,kind,currency,effective_date,reason]
        if kind not in ('bonus','adjustment','supplement') or currency not in CURRENCIES or not minor or abs(minor)>9000000000000000:
            raise ValidationError('Некорректное ручное начисление')
        if kind in ('bonus','supplement') and minor<0:raise ValidationError('Для уменьшения используйте управленческую корректировку')
        with transaction(self.db):
            replay,result=self.replay(operation_key,'adjustment',payload)
            if replay:return result
            v=self.require_version(version_id,editable=True);e=self.people.employee(employee_id)
            approved=self.db.execute("SELECT 1 FROM timesheet_workers w JOIN timesheet_versions v ON v.id=w.version_id JOIN timesheets t ON t.id=v.timesheet_id WHERE w.employee_id=? AND t.project_id=? AND t.contractor_id=? AND t.year=? AND t.month=? AND v.status='approved'",(employee_id,self.project_id,v['contractor_id'],v['year'],v['month'])).fetchone()
            if e['contractor_id']!=v['contractor_id'] and not approved:raise ValidationError('Работник другого подрядчика')
            stamp=day(effective_date or '{:04d}-{:02d}-01'.format(v['year'],v['month']))
            if stamp[:7]!='{:04d}-{:02d}'.format(v['year'],v['month']):raise ValidationError('Дата корректировки вне расчётного месяца')
            ident=self.db.execute('INSERT INTO payroll_adjustments(version_id,employee_id,effective_date,kind,amount_minor,currency,reason,recorded_at) VALUES (?,?,?,?,?,?,?,?)',(version_id,employee_id,stamp,kind,minor,currency,reason,instant())).lastrowid
            self.dirty(version_id,'Ручное начисление с основанием');return self.remember(operation_key,'adjustment',payload,ident)

    def _compute(self,v,cutoff=None):
        from .payroll_piece_repository import PayrollPieceRepository
        from app.services.payroll_calculator import PayrollCalculator
        cutoff=instant(cutoff);p=self.require_period(v['period_id'])
        piece=PayrollPieceRepository(self.db,self.project_id).validate(v['id'],cutoff)
        adjustments=[dict(r) for r in self.db.execute('SELECT * FROM payroll_adjustments WHERE version_id=? ORDER BY id',(v['id'],))]
        result=PayrollCalculator(self.db,self.project_id).calculate(p,v,cutoff,piece_lines=piece.get('lines',[]),adjustments=adjustments)
        result['errors']=list(dict.fromkeys(result.get('errors',[])+piece.get('errors',[])))
        result['warnings']=list(dict.fromkeys(result.get('warnings',[])+piece.get('warnings',[])))
        result['sources']=dict(calculator=result.get('sources',{}),pieces=piece.get('sources',{}),validation_errors=result['errors'],settings=dict(norm_hours=v['norm_hours'],comment=v['comment']))
        result['source_digest']=fingerprint(result['sources']);result['cutoff']=cutoff
        return result

    @staticmethod
    def _rows(lines,workers):
        names={r['employee_id']:r.get('full_name',str(r['employee_id'])) for r in workers}
        result={}
        for line in lines:
            key=(line['employee_id'],line['currency'])
            row=result.setdefault(key,dict(employee_id=key[0],full_name=names.get(key[0],str(key[0])),currency=key[1],system=set(),base=0,piece=0,bonus=0,supplement=0,adjustment=0,total=0))
            component=line['component'];category='base' if component.startswith('base_') else component
            row[category]+=line['amount_minor'];row['total']+=line['amount_minor']
            source=line.get('source',{})
            term=source.get('term') or next((t for t in source.get('snapshot',{}).get('dependencies',{}).get('terms',[]) if t['employee_id']==line['employee_id']),{})
            if term.get('system'):row['system'].add(term['system'])
        for row in result.values():
            row['system']=' + '.join(sorted(row['system'])) or 'manual'
            for key in ('base','piece','bonus','supplement','adjustment','total'):row[key]=from_minor(row[key])
        return [result[k] for k in sorted(result)]

    def calculate(self,version_id,operation_key=None):
        payload=[version_id]
        with transaction(self.db):
            replay,result=self.replay(operation_key,'calculate',payload)
            if replay:return result
            v=self.require_version(version_id,editable=True);data=self._compute(v)
            run_no=self.db.execute('SELECT COALESCE(MAX(run_no),0)+1 FROM payroll_runs WHERE version_id=?',(version_id,)).fetchone()[0]
            rows=self._rows(data['lines'],data.get('workers',[]));now=instant()
            run=self.db.execute('INSERT INTO payroll_runs(version_id,run_no,cutoff,source_digest,source_json,rows_json,totals_json,warnings_json,errors_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (version_id,run_no,data['cutoff'],data['source_digest'],encode(data['sources']),encode(rows),encode(data['totals']),encode(data['warnings']),encode(data['errors']),now)).lastrowid
            for line in data['lines']:
                self.db.execute('INSERT INTO payroll_accruals(run_id,employee_id,component,amount_minor,currency,term_revision_id,rate_revision_id,work_item_id,source_json,comment) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (run,line['employee_id'],line['component'],line['amount_minor'],line['currency'],line.get('term_revision_id'),line.get('rate_revision_id'),line.get('work_item_id'),encode(line.get('source',{})),line.get('comment','')))
            self.db.execute('UPDATE payroll_runs SET sealed=1 WHERE id=?',(run,))
            self.db.execute("UPDATE payroll_versions SET status='calculated',current_run_id=?,calculated_at=?,updated_at=?,revision_no=revision_no+1 WHERE id=?",(run,now,now,version_id))
            self.event(version_id,'calculated',old_status=v['status'],new_status='calculated',details=dict(run_id=run,errors=len(data['errors'])))
            return self.remember(operation_key,'calculate',payload,run)

    def _run(self,v):
        if v['current_run_id'] is None:return None
        return dict(self.db.execute('SELECT * FROM payroll_runs WHERE id=? AND version_id=?',(v['current_run_id'],v['id'])).fetchone())

    def transition(self,version_id,action,reason='',operation_key=None):
        mapping={'submit':('calculated','submitted'),'return':('submitted','draft'),'approve':('submitted','approved'),'close':('approved','closed')}
        if action not in mapping:raise ValidationError('Неизвестный переход начислений')
        reason=clean(reason,'Основание',4000,False);payload=[version_id,action,reason]
        with transaction(self.db):
            replay,result=self.replay(operation_key,'transition',payload)
            if replay:return result
            self.scope();v=self.require_version(version_id);old,new=mapping[action]
            if v['status']!=old:raise ValidationError('Недопустимый переход статуса начислений')
            if action=='return' and not reason:raise ValidationError('Укажите основание возврата')
            if action!='return':
                run=self._run(v)
                if run is None or json.loads(run['errors_json']):raise ValidationError('Устраните ошибки и рассчитайте ведомость')
                fresh=self._compute(v)
                if fresh['errors'] or fresh['source_digest']!=run['source_digest']:raise ValidationError('Исходные данные изменились; нужен новый расчёт или корректирующая версия')
            now=instant();field={'approve':'approved_at','close':'closed_at'}.get(action)
            query='UPDATE payroll_versions SET status=?,updated_at=?,revision_no=revision_no+1'+(', '+field+'=?' if field else '')+' WHERE id=?'
            self.db.execute(query,(new,now,now,version_id) if field else (new,now,version_id))
            self.event(version_id,action,reason,old,new);return self.remember(operation_key,'transition',payload,True)

    def create_correction(self,period_id,reason,operation_key=None):
        from .payroll_piece_repository import PayrollPieceRepository
        reason=clean(reason,'Причина пересмотра',4000);payload=[period_id,reason]
        with transaction(self.db):
            replay,result=self.replay(operation_key,'correction',payload)
            if replay:return result
            self.scope();p=self.require_period(period_id);old=self.get_version(period_id)
            if old['status'] not in ('approved','closed'):raise ValidationError('Сначала завершите или утвердите текущую версию')
            now=instant();vid=self.db.execute('INSERT INTO payroll_versions(period_id,version_no,base_version_id,correction_reason,norm_hours,comment,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                (period_id,old['version_no']+1,old['id'],reason,old['norm_hours'],old['comment'],now,now)).lastrowid
            for a in self.db.execute('SELECT * FROM payroll_adjustments WHERE version_id=? AND reverses_id IS NULL AND id NOT IN (SELECT reverses_id FROM payroll_adjustments WHERE reverses_id IS NOT NULL)',(old['id'],)).fetchall():
                self.db.execute('INSERT INTO payroll_adjustments(version_id,employee_id,effective_date,kind,amount_minor,currency,reason,recorded_at) VALUES (?,?,?,?,?,?,?,?)',(vid,a['employee_id'],a['effective_date'],a['kind'],a['amount_minor'],a['currency'],a['reason'],now))
            PayrollPieceRepository(self.db,self.project_id).copy_to_version(old['id'],vid)
            self.event(vid,'correction_created',reason,details=dict(base_version_id=old['id']))
            return self.remember(operation_key,'correction',payload,vid)

    def report(self,period_id,version_id=None):
        from app.calculation_engine.historical import read_snapshot
        from .payroll_piece_repository import PayrollPieceRepository
        with read_snapshot(self.db):
            p=self.require_period(period_id);v=self.get_version(period_id,version_id);run=self._run(v)
            rows=json.loads(run['rows_json']) if run else [];totals={c:from_minor(n) for c,n in json.loads(run['totals_json']).items()} if run else {}
            for row in rows:
                for name in ('base','piece','bonus','supplement','adjustment','total'):row[name]=Decimal(row[name])
            lines=[]
            if run:
                for r in self.db.execute('SELECT * FROM payroll_accruals WHERE run_id=? ORDER BY employee_id,currency,id',(run['id'],)):
                    lines.append(dict(r,amount=from_minor(r['amount_minor']),source=json.loads(r['source_json'])))
            errors=json.loads(run['errors_json']) if run else [];warnings=json.loads(run['warnings_json']) if run else [];changed=False
            if run:
                try:
                    fresh=self._compute(v)
                    changed=fresh['source_digest']!=run['source_digest'] or fresh['errors']!=errors
                except (ValueError,ArithmeticError,KeyError):changed=True
                if changed:warnings.append('Источники отличаются от зафиксированного расчёта. Утверждённые суммы сохранены; нужна новая версия.')
            delta={};employee_deltas=[]
            if v['base_version_id']:
                base=self._run(self.require_version(v['base_version_id']));oldtot={c:from_minor(n) for c,n in json.loads(base['totals_json']).items()} if base else {}
                delta={c:subtract(totals.get(c,Decimal(0)),oldtot.get(c,Decimal(0))) for c in sorted(set(totals)|set(oldtot))}
                oldrows={(r['employee_id'],r['currency']):r for r in json.loads(base['rows_json'])} if base else {}
                for row in rows:row['delta']=subtract(row['total'],Decimal(oldrows.get((row['employee_id'],row['currency']),{}).get('total','0')))
                newrows={(r['employee_id'],r['currency']):r for r in rows}
                for key in sorted(set(newrows)|set(oldrows)):
                    newrow=newrows.get(key);oldrow=oldrows.get(key);before=Decimal(oldrow['total']) if oldrow else Decimal(0);after=newrow['total'] if newrow else Decimal(0)
                    employee_deltas.append(dict(employee_id=key[0],currency=key[1],full_name=(newrow or oldrow)['full_name'],before=before,after=after,delta=subtract(after,before)))
            return dict(period=p,version=v,rows=rows,lines=lines,totals=totals,errors=errors,warnings=warnings,source_changed=changed,delta=delta,employee_deltas=employee_deltas,
                sources=json.loads(run['source_json']) if run else {},run=run,
                versions=[dict(x) for x in self.db.execute('SELECT * FROM payroll_versions WHERE period_id=? ORDER BY version_no DESC',(period_id,))],
                events=[dict(x) for x in self.db.execute('SELECT * FROM payroll_events WHERE version_id=? ORDER BY id',(v['id'],))],
                runs=[dict(x) for x in self.db.execute('SELECT id,run_no,cutoff,source_digest,created_at FROM payroll_runs WHERE version_id=? ORDER BY run_no',(v['id'],))],
                adjustments=[dict(x,amount=from_minor(x['amount_minor'])) for x in self.db.execute('SELECT * FROM payroll_adjustments WHERE version_id=? ORDER BY id',(v['id'],))],
                batches=PayrollPieceRepository(self.db,self.project_id).batches(v['id']))

    def dashboard(self,year=None,month=None,contractor_id=None):
        """Aggregate frozen approved runs, with a separate current-source audit.

        Hours are taken from each run's timesheet snapshot, once per person and
        period. Piece quantities are taken once per batch, never once per share.
        A draft correction never adds a second copy of the approved fund.
        """
        from app.calculation_engine.historical import read_snapshot
        from .timesheet_repository import decimal_hours
        with read_snapshot(self.db):
            if contractor_id is not None:self.people.contractor(contractor_id)
            if year is not None and (isinstance(year,bool) or not isinstance(year,int) or not 1900<=year<=9999):raise ValidationError('Некорректный год')
            if month is not None and (isinstance(month,bool) or not isinstance(month,int) or not 1<=month<=12):raise ValidationError('Некорректный месяц')
            periods=[p for p in self.list_periods() if (year is None or p['year']==year) and (month is None or p['month']==month) and (contractor_id is None or p['contractor_id']==contractor_id)]
            selected={p['id'] for p in periods};funds=defaultdict(int);warnings=[]
            by_contractor=defaultdict(int);by_method=defaultdict(int);by_work=defaultdict(int)
            workers=defaultdict(set);hours=defaultdict(list);ambiguous=set();units=defaultdict(list);invalid_units=set();seen_batches=set();work_labels={};sources_changed=[]
            runs=self.db.execute("SELECT v.*,p.project_id,p.contractor_id,p.year,p.month,c.name AS contractor_name,r.source_json,r.source_digest "
                "FROM payroll_versions v JOIN payroll_periods p ON p.id=v.period_id JOIN contractors c ON c.id=p.contractor_id JOIN payroll_runs r ON r.id=v.current_run_id "
                "WHERE p.project_id=? AND v.status IN ('approved','closed') AND v.version_no=(SELECT MAX(x.version_no) FROM payroll_versions x WHERE x.period_id=p.id AND x.status IN ('approved','closed')) ORDER BY p.id",(self.project_id,)).fetchall()
            raw=[dict(r) for r in self.db.execute("SELECT a.*,p.id AS period_id,p.contractor_id,t.system,w.catalog_id,c.code,c.name,c.unit "
                "FROM payroll_accruals a JOIN payroll_runs r ON r.id=a.run_id JOIN payroll_versions v ON v.id=r.version_id JOIN payroll_periods p ON p.id=v.period_id "
                "LEFT JOIN payroll_terms t ON t.id=a.term_revision_id LEFT JOIN work_items w ON w.id=a.work_item_id LEFT JOIN work_catalog c ON c.id=w.catalog_id "
                "WHERE p.project_id=? AND v.status IN ('approved','closed') AND r.id=v.current_run_id "
                "AND v.version_no=(SELECT MAX(x.version_no) FROM payroll_versions x WHERE x.period_id=p.id AND x.status IN ('approved','closed'))",(self.project_id,)) if r['period_id'] in selected]
            by_period=defaultdict(list)
            for row in raw:by_period[row['period_id']].append(row)
            contractor_names={p['contractor_id']:p['contractor_name'] for p in periods}
            for run in runs:
                if run['period_id'] not in selected:continue
                current=self._compute(dict(run))
                changed=current['source_digest']!=run['source_digest'] or bool(current['errors'])
                if changed:sources_changed.append(run['period_id'])
                snapshot=json.loads(run['source_json'])['calculator'];person_currencies=defaultdict(set)
                for row in by_period[run['period_id']]:person_currencies[row['employee_id']].add(row['currency'])
                for employee,currencies in person_currencies.items():
                    if len(currencies)>1:ambiguous.update(currencies)
                    for currency in currencies:workers[currency].add(employee)
                for entry in snapshot['attendance']:
                    if entry['project_id']==self.project_id and entry['counts_as_work'] and entry['hours_nanos']>0:
                        for currency in person_currencies[entry['employee_id']]:hours[currency].append(decimal_hours(entry['hours_nanos']))
                for row in by_period[run['period_id']]:
                    currency=row['currency'];amount=row['amount_minor'];funds[currency]+=amount
                    by_contractor[(row['contractor_id'],currency)]+=amount;by_method[(row['system'] or 'manual',currency)]+=amount
                    source=json.loads(row['source_json']);dependencies=source.get('snapshot',{}).get('dependencies',{})
                    operation=(dependencies.get('operation') or {}).get('id')
                    key=(row['catalog_id'],operation,currency)
                    by_work[key]+=amount;work_labels[key]=dict(work_item_id=row['work_item_id'],code=row['code'],name=row['name'] or 'Без привязки к работе',unit=row['unit'],operation_id=operation)
                    batch=source.get('batch_id')
                    if row['component']=='piece' and batch not in seen_batches:
                        seen_batches.add(batch)
                        if not changed:units[key].append(Decimal(source['quantity']))
                        else:invalid_units.add(key)
            if any(p['status'] not in ('approved','closed') for p in periods):warnings.append('Есть неутверждённые версии. Их суммы не входят в утверждённый фонд.')
            if sources_changed:warnings.append('Изменились источники утверждённых периодов: '+', '.join(map(str,sources_changed))+'. Суммы сохранены; требуется пересмотр.')
            if ambiguous:warnings.append('У работника есть начисления в разных валютах. Стоимость часа для этих валют не определена, чтобы не использовать часы дважды.')
            metrics={currency:dict(employee_count=len(workers[currency]),average=ratio(from_minor(total),Decimal(len(workers[currency]))),
                hours=exact_sum(hours[currency]),hour_cost=ratio(from_minor(total),exact_sum(hours[currency])) if hours[currency] and currency not in ambiguous else None) for currency,total in funds.items()}
            work=[]
            for key,amount in by_work.items():
                quantity=exact_sum(units[key]) if units.get(key) and key not in invalid_units else None
                work.append(dict(work_labels[key],currency=key[2],amount=from_minor(amount),quantity=quantity,unit_cost=ratio(from_minor(amount),quantity) if quantity else None))
            return dict(funds={c:from_minor(n) for c,n in funds.items()},periods=periods,warnings=warnings,metrics=metrics,source_conflicts=sources_changed,
                by_project=[dict(project_id=self.project_id,currency=c,amount=from_minor(n)) for c,n in funds.items()],
                by_contractor=[dict(contractor_id=k[0],contractor_name=contractor_names[k[0]],currency=k[1],amount=from_minor(v)) for k,v in by_contractor.items()],
                by_method=[dict(system=k[0],currency=k[1],amount=from_minor(v)) for k,v in by_method.items()],by_work=work,
                filters=dict(year=year,month=month,contractor_id=contractor_id))
