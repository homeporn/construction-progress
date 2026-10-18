"""Document workflow and exact journal projections; no second volume ledger."""
import hashlib
import json
from decimal import Decimal

from app.calculation_engine.historical import read_snapshot
from app.decimal_utils import ZERO, exact_sum, subtract
from app.time_utils import day, end_of_day, instant
from .base import ValidationError, transaction
from .event_repository import EventRepository
from .project_repository import clean
from .work_order_repository import WorkOrderRepository, encoded


class AssignmentOverrunError(ValidationError):
    pass


class WorkOrderWorkflowRepository:
    FACT_STATUSES = ('issued', 'in_progress')
    CONFIRMATION_STATUSES = ('issued', 'in_progress', 'submitted')

    def __init__(self, db, project_id=1):
        self.db, self.project_id = db, project_id
        self.orders = WorkOrderRepository(db, project_id)
        self.events = EventRepository(db, project_id)

    def line(self, line_id):
        row = self.db.execute("SELECT l.*,o.project_id,o.order_number,o.status AS order_status,o.issued_at,o.updated_at "
                              "FROM work_order_lines l JOIN work_orders o ON o.id=l.work_order_id "
                              "WHERE l.id=? AND o.project_id=?", (line_id, self.project_id)).fetchone()
        if row is None:
            raise ValidationError('Строка наряда не принадлежит текущему объекту')
        return row

    def validate_link(self, line_id, item_id, kind, business_date, recorded_at, *, correction=False,
                      quantity=ZERO, allow_overrun=False, overrun_reason=''):
        """Caller owns BEGIN IMMEDIATE through event insertion and timeline checks."""
        line = self.line(line_id)
        if line['work_item_id'] != item_id:
            raise ValidationError('Выбранный наряд не содержит эту учётную позицию')
        allowed = ('issued', 'in_progress', 'submitted', 'accepted', 'closed', 'cancelled') if correction else (
            self.FACT_STATUSES if kind == 'fact' else self.CONFIRMATION_STATUSES)
        if not line['order_number'] or line['order_status'] not in allowed:
            raise ValidationError('Статус наряда не разрешает эту операцию')
        last_action = self.db.execute('SELECT MAX(performed_at) FROM work_order_workflow_actions WHERE work_order_id=?', (line['work_order_id'],)).fetchone()[0]
        if recorded_at < max(line['issued_at'], line['updated_at'], last_action or line['issued_at']):
            raise ValidationError('Регистрация события не может предшествовать выдаче или последнему статусу наряда')
        if kind == 'fact' and quantity > ZERO:
            # Check every affected business-date boundary, including backdated input.
            by_date = {}
            for row in self.db.execute("SELECT work_date,quantity_delta FROM fact_events WHERE work_order_line_id=? "
                                       "AND recorded_at<=? ORDER BY work_date,id", (line_id, recorded_at)):
                by_date.setdefault(row['work_date'], []).append(Decimal(row['quantity_delta']))
            by_date.setdefault(business_date, [])
            cumulative, maximum = ZERO, ZERO
            for date, values in sorted(by_date.items()):
                cumulative = exact_sum([cumulative] + values)
                if date >= business_date:
                    maximum = max(maximum, exact_sum([cumulative, quantity]))
            if maximum > Decimal(line['planned_quantity']) and (not allow_overrun or not overrun_reason.strip()):
                raise AssignmentOverrunError('Объём превысит задание строки наряда ' + line['order_number'] +
                    ': после ввода ' + format(quantity, 'f') + ' ' + line['unit_snapshot'] + ' накопительный факт достигнет ' +
                    format(maximum, 'f') + ' при задании ' + line['planned_quantity'] + ' ' + line['unit_snapshot'] +
                    ' (превышение ' + format(subtract(maximum, Decimal(line['planned_quantity'])), 'f') + '). '
                    'Подтвердите превышение и укажите причину. Это отдельная проверка сменного задания.')
        return line

    def metrics(self, order_id, report_date=None, knowledge_cutoff=None):
        with read_snapshot(self.db):
            self.orders._order(order_id)
            cutoff = instant(knowledge_cutoff)
            report_date = day(report_date or cutoff[:10])
            result, overall = [], {}
            for line in self.db.execute('SELECT * FROM work_order_lines WHERE work_order_id=? ORDER BY sort_order,id', (order_id,)):
                item_id = line['work_item_id']
                if item_id not in overall:
                    overall[item_id] = tuple(self.events.cumulative_quantities(kind, report_date, cutoff,
                        work_item_id=item_id).get(item_id, ZERO) for kind in ('fact', 'confirmation'))
                quantities = [self.events.cumulative_quantities(kind, report_date, cutoff, work_item_id=item_id,
                    work_order_line_id=line['id']) for kind in ('fact', 'confirmation')]
                actual, confirmed = [q.get(item_id, ZERO) for q in quantities]
                assignment = Decimal(line['planned_quantity'])
                result.append(dict(line_id=line['id'], work_item_id=item_id, assignment=assignment, actual=actual,
                    confirmed=confirmed, remaining=max(subtract(assignment, actual), ZERO),
                    overrun=max(subtract(actual, assignment), ZERO), general_actual=overall[item_id][0],
                    general_confirmed=overall[item_id][1], general_pending=subtract(overall[item_id][0], overall[item_id][1]),
                    has_actual=item_id in quantities[0], has_confirmation=item_id in quantities[1]))
            records = []
            for table, date in (('fact_events', 'work_date'), ('confirmation_events', 'confirmed_date')):
                records.extend((table,) + tuple(row) for row in self.db.execute(
                    'SELECT e.id,e.work_order_line_id,e.quantity_delta,e.' + date + ',e.recorded_at FROM ' + table +
                    ' e JOIN work_order_lines l ON l.id=e.work_order_line_id WHERE l.work_order_id=? AND e.' + date +
                    '<=? AND e.recorded_at<=? ORDER BY e.id', (order_id, report_date, cutoff)))
            signature = hashlib.sha256(encoded(records).encode('utf-8')).hexdigest()
            return result, signature

    def choices(self, kind):
        allowed = self.FACT_STATUSES if kind == 'fact' else self.CONFIRMATION_STATUSES
        rows = self.db.execute("SELECT l.id,l.work_item_id,l.work_order_id,l.planned_quantity,l.unit_snapshot,o.order_number,"
            "o.shift_date,o.header_snapshot,o.status FROM work_order_lines l JOIN work_orders o ON o.id=l.work_order_id "
            "JOIN work_items w ON w.id=l.work_item_id JOIN buildings b ON b.id=w.building_id "
            "WHERE o.project_id=? AND o.order_number IS NOT NULL AND b.status='active' ORDER BY o.id DESC,l.sort_order,l.id",
            (self.project_id,)).fetchall()
        cache, result = {}, []
        for row in rows:
            if row['status'] not in allowed:
                continue
            order_id = row['work_order_id']
            if order_id not in cache:
                cache[order_id] = {m['line_id']: m for m in self.metrics(order_id)[0]}
            item = dict(row)
            item.update(cache[order_id][row['id']])
            item['contractor_name'] = json.loads(row['header_snapshot'])['contractor_name']
            item['date_label'] = '.'.join(reversed(row['shift_date'].split('-')))
            result.append(item)
        return result

    def _snapshot(self, values):
        return encoded([{key: format(value, 'f') if isinstance(value, Decimal) else value for key, value in row.items()} for row in values])

    def act(self, order_id, action, employee_id, *, comment='', checks=None, event_key, performed_at=None, expected_signature=None):
        if action not in ('submitted', 'accepted', 'closed'):
            raise ValidationError('Неизвестное действие с нарядом')
        comment = clean(comment, 'Комментарий действия', 2000, False)
        event_key = clean(event_key, 'Ключ операции', 100)
        with transaction(self.db):
            self.orders.work._writable()
            order = self.orders._order(order_id)
            employee = self.orders.people.employee(employee_id, active=True)
            old = self.db.execute('SELECT * FROM work_order_workflow_actions WHERE event_key=?', (event_key,)).fetchone()
            if old:
                if (old['work_order_id'], old['action'], old['employee_id'], old['comment']) != (order_id, action, employee_id, comment):
                    raise ValidationError('Ключ операции уже использован для другого действия')
                return old['id']
            allowed = dict(submitted=('in_progress',), accepted=('submitted', 'accepted'), closed=('accepted',))
            if order['status'] not in allowed[action]:
                raise ValidationError('Недопустимый переход статуса наряда')
            now = instant(performed_at)
            last_action = self.db.execute('SELECT MAX(performed_at) FROM work_order_workflow_actions WHERE work_order_id=?', (order_id,)).fetchone()[0]
            if now < max(order['updated_at'], last_action or order['updated_at']):
                raise ValidationError('Действие не может предшествовать истории наряда')
            latest = self.db.execute('SELECT MAX(recorded_at) FROM (SELECT f.recorded_at FROM fact_events f JOIN work_order_lines l '
                'ON l.id=f.work_order_line_id WHERE l.work_order_id=? UNION ALL SELECT c.recorded_at FROM confirmation_events c '
                'JOIN work_order_lines l ON l.id=c.work_order_line_id WHERE l.work_order_id=?)', (order_id, order_id)).fetchone()[0]
            if latest and now < latest:
                raise ValidationError('Действие не может предшествовать регистрации связанных событий')
            values, signature = self.metrics(order_id, now[:10], now)
            if expected_signature is not None and signature != expected_signature:
                raise ValidationError('Связанные объёмы изменились. Обновите карточку и проверьте результаты заново')
            if action == 'submitted' and not any(row['actual'] > ZERO for row in values):
                raise ValidationError('Для сдачи требуется положительный факт, связанный с нарядом')
            if action == 'accepted':
                checks = checks or {}
                if set(checks) != {row['line_id'] for row in values} or not all(c.get('checked') is True for c in checks.values()):
                    raise ValidationError('Отметьте проверку каждой строки наряда')
            if action == 'closed':
                review = self.db.execute("SELECT event_signature FROM work_order_workflow_actions WHERE work_order_id=? "
                                         "AND action='accepted' ORDER BY performed_at DESC,id DESC LIMIT 1", (order_id,)).fetchone()
                if review is None or review['event_signature'] != signature:
                    raise ValidationError('Объёмы изменились после проверки. Повторите проверку наряда')
                if any(row['remaining'] > ZERO for row in values) and not comment:
                    raise ValidationError('Укажите комментарий о невыполненном остатке задания')
            history_id = None
            if order['status'] != action:
                self.db.execute('UPDATE work_orders SET status=?,updated_at=?,workflow_reason=?,closed_at=? WHERE id=?',
                    (action, now, comment or {'submitted': 'Сдача на проверку', 'accepted': 'Проверка результатов', 'closed': 'Закрытие наряда'}[action],
                     now if action == 'closed' else order['closed_at'], order_id))
                history_id = self.db.execute('SELECT id FROM work_order_status_history WHERE work_order_id=? ORDER BY id DESC LIMIT 1', (order_id,)).fetchone()[0]
            action_id = self.db.execute('INSERT INTO work_order_workflow_actions(work_order_id,status_history_id,action,employee_id,'
                'employee_name_snapshot,performed_at,comment,snapshot,event_signature,event_key) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (order_id, history_id, action, employee_id, employee['full_name'], now, comment, self._snapshot(values), signature, event_key)).lastrowid
            if action == 'accepted':
                for line_id, check in checks.items():
                    self.db.execute('INSERT INTO work_order_line_reviews(action_id,work_order_line_id,checked,comment) VALUES (?,?,1,?)',
                        (action_id, line_id, clean(check.get('comment', ''), 'Замечание проверяющего', 2000, False)))
            return action_id

    def enrich(self, order, report_date=None, knowledge_cutoff=None):
        cutoff = instant(knowledge_cutoff)
        report_date = day(report_date or cutoff[:10])
        effective = min(cutoff, end_of_day(report_date))
        if effective < order['created_at']:
            raise ValidationError('Наряд ещё не существовал на выбранный момент')
        order['historical'] = report_date != instant()[:10] or knowledge_cutoff is not None
        order['report_date'], order['cutoff'] = report_date, cutoff
        order['history'] = [r for r in order['history'] if r['changed_at'] <= effective]
        order['status'] = order['history'][-1]['new_status'] if order['history'] else 'draft'
        if order['status'] != 'closed':
            order['closed_at'] = None
        if order['status'] != 'cancelled':
            order['cancellation_reason'] = ''
        if order['historical'] and order['history']:
            order['updated_at'] = order['history'][-1]['changed_at']
            order['workflow_reason'] = order['history'][-1]['reason']
        order['approvals'] = [r for r in order['approvals'] if r['performed_at'] <= effective]
        if order['issued_at'] and effective < order['issued_at']:
            raise ValidationError('Наряд ещё не был выдан на выбранный момент; просмотр снимка выдачи недоступен')
        metrics, signature = self.metrics(order['id'], report_date, cutoff)
        actions = [dict(r) for r in self.db.execute('SELECT * FROM work_order_workflow_actions WHERE work_order_id=? '
            'AND performed_at<=? ORDER BY performed_at,id', (order['id'], effective))]
        for action in actions:
            action['values'] = json.loads(action['snapshot'])
        order['workflow_actions'] = actions
        order['submitted_action'] = next((a for a in reversed(actions) if a['action']=='submitted'), None)
        order['accepted_action'] = next((a for a in reversed(actions) if a['action']=='accepted'), None)
        order['event_signature'] = signature
        order['incomplete'] = any(row['remaining'] > ZERO for row in metrics)
        active_items = {row['id'] for row in self.orders.available_items()} if not order['historical'] else set()
        order['entry_item_ids'] = active_items
        review = next((a for a in reversed(actions) if a['action'] == 'accepted'), None)
        checks = {row['work_order_line_id']: dict(row) for row in self.db.execute('SELECT * FROM work_order_line_reviews WHERE action_id=?', (review['id'],))} if review else {}
        by_id = {m['line_id']: m for m in metrics}
        for line in order['lines']:
            line.update(by_id[line['id']])
            line['review'] = checks.get(line['id'])
            line['review_current'] = bool(review and review['event_signature'] == signature)
        if order['status'] in ('in_progress', 'submitted', 'accepted', 'closed') and not any(m['actual'] > ZERO for m in metrics):
            order['warnings'].append('Все связанные факты отсутствуют на дату среза или аннулированы. Статус автоматически не откатывается.')
        if any(m['confirmed'] > m['actual'] for m in metrics):
            order['warnings'].append('Связанное подтверждение превышает факт по наряду. Оно относится к общему факту работы; связь с конкретным событием выполнения не установлена.')
        if review and review['event_signature'] != signature:
            order['warnings'].append('После проверки связанные объёмы изменились. Прежнее решение сохранено; требуется повторная проверка.')
        closed = next((a for a in reversed(actions) if a['action'] == 'closed'), None)
        order['closed_snapshot'] = closed
        if closed and closed['event_signature'] != signature:
            order['warnings'].append('После закрытия зарегистрированы корректировки. Ниже показаны текущие объёмы; снимок на момент закрытия сохранён отдельно.')
        if any(m['overrun'] > ZERO for m in metrics):
            order['warnings'].append('Есть превышение сменного задания. Оно учитывается отдельно от плана ведомости.')
        return order
