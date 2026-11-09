"""Exact, read-only work-order control projection.

Primary journals remain the only fact/confirmation ledger. Control rows describe
where the unperformed assignment went; they never become journal quantities.
"""
from collections import defaultdict
from decimal import Decimal
import hashlib
import json

from app.calculation_engine.historical import read_snapshot
from app.decimal_utils import ZERO, exact_sum, subtract
from app.repositories.base import ValidationError
from app.time_utils import day, end_of_day, instant


POLICY = {
    'open': 'Открытые наряды: выданные наряды в статусах issued, in_progress, submitted, accepted; черновики, закрытые и отменённые исключены.',
    'remaining_lines': 'Строки с неразрешённым остатком Q − подтверждение − выданные переносы − списание; черновые резервы остаются неразрешёнными.',
    'dynamics': 'Факт и подтверждение показаны по датам первичных журналов; исходное задание — по дате смены корневой строки. Переносы не создают новое задание.',
    'available': 'Задание − max(связанный факт, связанное подтверждение) − переносы − списания; минимум 0.',
    'pending_acceptance': 'Без полного надёжного сопоставления подтверждений с фактами: Требуется сопоставление.',
    'brigades': 'При нескольких бригадах объём показан для общего состава по снимку наряда: распределение объёма между бригадами не установлено.',
    'overdue': 'Срок завершения не установлен: дата смены не является согласованным сроком.',
}


def _json(value, fallback=None):
    return json.loads(value) if value else ({} if fallback is None else fallback)


def _sum(rows, key):
    return exact_sum(row[key] for row in rows)


