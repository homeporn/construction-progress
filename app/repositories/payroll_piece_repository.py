"""Explicit confirmed-volume claims and auditable piecework distributions.

Physical journals are read only. A preview fixes the exact input dependencies;
confirmation checks them again while holding the SQLite writer lock.
"""
from collections import defaultdict
from decimal import Decimal, DecimalException
import json
from uuid import uuid4

from app.decimal_utils import canonical, exact_sum, multiply, subtract
from app.time_utils import instant
from .base import ValidationError, transaction
from .brigade_repository import BrigadeRepository
from .labor_repository import LaborRepository
from .payroll_base import PayrollBase, encode, fingerprint
from app.services.payroll_money import to_minor, split_money
from .payroll_rules_repository import PayrollRulesRepository
from .project_repository import clean


def _number(value):
    try:
        return canonical(value)
    except (DecimalException, TypeError, ValueError) as exc:
        raise ValidationError('Укажите конечное десятичное число с допустимой точностью') from exc


class PayrollPieceRepository(PayrollBase):
    def _root(self, event_id, cutoff=None):
        known = instant(cutoff)
        row = self.db.execute(
            'SELECT c.*,w.catalog_id,k.code,k.name,k.unit AS catalog_unit FROM confirmation_events c '
            'JOIN work_items w ON w.id=c.work_item_id JOIN work_catalog k ON k.id=w.catalog_id '
            'JOIN buildings b ON b.id=w.building_id WHERE c.id=? AND b.project_id=? '
            'AND c.correction_of_id IS NULL AND c.recorded_at<=?',
            (event_id, self.project_id, known)).fetchone()
        if row is None:
            raise ValidationError('Нужно исходное независимое подтверждение текущего объекта')
        family = [dict(r) for r in self.db.execute(
            'WITH RECURSIVE family AS (SELECT * FROM confirmation_events WHERE id=? UNION ALL '
            'SELECT c.* FROM confirmation_events c JOIN family p ON c.correction_of_id=p.id) '
            'SELECT * FROM family WHERE recorded_at<=? ORDER BY id', (event_id, known))]
        return dict(row, available=exact_sum(Decimal(r['quantity_delta']) for r in family), family=family)

    def _claims(self, cutoff=None):
        known = instant(cutoff)
        return [dict(r, quantity=Decimal(r['quantity'])) for r in self.db.execute(
            'SELECT c.*,c.id AS root_id,c.id AS claim_id,r.id AS revision_id,r.revision_no,r.previous_id,'
            'r.quantity,r.proof_ref,r.reason,r.recorded_at FROM payroll_claims c '
            'JOIN payroll_claim_revisions r ON r.claim_id=c.id WHERE c.project_id=? AND r.recorded_at<=? '
            'AND NOT EXISTS(SELECT 1 FROM payroll_claim_revisions n WHERE n.claim_id=c.id '
            'AND n.revision_no>r.revision_no AND n.recorded_at<=?) ORDER BY c.id',
            (self.project_id, known, known))]

    def _claim(self, claim_id, cutoff=None):
        row = next((r for r in self._claims(cutoff) if r['claim_id'] == claim_id), None)
        if row is None:
            raise ValidationError('Назначение подтверждённого объёма не принадлежит объекту или недоступно')
        return row

    def _check_claim_set(self, root, claims, cutoff=None):
        positive = [r for r in claims if r['quantity'] > 0]
        source_bank, _ = self._bank(cutoff)
        modes = {r['operation_id'] for r in positive}
        modes.update(operation for (event, operation), quantity in source_bank.items()
                     if event == root['id'] and quantity > 0)
        if None in modes and any(operation is not None for operation in modes):
            raise ValidationError('Полная расценка и технологические операции одного подтверждения несовместимы')
        totals = defaultdict(list)
        for row in positive:
            totals[row['operation_id']].append(row['quantity'])
        if any(exact_sum(values) > root['available'] for values in totals.values()):
            raise ValidationError('Назначенный объём превышает остаток исходного подтверждения с корректировками')

    def save_claim(self, data, claim_id=None, expected_revision=None, operation_key=None):
        payload = dict(data=data, claim_id=claim_id, expected_revision=expected_revision)
        with transaction(self.db):
            replay, result = self.replay(operation_key, 'piece_claim', payload)
            if replay:
                return result
            self.scope()
            old = self._claim(claim_id) if claim_id is not None else None
            identity = {field: data.get(field, old[field] if old else None) for field in
                ('confirmation_event_id', 'contractor_id', 'brigade_id', 'employee_id', 'operation_id')}
            if old and any(identity[k] != old[k] for k in identity):
                raise ValidationError('Источник и исполнители назначения неизменяемы; создайте отдельное назначение')
            if old and expected_revision != old['revision_no']:
                raise ValidationError('Ревизия назначения изменилась; обновите страницу')
            quantity = _number(data.get('quantity', ''))
            if Decimal(quantity) < 0 or (old is None and Decimal(quantity) == 0):
                raise ValidationError('Укажите положительный объём; нулевая ревизия снимает прежнее назначение')
            proof = clean(data.get('proof_ref', ''), 'Основание назначения', 2000)
            reason = clean(data.get('reason', ''), 'Причина изменения', 4000, bool(old))
            root = self._root(identity['confirmation_event_id'])
            self.people.contractor(identity['contractor_id'], active=True)
            if identity['operation_id'] is not None:
                operation = self.db.execute('SELECT * FROM payroll_tech_operations WHERE id=? AND project_id=? '
                    'AND catalog_id=? AND is_active=1', (identity['operation_id'], self.project_id, root['catalog_id'])).fetchone()
                if operation is None:
                    raise ValidationError('Технологическая операция не принадлежит подтверждённой работе')
            if identity['brigade_id'] is not None:
                brigade = BrigadeRepository(self.db, self.project_id).get(identity['brigade_id'])
                if brigade['contractor_id'] != identity['contractor_id']:
                    raise ValidationError('Бригада не относится к выбранному подрядчику')
            if identity['employee_id'] is not None and Decimal(quantity) > 0:
                self._participant(identity['employee_id'], identity, root['confirmed_date'], instant())
            related = [r for r in self._claims() if r['confirmation_event_id'] == root['id'] and r['claim_id'] != claim_id]
            self._check_claim_set(root, related + [dict(identity, quantity=Decimal(quantity))])
            now = instant()
            if old is None:
                claim_id = self.db.execute('INSERT INTO payroll_claims(project_id,confirmation_event_id,contractor_id,'
                    'brigade_id,employee_id,operation_id,created_at) VALUES (?,?,?,?,?,?,?)',
                    (self.project_id, identity['confirmation_event_id'], identity['contractor_id'], identity['brigade_id'],
                     identity['employee_id'], identity['operation_id'], now)).lastrowid
            self.db.execute('INSERT INTO payroll_claim_revisions(claim_id,revision_no,previous_id,quantity,proof_ref,reason,recorded_at) '
                'VALUES (?,?,?,?,?,?,?)', (claim_id, old['revision_no'] + 1 if old else 1,
                old['revision_id'] if old else None, quantity, proof, reason, now))
            return self.remember(operation_key, 'piece_claim', payload, claim_id)

    def _selected_versions(self, cutoff=None):
        known = instant(cutoff)
        rows = [dict(r) for r in self.db.execute('SELECT v.*,p.project_id FROM payroll_versions v '
            'JOIN payroll_periods p ON p.id=v.period_id WHERE p.project_id=? AND v.created_at<=? ORDER BY v.version_no',
            (self.project_id, known))]
        selected = {}
        for row in rows:
            # The approved baseline and its open replacement each reserve their
            # full quantity, but their shared period reserves only the maximum.
            approved = row['status'] in ('approved', 'closed') and (row['approved_at'] is None or row['approved_at'] <= known)
            kind = 'approved' if approved else 'current'
            selected[(row['period_id'], kind)] = row
        return {r['id']: r for r in selected.values()}

    def _bank(self, cutoff=None, candidate=None, exclude_batch_id=None, supersede_version_id=None):
        known = instant(cutoff)
        versions = self._selected_versions(known)
        if supersede_version_id is not None:
            replacement = self.require_version(supersede_version_id)
            versions = {key: value for key, value in versions.items()
                        if value['period_id'] != replacement['period_id'] or key == supersede_version_id}
        rows = [dict(r, quantity=Decimal(r['quantity'])) for r in self.db.execute(
            'SELECT b.*,c.confirmation_event_id,c.operation_id FROM payroll_piece_batches b '
            'JOIN payroll_claims c ON c.id=b.claim_id WHERE c.project_id=? AND b.created_at<=? '
            'AND (b.status=\'active\' OR b.cancelled_at>?) ORDER BY b.id', (self.project_id, known, known))
            if r['version_id'] in versions and r['id'] != exclude_batch_id]
        if candidate is not None:
            rows.append(candidate)
        per_source, per_claim = defaultdict(lambda: defaultdict(list)), defaultdict(lambda: defaultdict(list))
        for row in rows:
            if row['version_id'] not in versions:
                continue
            period = versions[row['version_id']]['period_id']
            source = (row['confirmation_event_id'], row['operation_id'])
            per_source[(source, period)][row['version_id']].append(row['quantity'])
            per_claim[(row['claim_id'], period)][row['version_id']].append(row['quantity'])
        source_totals, claim_totals = defaultdict(list), defaultdict(list)
        for (source, period), values in per_source.items():
            source_totals[source].append(max(exact_sum(v) for v in values.values()))
        for (claim, period), values in per_claim.items():
            claim_totals[claim].append(max(exact_sum(v) for v in values.values()))
        return ({key: exact_sum(values) for key, values in source_totals.items()},
                {key: exact_sum(values) for key, values in claim_totals.items()})

    def _check_reservation(self, version, claim, root, cutoff, candidate=None):
        source_key = (root['id'], claim['operation_id'])
        source_bank, claim_bank = self._bank(cutoff, candidate)
        source_total = source_bank.get(source_key, Decimal(0))
        claim_total = claim_bank.get(claim['claim_id'], Decimal(0))
        if source_total <= root['available'] and claim_total <= claim['quantity']:
            return
        if version['base_version_id'] is not None:
            # An upstream reduction can make the frozen baseline exceed the
            # bank. Its correction must be able to reduce that obligation.
            # Other periods still see max(baseline,draft) until approval.
            future_sources, future_claims = self._bank(cutoff, candidate, supersede_version_id=version['id'])
            before_sources, _ = self._bank(cutoff)
            # Reassigning the corrected quantity may legitimately introduce a
            # new claim. Only the shared physical-source reservation must not
            # grow; the replacement still has to fit both source and claim.
            does_not_grow = source_total <= before_sources.get(source_key, Decimal(0))
            fits_replacement = (future_sources.get(source_key, 0) <= root['available']
                                and future_claims.get(claim['claim_id'], 0) <= claim['quantity'])
            if does_not_grow and fits_replacement:
                return
        if source_total > root['available']:
            raise ValidationError('Подтверждённый объём уже зарезервирован в других начислениях')
        raise ValidationError('Назначенный объём уже зарезервирован в других начислениях')

    def list_claims(self, cutoff=None):
        claims = self._claims(cutoff)
        source_bank, claim_bank = self._bank(cutoff)
        result = []
        for claim in claims:
            root = self._root(claim['confirmation_event_id'], cutoff)
            assigned = exact_sum(r['quantity'] for r in claims if r['confirmation_event_id'] == root['id']
                                 and r['operation_id'] == claim['operation_id'])
            reserved = claim_bank.get(claim['claim_id'], Decimal(0))
            source_reserved = source_bank.get((root['id'], claim['operation_id']), Decimal(0))
            result.append(dict(claim, sourceavailable=root['available'], available=root['available'], assigned=assigned,
                reserved=reserved, remaining=min(subtract(claim['quantity'], reserved), subtract(root['available'], source_reserved)),
                code=root['code'], name=root['name'], unit=root['unit'], work_item_id=root['work_item_id'],
                effective_date=root['confirmed_date']))
        return result

    def _participant(self, employee_id, claim, date, cutoff):
        employee = self.people.employee(employee_id)
        if not employee['is_active'] or not self.people.available(employee_id, date, cutoff):
            raise ValidationError('Получатель недоступен на объекте на дату подтверждения')
        if employee['contractor_id'] != claim['contractor_id']:
            raise ValidationError('Получатель не относится к подрядчику назначения')
        if claim.get('employee_id') is not None and employee_id != claim['employee_id']:
            raise ValidationError('Получатель отличается от персонального назначения')
        membership = BrigadeRepository(self.db, self.project_id).current(employee_id, date, cutoff)
        if claim.get('brigade_id') is not None and (membership is None or membership['brigade_id'] != claim['brigade_id']):
            raise ValidationError('Получатель не состоял в указанной бригаде на дату подтверждения')
        periods = [dict(r) for r in self.db.execute('SELECT p.* FROM employee_project_periods p '
            'JOIN employee_projects e ON e.id=p.employee_project_id WHERE e.employee_id=? AND e.project_id=? '
            'AND COALESCE(p.recorded_at,\'\')<=? ORDER BY p.id', (employee_id, self.project_id, cutoff))]
        # Current names/status are included so an editable distribution must be
        # reviewed again after a personnel change. Approved history stays stored.
        return dict(employee=dict(employee), membership=dict(membership) if membership else None, project_periods=periods)

    def _build(self, version_id, request, cutoff=None, *, reserve=True):
        known = instant(cutoff)
        version = self.require_version(version_id)
        claim = self._claim(request['claim_id'], known)
        root = self._root(claim['confirmation_event_id'], known)
        if version['contractor_id'] != claim['contractor_id']:
            raise ValidationError('Подрядчик периода отличается от назначения объёма')
        month = '%04d-%02d' % (version['year'], version['month'])
        if root['confirmed_date'][:7] != month and not request['reason'].strip():
            raise ValidationError('Для начисления объёма другого месяца укажите явную причину')
        quantity = Decimal(_number(request['quantity']))
        if quantity <= 0:
            raise ValidationError('Распределяемый объём должен быть положительным')
        related = [r for r in self._claims(known) if r['confirmation_event_id'] == root['id']]
        self._check_claim_set(root, related, known)
        if quantity > claim['quantity']:
            raise ValidationError('Объём распределения превышает явное назначение исполнителю')
        rate = PayrollRulesRepository(self.db, self.project_id).rate_on(root['catalog_id'], claim['contractor_id'],
            root['confirmed_date'], claim['operation_id'], known)
        if rate is None or rate['unit'] != root['unit']:
            raise ValidationError('Нет применимой расценки в единице подтверждения на дату подтверждения')
        if claim['operation_id'] is not None:
            operation = self.db.execute('SELECT * FROM payroll_tech_operations WHERE id=? AND project_id=?',
                (claim['operation_id'], self.project_id)).fetchone()
            if operation is None or not operation['is_active']:
                raise ValidationError('Технологическая операция архивирована или недоступна')
            operation = dict(operation)
        else:
            operation = None
        total_minor = to_minor(multiply(quantity, Decimal(rate['amount'])))
        if abs(total_minor) > 9000000000000000:
            raise ValidationError('Сумма сдельного начисления превышает допустимый размер')
        recipients = request['recipients']
        if not recipients:
            raise ValidationError('Явно выберите получателей распределения')
        method = request['method']
        if method not in ('equal', 'hours', 'ktu', 'manual'):
            raise ValidationError('Неизвестный способ распределения')
        seen, participants, weights, terms = set(), [], {}, []
        labor = LaborRepository(self.db, self.project_id)
        allocated, entries = [], []
        if method == 'hours':
            allocated = [r for r in labor.hour_rows(known, root['confirmed_date'], root['confirmed_date'])
                         if r['work_item_id'] == root['work_item_id'] and r['contractor_id'] == claim['contractor_id']]
            entries = [r for r in labor.approved_entries(root['confirmed_date'], root['confirmed_date'], known)
                       if r['contractor_id'] == claim['contractor_id']]
        manual = {}
        for recipient in recipients:
            employee_id = recipient['employee_id']
            if isinstance(employee_id, bool) or not isinstance(employee_id, int) or employee_id in seen:
                raise ValidationError('Получатели должны быть уникальными работниками')
            seen.add(employee_id)
            participant = self._participant(employee_id, claim, root['confirmed_date'], known)
            term = PayrollRulesRepository(self.db, self.project_id).terms_on(employee_id, claim['contractor_id'], root['confirmed_date'], known)
            if term is None or not term['piece_enabled']:
                raise ValidationError('У получателя нет условий, разрешающих сдельную оплату на дату подтверждения')
            if term['currency'] != rate['currency']:
                raise ValidationError('Валюта условий получателя отличается от валюты расценки')
            participants.append(participant)
            terms.append(term)
            if method == 'equal':
                weight = Decimal(1)
            elif method == 'ktu':
                weight = Decimal(_number(recipient.get('weight', '')))
                if weight <= 0:
                    raise ValidationError('КТУ каждого получателя должен быть положительным')
            elif method == 'hours':
                own = [r for r in allocated if r['employee_id'] == employee_id]
                entry = next((r for r in entries if r['employee_id'] == employee_id), None)
                if entry is None or not own or any(r['timesheet_entry_id'] != entry['id'] for r in own):
                    raise ValidationError('Нужны актуальные распределённые часы4.3 по этой работе и дате подтверждения')
                weight = exact_sum(r['hours'] for r in own)
                all_hours = exact_sum(r['hours'] for r in labor.hour_rows(known, root['confirmed_date'], root['confirmed_date'])
                    if r['employee_id'] == employee_id and r['contractor_id'] == claim['contractor_id'])
                if weight <= 0 or all_hours > entry['hours']:
                    raise ValidationError('Распределённые часы получателя недействительны')
            else:
                amount = Decimal(_number(recipient.get('amount', '')))
                if amount < 0 or multiply(amount, Decimal(100)) != to_minor(amount):
                    raise ValidationError('Ручная сумма должна быть неотрицательной и иметь не более двух знаков')
                manual[employee_id] = to_minor(amount)
                weight = amount
            weights[employee_id] = weight
        if method == 'manual':
            if sum(manual.values()) != total_minor:
                raise ValidationError('Сумма ручных долей не равна полной сумме начисления')
            amounts = manual
        else:
            amounts = split_money(total_minor, weights)
        by_employee = {p['employee']['id']: p for p in participants}
        shares = [dict(employee_id=i, full_name=by_employee[i]['employee']['full_name'],
                       amount_minor=amounts[i], weight=canonical(weights[i])) for i in sorted(weights)]
        relevant_allocated = [r for r in allocated if r['employee_id'] in seen]
        relevant_entries = [r for r in entries if r['employee_id'] in seen]
        dependencies = dict(root={k: root[k] for k in ('id', 'work_item_id', 'catalog_id', 'unit', 'confirmed_date', 'family')},
            available=canonical(root['available']), claims=related, claim_revision_id=claim['revision_id'],
            rate=rate, operation=operation, participants=participants, terms=terms,
            allocated_hours=relevant_allocated, approved_entries=relevant_entries)
        # JSON roundtrip ensures Decimal snapshots compare to persisted JSON in
        # exactly the same representation after a process restart.
        dependencies = json.loads(encode(dependencies))
        digest = fingerprint(dependencies)
        if reserve:
            self._check_reservation(version, claim, root, known, dict(version_id=version_id, claim_id=claim['claim_id'],
                confirmation_event_id=root['id'], operation_id=claim['operation_id'], quantity=quantity))
        return dict(total_minor=total_minor, shares=shares, currency=rate['currency'], source=dependencies,
            snapshot_digest=digest, claim_revision_id=claim['revision_id'], rate_revision_id=rate['id'],
            work_item_id=root['work_item_id'], rate_snapshot=rate['amount'])

    def preview(self, version_id, claim_id, quantity, method, recipients, reason='', operation_key=None):
        payload = dict(version_id=version_id, claim_id=claim_id, quantity=quantity, method=method, recipients=recipients, reason=reason)
        with transaction(self.db):
            replay, result = self.replay(operation_key, 'piece_preview', payload)
            if replay:
                return result
            version = self.require_version(version_id, editable=True)
            reason = clean(reason, 'Причина распределения', 4000, False)
            request = dict(claim_id=claim_id, quantity=_number(quantity), method=method,
                           recipients=sorted(recipients, key=lambda r: r['employee_id']), reason=reason)
            built = self._build(version_id, request)
            preview_id = uuid4().hex
            basis = dict(request=request, version_revision=version['revision_no'])
            self.db.execute('INSERT INTO payroll_piece_previews(id,version_id,claim_id,claim_revision_id,rate_revision_id,'
                'quantity,total_minor,currency,method,shares_json,basis_json,snapshot_digest,source_json,created_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (preview_id, version_id, claim_id, built['claim_revision_id'],
                built['rate_revision_id'], request['quantity'], built['total_minor'], built['currency'], method,
                encode(built['shares']), encode(basis), built['snapshot_digest'], encode(built['source']), instant()))
            result = dict(id=preview_id, version_id=version_id, claim_id=claim_id, quantity=request['quantity'],
                          method=method, reason=reason, **built)
            return self.remember(operation_key, 'piece_preview', payload, result)

    def confirm(self, version_id, preview_id, operation_key=None):
        payload = dict(version_id=version_id, preview_id=preview_id)
        with transaction(self.db):
            replay, result = self.replay(operation_key, 'piece_confirm', payload)
            if replay:
                return result
            preview = self.db.execute('SELECT * FROM payroll_piece_previews WHERE id=? AND version_id=?',
                                      (preview_id, version_id)).fetchone()
            if preview is None:
                raise ValidationError('Предварительный расчёт не принадлежит версии')
            version = self.require_version(version_id)
            if preview['status'] == 'committed':
                return self.remember(operation_key, 'piece_confirm', payload, preview['result_batch_id'])
            version = self.require_version(version_id, editable=True)
            basis = json.loads(preview['basis_json'])
            if preview['status'] != 'preview' or basis['version_revision'] != version['revision_no']:
                raise ValidationError('Версия изменилась после предварительного расчёта; пересмотрите распределение')
            built = self._build(version_id, basis['request'])
            if (built['snapshot_digest'] != preview['snapshot_digest'] or built['total_minor'] != preview['total_minor']
                    or encode(built['shares']) != preview['shares_json']):
                raise ValidationError('Источники распределения изменились; сформируйте новый предварительный расчёт')
            source = dict(request=basis['request'], dependencies=built['source'], snapshot_digest=built['snapshot_digest'])
            batch_id = self.db.execute('INSERT INTO payroll_piece_batches(version_id,claim_id,claim_revision_id,rate_revision_id,'
                'quantity,rate_snapshot,amount_minor,currency,method,source_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (version_id, preview['claim_id'], built['claim_revision_id'], built['rate_revision_id'], preview['quantity'],
                 built['rate_snapshot'], built['total_minor'], built['currency'], preview['method'], encode(source), instant())).lastrowid
            for share in built['shares']:
                self.db.execute('INSERT INTO payroll_piece_shares(batch_id,employee_id,amount_minor,weight) VALUES (?,?,?,?)',
                    (batch_id, share['employee_id'], share['amount_minor'], share['weight']))
            self.db.execute("UPDATE payroll_piece_previews SET status='committed',result_batch_id=? WHERE id=?", (batch_id, preview_id))
            self.dirty(version_id, basis['request']['reason'])
            self.event(version_id, 'piece_confirmed', basis['request']['reason'], details=dict(batch_id=batch_id, preview_id=preview_id))
            return self.remember(operation_key, 'piece_confirm', payload, batch_id)

    def cancel_batch(self, version_id, batch_id, reason, operation_key=None):
        payload = dict(version_id=version_id, batch_id=batch_id, reason=reason)
        with transaction(self.db):
            replay, result = self.replay(operation_key, 'piece_cancel', payload)
            if replay:
                return result
            self.require_version(version_id, editable=True)
            reason = clean(reason, 'Причина отмены распределения', 4000)
            row = self.db.execute('SELECT * FROM payroll_piece_batches WHERE id=? AND version_id=?', (batch_id, version_id)).fetchone()
            if row is None or row['status'] != 'active':
                raise ValidationError('Активное распределение этой версии не найдено')
            self.db.execute("UPDATE payroll_piece_batches SET status='cancelled',cancelled_at=?,cancellation_reason=? WHERE id=?",
                            (instant(), reason, batch_id))
            self.dirty(version_id, reason)
            self.event(version_id, 'piece_cancelled', reason, details=dict(batch_id=batch_id))
            return self.remember(operation_key, 'piece_cancel', payload, batch_id)

    def batches(self, version_id):
        self.require_version(version_id)
        result = []
        for row in self.db.execute('SELECT * FROM payroll_piece_batches WHERE version_id=? ORDER BY id', (version_id,)):
            source = json.loads(row['source_json'])
            names = {p['employee']['id']: p['employee']['full_name'] for p in source['dependencies']['participants']}
            shares = [dict(s, full_name=names[s['employee_id']]) for s in self.db.execute(
                'SELECT * FROM payroll_piece_shares WHERE batch_id=? ORDER BY employee_id', (row['id'],))]
            result.append(dict(row, total_minor=row['amount_minor'], source=source, shares=shares))
        return result

    def validate(self, version_id, cutoff=None):
        version = self.require_version(version_id)
        known = instant(cutoff)
        errors, warnings, sources, lines = [], [], [], []
        for batch in self.batches(version_id):
            if batch['created_at'] > known or (batch['status'] == 'cancelled' and batch['cancelled_at'] <= known):
                continue
            stored = batch['source']
            record = dict(batch_id=batch['id'], claim_id=batch['claim_id'], quantity=batch['quantity'],
                amount_minor=batch['amount_minor'], currency=batch['currency'], source=stored)
            sources.append(record)
            try:
                built = self._build(version_id, stored['request'], known, reserve=False)
                record['current_snapshot_digest'] = built['snapshot_digest']
                if built['snapshot_digest'] != stored['snapshot_digest']:
                    raise ValidationError('Изменились подтверждение, назначение, расценка, условия или получатели')
                if built['total_minor'] != batch['amount_minor'] or sum(s['amount_minor'] for s in batch['shares']) != batch['amount_minor']:
                    raise ValidationError('Сумма распределения не совпадает с начислением')
                claim = self._claim(batch['claim_id'], known)
                root = self._root(claim['confirmation_event_id'], known)
                self._check_reservation(version, claim, root, known)
            except (ValidationError, ValueError) as exc:
                record['validation_error'] = str(exc)
                errors.append('Распределение #%s: %s' % (batch['id'], exc))
            dependencies = stored['dependencies']
            terms = {r['employee_id']: r for r in dependencies['terms']}
            for share in batch['shares']:
                lines.append(dict(employee_id=share['employee_id'], full_name=share['full_name'], component='piece', amount_minor=share['amount_minor'],
                    currency=batch['currency'], term_revision_id=terms[share['employee_id']]['id'],
                    rate_revision_id=batch['rate_revision_id'], work_item_id=dependencies['root']['work_item_id'],
                    source=dict(batch_id=batch['id'], claim_id=batch['claim_id'], claim_revision_id=batch['claim_revision_id'],
                                quantity=batch['quantity'], share_weight=share['weight'], snapshot=stored),
                    comment=stored['request']['reason']))
        return dict(errors=errors, warnings=warnings, sources=sources, lines=lines)

    def copy_to_version(self, old_version_id, new_version_id):
        if not self.db.in_transaction:
            raise RuntimeError('Копирование распределений требует транзакции вызывающего репозитория')
        old = self.require_version(old_version_id)
        new = self.require_version(new_version_id, editable=True)
        if old['period_id'] != new['period_id'] or new['base_version_id'] != old_version_id:
            raise ValidationError('Распределения копируются только в корректировку того же периода')
        existing = {b['source'].get('copied_from_id') for b in self.batches(new_version_id)}
        for batch in self.batches(old_version_id):
            if batch['status'] != 'active' or batch['id'] in existing:
                continue
            source = dict(batch['source'], copied_from_id=batch['id'])
            copied = self.db.execute('INSERT INTO payroll_piece_batches(version_id,claim_id,claim_revision_id,rate_revision_id,'
                'quantity,rate_snapshot,amount_minor,currency,method,source_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (new_version_id, batch['claim_id'], batch['claim_revision_id'], batch['rate_revision_id'], batch['quantity'],
                 batch['rate_snapshot'], batch['amount_minor'], batch['currency'], batch['method'], encode(source), instant())).lastrowid
            for share in batch['shares']:
                self.db.execute('INSERT INTO payroll_piece_shares(batch_id,employee_id,amount_minor,weight) VALUES (?,?,?,?)',
                    (copied, share['employee_id'], share['amount_minor'], share['weight']))