class WorkOrderControlProjection:
    def __init__(self, db, project_id=1):
        self.db, self.project_id = db, project_id

    def _dates(self, report_date, knowledge_cutoff):
        cutoff = instant(knowledge_cutoff)
        report = day(report_date or cutoff[:10])
        return report, cutoff, min(cutoff, end_of_day(report))

    def _exists(self, table):
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def _orders(self, order_ids, effective):
        if order_ids is None:
            order_ids = [r[0] for r in self.db.execute('SELECT id FROM work_orders WHERE project_id=? AND created_at<=?', (self.project_id,effective))]
        order_ids = list(dict.fromkeys(order_ids))
        if len(order_ids)>200:
            result={}
            for start in range(0,len(order_ids),200):
                result.update(self._orders(order_ids[start:start+200],effective))
            return result
        sql = 'SELECT o.*,c.name AS contractor_name FROM work_orders o JOIN contractors c ON c.id=o.contractor_id WHERE o.project_id=? AND o.created_at<=?'
        args = [self.project_id, effective]
        if order_ids is not None:
            order_ids = list(dict.fromkeys(order_ids))
            if not order_ids:
                return {}
            sql += ' AND o.id IN (' + ','.join('?' for _ in order_ids) + ')'
            args.extend(order_ids)
        result = {r['id']: dict(r) for r in self.db.execute(sql, args)}
        if not result:
            return result
        slots = ','.join('?' for _ in result)
        histories = self.db.execute('SELECT * FROM work_order_status_history WHERE work_order_id IN (' + slots + ') AND changed_at<=? ORDER BY changed_at,id', [*result, effective])
        for order in result.values():
            order['status'] = 'draft'
            if order['issued_at'] and order['issued_at']>effective:
                order['order_number']=None
                order['header_snapshot']=''
            header = _json(order['header_snapshot'])
            order['contractor_name'] = header.get('contractor_name', order['contractor_name'])
            order['brigades'] = []
        for h in histories:
            result[h['work_order_id']]['status'] = h['new_status']
        for row in self.db.execute('SELECT DISTINCT c.work_order_id,m.brigade_number_snapshot FROM work_order_crew c JOIN work_order_crew_members m ON m.crew_id=c.id WHERE c.work_order_id IN (' + slots + ") AND m.brigade_number_snapshot!='' ORDER BY c.work_order_id,m.brigade_number_snapshot", list(result)):
            result[row['work_order_id']]['brigades'].append(row['brigade_number_snapshot'])
        return result

    def _transfers(self, effective):
        if not self._exists('work_order_transfers'):
            return []
        records = []
        for r in self.db.execute('SELECT t.*,tl.id AS transfer_line_id,tl.source_line_id,tl.target_line_id,tl.quantity,tl.unit,so.order_number AS source_order_number,ta.order_number AS target_order_number,c.id AS cancellation_id,c.reason AS cancellation_reason,c.recorded_at AS cancelled_at FROM work_order_transfers t JOIN work_order_transfer_lines tl ON tl.transfer_id=t.id JOIN work_orders so ON so.id=t.source_order_id JOIN work_orders ta ON ta.id=t.target_order_id LEFT JOIN work_order_transfer_cancellations c ON c.transfer_id=t.id AND c.recorded_at<=? WHERE t.project_id=? AND t.created_at<=? ORDER BY t.created_at,t.id,tl.id', (effective, self.project_id, effective)):
            row = dict(r)
            row['quantity'] = Decimal(row['quantity'])
            row['active'] = row['cancellation_id'] is None
            row['date'] = row['created_at'][:10]
            row['reason'] = r['reason']
            records.append(row)
        return records

    def _project(self, order_ids=None, report_date=None, knowledge_cutoff=None, include_general=False):
        report, cutoff, effective = self._dates(report_date, knowledge_cutoff)
        orders = self._orders(order_ids, effective)
        if not orders:
            return [], [], [], []
        lines = []
        line_records=[]
        order_keys=list(orders)
        for start in range(0,len(order_keys),200):
            batch=order_keys[start:start+200]
            slots=','.join('?' for _ in batch)
            line_records.extend(self.db.execute('SELECT l.*,w.catalog_id,w.building_id,w.floor_id,c.code,c.name AS catalog_name FROM work_order_lines l JOIN work_items w ON w.id=l.work_item_id JOIN work_catalog c ON c.id=w.catalog_id WHERE l.work_order_id IN (' + slots + ') ORDER BY l.work_order_id,l.sort_order,l.id', batch).fetchall())
        for r in line_records:
            line = dict(r)
            order = orders[r['work_order_id']]
            line.update(order_id=order['id'], project_id=self.project_id, order_number=order['order_number'], status=order['status'], shift_date=order['shift_date'], contractor_id=order['contractor_id'], contractor_name=order['contractor_name'], brigades=order['brigades'], line_id=r['id'], assignment=Decimal(r['planned_quantity']), unit=r['unit_snapshot'], name=r['work_name_snapshot'] or r['catalog_name'], location=_json(r['location_snapshot']))
            lines.append(line)
        by_id = {line['line_id']: line for line in lines}
        by_item = {line['work_item_id'] for line in lines}
        events, general, signatures = {'fact': defaultdict(list), 'confirmation': defaultdict(list)}, {'fact': defaultdict(list), 'confirmation': defaultdict(list)}, []
        for kind, table, date_col in [('fact','fact_events','work_date'), ('confirmation','confirmation_events','confirmed_date')]:
            if not lines:
                continue
            if include_general:
                condition, ids = 'e.work_item_id', list(by_item)
            else:
                condition, ids = 'e.work_order_line_id', list(by_id)
            event_records=[]
            for start in range(0,len(ids),400):
                batch=ids[start:start+400]
                sql = 'SELECT e.* FROM ' + table + ' e WHERE ' + condition + ' IN (' + ','.join('?' for _ in batch) + ') AND e.' + date_col + '<=? AND e.recorded_at<=? ORDER BY e.id'
                event_records.extend(self.db.execute(sql, [*batch, report, cutoff]).fetchall())
            for r in sorted(event_records,key=lambda row:row['id']):
                row = dict(r)
                row['quantity'] = Decimal(r['quantity_delta'])
                if include_general:
                    general[kind][r['work_item_id']].append(row['quantity'])
                if r['work_order_line_id'] in by_id:
                    events[kind][r['work_order_line_id']].append(row)
                    signatures.append((table,r['id'],r['work_order_line_id'],r['quantity_delta'],r[date_col],r['recorded_at']))
        transfers = self._transfers(effective)
        transfer_orders = {r['source_order_id'] for r in transfers} | {r['target_order_id'] for r in transfers}
        statuses = self._orders(transfer_orders, effective) if transfer_orders else {}
        for t in transfers:
            target = statuses.get(t['target_order_id'], {})
            t['status'] = target.get('status', 'draft')
            # A historical draft must not acquire the number assigned later.
            t['target_order_number'] = target.get('order_number')
            if t['source_order_id'] in orders or t['target_order_id'] in orders:
                # Review depends on allocation capacity, not the child's review
                # workflow. submitted→accepted must not invalidate itself.
                capacity = 'cancelled' if not t['active'] or t['status']=='cancelled' else ('draft' if t['status']=='draft' else 'sent')
                signatures.append(('transfer', dict(t,status=capacity)))
        resolutions, matches = [], []
        if self._exists('work_order_resolutions') and lines:
            line_ids=list(by_id)
            for start in range(0,len(line_ids),400):
                batch=line_ids[start:start+400]
                line_slots = ','.join('?' for _ in batch)
                resolutions.extend(dict(r) for r in self.db.execute('SELECT * FROM work_order_resolutions WHERE project_id=? AND line_id IN (' + line_slots + ') AND recorded_at<=? ORDER BY recorded_at,id', [self.project_id,*batch,effective]))
                matches.extend(dict(r) for r in self.db.execute('SELECT * FROM work_order_volume_matches WHERE project_id=? AND line_id IN (' + line_slots + ') AND recorded_at<=? ORDER BY revision_no,id', [self.project_id,*batch,effective]))
            resolutions.sort(key=lambda r:(r['recorded_at'],r['id']))
            matches.sort(key=lambda r:(r['revision_no'],r['id']))
            signatures.extend(('resolution', r) for r in resolutions)
            signatures.extend(('match', r) for r in matches)
        for line in lines:
            lid, q = line['line_id'], line['assignment']
            facts, confirmations = events['fact'][lid], events['confirmation'][lid]
            f, c = exact_sum(r['quantity'] for r in facts), exact_sum(r['quantity'] for r in confirmations)
            outgoing = [r for r in transfers if r['source_line_id'] == lid and r['active']]
            incoming = [r for r in transfers if r['target_line_id'] == lid and r['active']]
            transferred = _sum(outgoing, 'quantity')
            reserved = _sum([r for r in outgoing if r['status'] == 'draft'], 'quantity')
            sent = _sum([r for r in outgoing if r['status'] not in ('draft','cancelled')], 'quantity')
            active_resolutions = [r for r in resolutions if r['line_id']==lid]
            revoked = {r['reverses_id'] for r in active_resolutions if r['kind']=='revoke'}
            written_off = exact_sum(Decimal(r['quantity']) for r in active_resolutions if r['kind']=='writeoff' and r['id'] not in revoked)
            conflicts = []
            if f < ZERO or c < ZERO:
                conflicts.append('Отрицательный связанный объём журнала')
            roots = {}
            for kind, records in [('fact',facts), ('confirmation',confirmations)]:
                indexed = {r['id']: r for r in records}
                grouped = defaultdict(list)
                for event in records:
                    root, seen = event, set()
                    while root['correction_of_id'] is not None:
                        if root['id'] in seen or root['correction_of_id'] not in indexed:
                            conflicts.append('Недоступен корень цепочки корректировок')
                            break
                        seen.add(root['id'])
                        root = indexed[root['correction_of_id']]
                    grouped[root['id']].append(event['quantity'])
                roots[kind] = {rid: exact_sum(values) for rid,values in grouped.items()}
                if any(v < ZERO for v in roots[kind].values()):
                    conflicts.append('Отрицательная ёмкость корня журнала')
            latest = {}
            for m in matches:
                if m['line_id']==lid:
                    latest[(m['fact_event_id'],m['confirmation_event_id'])] = m
            fact_used, confirmation_used = defaultdict(list), defaultdict(list)
            for (fid,cid), m in latest.items():
                quantity = Decimal(m['quantity'])
                if quantity == ZERO:
                    continue
                if fid not in roots['fact'] or cid not in roots['confirmation'] or quantity < ZERO:
                    conflicts.append('Сопоставление ссылается на недоступный корень связанного журнала')
                fact_used[fid].append(quantity)
                confirmation_used[cid].append(quantity)
            if any(exact_sum(qs)>roots['fact'].get(fid,ZERO) for fid,qs in fact_used.items()) or any(exact_sum(qs)>roots['confirmation'].get(cid,ZERO) for cid,qs in confirmation_used.items()):
                conflicts.append('Сопоставление превышает текущую ёмкость корня журнала')
            matched = exact_sum(exact_sum(qs) for qs in confirmation_used.values())
            matching_required = c > ZERO and f > ZERO and (any(exact_sum(confirmation_used[cid]) != quantity for cid,quantity in roots['confirmation'].items()) or bool(conflicts))
            pending = ZERO if f<=ZERO else (f if c<=ZERO else (None if matching_required else max(subtract(f,matched),ZERO)))
            excess = max(subtract(exact_sum([max(f,c),transferred,written_off]),q),ZERO)
            if excess>ZERO and (transferred>ZERO or written_off>ZERO):
                conflicts.append('Распределённый объём превышает безопасный остаток задания')
            if any(t['status']=='cancelled' for t in outgoing):
                conflicts.append('Целевой наряд отменён без отмены переноса')
            for t in incoming:
                if t['quantity']!=q or t['unit']!=line['unit']:
                    conflicts.append('Унаследованная строка не соответствует переносу')
            remaining = max(subtract(q,f),ZERO)
            unresolved = max(subtract(q,exact_sum([c,sent,written_off])),ZERO)
            open_quantity = max(subtract(q,exact_sum([c,transferred,written_off])),ZERO)
            line.update(actual=f, confirmed=c, remaining=remaining, execution_remaining=remaining, confirmed_remaining=max(subtract(q,c),ZERO), overrun=max(subtract(f,q),ZERO), confirmed_overrun=max(subtract(c,q),ZERO), transferred=transferred, reserved=reserved, sent=sent, written_off=written_off, available=max(subtract(q,exact_sum([max(f,c),transferred,written_off])),ZERO), open_quantity=open_quantity, unresolved=unresolved, closed_quantity=max(ZERO,min(q,exact_sum([c,sent,written_off]))), allocation_excess=excess, pending_acceptance=pending, matching_required=matching_required, matched_quantity=matched, incoming_quantity=_sum(incoming,'quantity'), has_actual=bool(facts), has_confirmation=bool(confirmations), conflicts=list(dict.fromkeys(conflicts)), fact_roots=roots['fact'], confirmation_roots=roots['confirmation'])
            ga = exact_sum(general['fact'][line['work_item_id']]) if include_general else f
            gc = exact_sum(general['confirmation'][line['work_item_id']]) if include_general else c
            line.update(general_actual=ga, general_confirmed=gc, general_pending=subtract(ga,gc))
            if line['conflicts']:
                key,label='conflict','Конфликт распределения'
            elif line['status']=='cancelled':
                key,label='cancelled','Наряд отменён'
            elif line['status']=='closed':
                key,label=('closed','Наряд закрыт') if unresolved==ZERO else ('review_required','Требуется повторная проверка закрытого наряда')
            elif sent>ZERO:
                key,label=('transferred','Остаток передан в выданный наряд') if unresolved==ZERO else ('partial_transferred','Остаток передан частично')
            elif reserved>ZERO:
                key,label='reserved','Зарезервировано в черновике'
            elif f>q or c>q:
                key,label='overrun','Превышение задания'
            elif matching_required:
                key,label='matching_required','Требуется сопоставление'
            elif unresolved==ZERO:
                key,label='full','Задание распределено полностью'
            elif f>ZERO or c>ZERO or transferred>ZERO or written_off>ZERO:
                key,label='partial','Частично выполнено'
            else:
                key,label='open','Не выполнено'
            line.update(state=label,state_key=key)
        return lines, transfers, signatures, [*resolutions,*matches]

    def lines(self, order_ids=None, report_date=None, knowledge_cutoff=None, include_general=False):
        with read_snapshot(self.db):
            return self._project(order_ids,report_date,knowledge_cutoff,include_general)[0]

    def metrics(self, order_id, report_date=None, knowledge_cutoff=None):
        with read_snapshot(self.db):
            if not self.db.execute('SELECT 1 FROM work_orders WHERE id=? AND project_id=?', (order_id,self.project_id)).fetchone():
                raise ValidationError('Наряд не принадлежит текущему объекту')
            lines,_,records,_ = self._project([order_id],report_date,knowledge_cutoff,True)
            signature = hashlib.sha256(json.dumps(records,ensure_ascii=False,sort_keys=True,default=str).encode('utf-8')).hexdigest()
            return lines, signature

    def report(self, order_id, report_date=None, knowledge_cutoff=None):
        with read_snapshot(self.db):
            self.metrics(order_id,report_date,knowledge_cutoff)
            all_lines,transfers,_,controls = self._project(None,report_date,knowledge_cutoff)
            connected = {order_id}
            while True:
                expanded = connected | {t[k] for t in transfers if t['source_order_id'] in connected or t['target_order_id'] in connected for k in ('source_order_id','target_order_id')}
                if expanded==connected:
                    break
                connected=expanded
            edges = [t for t in transfers if t['source_order_id'] in connected]
            nodes = [r for r in all_lines if r['order_id'] in connected]
            active_targets = {r['target_line_id'] for r in edges if r['active']}
            groups = defaultdict(list)
            for r in nodes:
                if r['status']!='cancelled':
                    groups[(r['catalog_id'],r['code'],r['unit'])].append(r)
            totals=[]
            for (catalog,code,unit),rows in groups.items():
                original = _sum([r for r in rows if r['line_id'] not in active_targets],'assignment')
                confirmed,opened,written = _sum(rows,'confirmed'),_sum(rows,'open_quantity'),_sum(rows,'written_off')
                totals.append(dict(catalog_id=catalog,code=code,unit=unit,original_quantity=original,confirmed=confirmed,open_quantity=opened,written_off=written,excess=max(subtract(exact_sum([confirmed,opened,written]),original),ZERO)))
            timeline = []
            for t in edges:
                timeline.append(dict(kind='transfer',recorded_at=t['created_at'],reason=t['reason'],transfer_id=t['id'],quantity=t['quantity']))
                if t['cancelled_at']:
                    timeline.append(dict(kind='transfer_cancelled',recorded_at=t['cancelled_at'],reason=t['cancellation_reason'],transfer_id=t['id'],quantity=t['quantity']))
            for c in controls:
                if any(r['line_id']==c['line_id'] for r in nodes):
                    timeline.append(dict(c,kind=c.get('kind','match')))
            timeline.sort(key=lambda r:(r['recorded_at'],r.get('id',0)))
            conflicts = [dict(order_id=r['order_id'],line_id=r['line_id'],code=r['code'],conflict=message) for r in nodes for message in r['conflicts']]
            return dict(lines=[r for r in nodes if r['order_id']==order_id],transfers=edges,chain_nodes=nodes,chain_edges=edges,chain_totals=totals,conflicts=conflicts,timeline=timeline,policy=POLICY)

    def _ancestor_ids(self, order_id, effective):
        ancestors = {order_id}
        transfers = self._transfers(effective)
        while True:
            expanded = ancestors | {t['source_order_id'] for t in transfers if t['active'] and t['target_order_id'] in ancestors}
            if expanded==ancestors:
                return ancestors
            ancestors=expanded

    def closure_errors(self, order_id, report_date=None, knowledge_cutoff=None):
        with read_snapshot(self.db):
            self.metrics(order_id,report_date,knowledge_cutoff)
            _,_,effective=self._dates(report_date,knowledge_cutoff)
            lines=self.lines(self._ancestor_ids(order_id,effective),report_date,knowledge_cutoff)
            errors=[message for r in lines for message in r['conflicts']]
            if any(r['order_id']==order_id and r['unresolved']>ZERO for r in lines):
                errors.append('Неподтверждённый остаток задания: оформите подтверждение, выдачу переноса или списание')
            return list(dict.fromkeys(errors))

    def assert_issue(self, order_id):
        with read_snapshot(self.db):
            self.metrics(order_id)
            _,_,effective=self._dates(None,None)
            rows=self.lines(self._ancestor_ids(order_id,effective))
            errors=[m for r in rows for m in r['conflicts']]
            if errors:
                raise ValidationError('; '.join(dict.fromkeys(errors)))
