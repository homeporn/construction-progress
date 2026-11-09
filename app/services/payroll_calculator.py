"""Read-only payroll projection; all inputs are returned for immutable runs.

Salary uses the explicitly entered full-month hour norm. Monthly fixed amounts
are allocated once across the person's approved project hours, by largest
remainder with project ID as the deterministic tie break.
Without approved hours a fixed amount is paid only when the employee has fixed
conditions on this project alone; multiple projects require an explicit manual
allocation. Pure piece bonuses use the final source confirmation date.
Monthly fixed amounts additionally require conditions applicable to the
settlement month; expired source-month fixed amounts need a reasoned manual
adjustment and are warned about instead of being charged again.
"""
import calendar
from collections import defaultdict
from decimal import Decimal, localcontext

from app.decimal_utils import decimal_value, exact_sum, multiply
from app.repositories.base import ValidationError
from app.repositories.labor_repository import LaborRepository
from app.repositories.payroll_rules_repository import CURRENCIES, PayrollRulesRepository
from app.repositories.timesheet_repository import decimal_hours
from app.time_utils import instant
from .payroll_money import from_minor, split_money, to_minor


def _snapshot(value):
    if isinstance(value, Decimal):
        number = format(value, 'f')
        return (number.rstrip('0').rstrip('.') if '.' in number else number) if value else '0'
    if isinstance(value, dict):
        return {str(key): _snapshot(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot(item) for item in value]
    return value


class PayrollCalculator:
    def __init__(self, db, project_id=1):
        self.db, self.project_id = db, project_id
        self.rules = PayrollRulesRepository(db, project_id)
        self.labor = LaborRepository(db, project_id)

    def _attendance(self, contractor, year, month, cutoff):
        headers = [dict(row) for row in self.db.execute(
            "SELECT t.id AS timesheet_id,t.project_id,t.contractor_id,t.year,t.month,v.id AS version_id,v.version_no,v.approved_at "
            "FROM timesheets t JOIN timesheet_versions v ON v.timesheet_id=t.id WHERE t.contractor_id=? AND t.year=? AND t.month=? "
            "AND v.status='approved' AND v.approved_at<=? AND v.version_no=(SELECT MAX(x.version_no) FROM timesheet_versions x "
            "WHERE x.timesheet_id=t.id AND x.status='approved' AND x.approved_at<=?) ORDER BY t.project_id,t.id",
            (contractor, year, month, cutoff, cutoff))]
        entries, workers = [], []
        for header in headers:
            for row in self.db.execute('SELECT * FROM timesheet_entries WHERE version_id=? ORDER BY work_date,employee_id,id', (header['version_id'],)):
                entries.append(dict(row, project_id=header['project_id'], timesheet_id=header['timesheet_id']))
            if header['project_id'] == self.project_id:
                workers.extend(dict(row) for row in self.db.execute('SELECT * FROM timesheet_workers WHERE version_id=? ORDER BY employee_id', (header['version_id'],)))
        return headers, entries, workers

    @staticmethod
    def _term(rows, employee, contractor, stamp):
        return next((row for row in rows if row['employee_id'] == employee and row['contractor_id'] == contractor
            and row['status'] == 'active' and row['valid_from'] <= stamp
            and (row['valid_to'] is None or stamp < row['valid_to'])), None)

    def _norm(self, period, version, errors, sources):
        try:
            if version.get('norm_hours') in (None, ''):
                raise ValueError()
            norm = decimal_value(version['norm_hours'])
            if norm <= 0:
                raise ValueError()
        except (ValueError, TypeError, ArithmeticError):
            errors.append('Для оклада требуется явная положительная норма часов полного месяца')
            return None
        rows = [dict(row) for row in self.db.execute(
            'SELECT p.id AS period_id,p.project_id,v.id AS version_id,v.norm_hours FROM payroll_periods p '
            'JOIN payroll_versions v ON v.period_id=p.id WHERE p.contractor_id=? AND p.year=? AND p.month=? '
            'AND v.version_no=(SELECT MAX(n.version_no) FROM payroll_versions n WHERE n.period_id=p.id) '
            'ORDER BY p.project_id,p.id', (period['contractor_id'], period['year'], period['month']))]
        sources['monthly_norms'] = rows
        for row in rows:
            if row['project_id'] != self.project_id and row['norm_hours'] not in (None, '') and decimal_value(row['norm_hours']) != norm:
                errors.append('Норма полного месяца подрядчика должна совпадать на всех объектах')
                return None
        return norm

    def _performance(self, employee, contractor, start, end, cutoff, attendance, projection):
        """Personal output is never inferred from a brigade or work order."""
        approved = [row for row in attendance if row['project_id'] == self.project_id
                    and row['employee_id'] == employee and row['counts_as_work'] and row['hours_nanos'] > 0]
        hours = {row['work_date']: decimal_hours(row['hours_nanos']) for row in approved}
        if not projection:
            projection.update(allocations=self.labor.hour_rows(cutoff, start, end),
                roots={row['id']: row for row in self.labor.fact_roots(end, cutoff)},
                attributes=self.labor.attribution_rows(cutoff), plans={})
            events = [dict(row) for row in self.db.execute(
                'SELECT f.id,f.work_item_id,f.quantity_delta,f.work_date,f.correction_of_id,f.recorded_at,f.unit '
                'FROM fact_events f JOIN work_items w ON w.id=f.work_item_id JOIN buildings b ON b.id=w.building_id '
                'WHERE b.project_id=? AND f.work_date<=? AND f.recorded_at<=? ORDER BY f.id',
                (self.project_id, end, cutoff))]
            by_id = {row['id']: row for row in events}
            root_ids = {}
            for event in events:
                current, trail = event, []
                while current['id'] not in root_ids and current['correction_of_id'] is not None:
                    trail.append(current['id'])
                    current = by_id[current['correction_of_id']]
                root_id = root_ids.get(current['id'], current['id'])
                root_ids[event['id']] = root_id
                for event_id in trail:
                    root_ids[event_id] = root_id
            projection.update(fact_events=events, fact_root_ids=root_ids)
        def plan_line(item, stamp, plan_id):
            key = item, stamp, plan_id
            if key not in projection['plans']:
                projection['plans'][key] = self.labor.plan_line(item, stamp, cutoff, plan_id)
            return projection['plans'][key]
        allocations = [row for row in projection['allocations']
                       if row['employee_id'] == employee and row['contractor_id'] == contractor and row['hours'] > 0]
        by_day = defaultdict(list)
        for row in allocations:
            by_day[row['work_date']].append(row['hours'])
        complete = bool(hours) and set(by_day) == set(hours) and all(exact_sum(by_day[stamp]) == amount for stamp, amount in hours.items())
        roots, attributes = projection['roots'], projection['attributes']
        personal = [row for row in attributes if row['employee_id'] == employee and row['contractor_id'] == contractor
                    and row['quantity'] > 0 and row['fact_event_id'] in roots
                    and start <= roots[row['fact_event_id']]['work_date'] <= end]
        relevant_roots = {row['fact_event_id'] for row in personal}
        related = [row for row in attributes if row['fact_event_id'] in relevant_roots and row['quantity'] > 0]
        allocated_workdays = {(row['work_item_id'], row['work_date']) for row in allocations}
        attributed_workdays = {(roots[row['fact_event_id']]['work_item_id'], roots[row['fact_event_id']]['work_date']) for row in personal}
        complete = complete and bool(personal) and allocated_workdays == attributed_workdays
        for root_id in relevant_roots:
            assignments = [row for row in related if row['fact_event_id'] == root_id]
            complete = complete and all(row['employee_id'] is not None for row in assignments)
            complete = complete and exact_sum(row['quantity'] for row in assignments) == roots[root_id]['available']
        earned, norms = [], []
        for row in personal:
            root = roots[row['fact_event_id']]
            plan = plan_line(root['work_item_id'], root['work_date'], row['plan_version_id'])
            if plan is None or plan['labor_norm'] is None or plan['labor_norm'] <= 0:
                complete = False
            else:
                earned.append(multiply(row['quantity'], plan['labor_norm']))
                norms.append(plan)
        # A stale/invalid allocation plan also prevents a certified bonus.
        for row in allocations:
            if plan_line(row['work_item_id'], row['work_date'], row['plan_version_id']) is None:
                complete = False
        worked = exact_sum(hours.values())
        with localcontext() as context:
            context.prec = 80
            ratio = exact_sum(earned) / worked if complete and worked > 0 else None
        source = dict(employee_id=employee, complete=bool(complete), approved_hours=worked,
            allocated_hours=exact_sum(row['hours'] for row in allocations), allocations=allocations,
            attributions=related, fact_roots=[roots[key] for key in sorted(relevant_roots)], plan_lines=norms,
            fact_events=[row for row in projection['fact_events'] if projection['fact_root_ids'][row['id']] in relevant_roots],
            normative_hours=exact_sum(earned) if complete else None, ratio=ratio)
        return ratio, _snapshot(source)

    def calculate(self, period, version, cutoff=None, piece_lines=None, adjustments=None):
        if period.get('project_id', self.project_id) != self.project_id:
            raise ValidationError('Расчётный период другого объекта')
        contractor, year, month = period['contractor_id'], period['year'], period['month']
        self.rules.people.contractor(contractor)
        start, end = '{:04d}-{:02d}-01'.format(year, month), '{:04d}-{:02d}-{:02d}'.format(year, month, calendar.monthrange(year, month)[1])
        known = instant(cutoff)
        headers, attendance, roster = self._attendance(contractor, year, month, known)
        current = [row for row in attendance if row['project_id'] == self.project_id]
        known_terms = [row for row in self.rules.list_terms(known) if row['contractor_id'] == contractor]
        terms = [row for row in known_terms if row['valid_from'] <= end
                 and (row['valid_to'] is None or row['valid_to'] > start)]
        errors, warnings, lines = [], [], []
        sources = dict(period=dict(project_id=self.project_id, contractor_id=contractor, year=year, month=month),
            norm_hours=version.get('norm_hours'), timesheets=headers, attendance=attendance, workers=roster,
            terms=terms, bonus_terms=[], piece_lines=list(piece_lines or []), adjustments=list(adjustments or []), performance=[])
        needs_time = any(row['status'] == 'active' and (row['base_kind'] in ('hourly', 'salary') or row['bonus_kind'] == 'performance') for row in terms)
        if not any(row['project_id'] == self.project_id for row in headers) and (needs_time or not (piece_lines or adjustments)):
            errors.append('Нет утверждённого табеля за расчётный месяц на выбранный момент знания')
        positive = [row for row in current if row['counts_as_work'] and row['hours_nanos'] > 0]
        if current and not positive:
            warnings.append('В утверждённом табеле нет положительных отработанных часов')
        by_employee, groups = defaultdict(list), defaultdict(list)
        selected_terms = {}
        for entry in positive:
            employee = entry['employee_id']
            by_employee[employee].append(entry)
            term = self._term(terms, employee, contractor, entry['work_date'])
            if term is None:
                errors.append('Работник {}: отсутствуют действующие условия оплаты на {}'.format(employee, entry['work_date']))
                continue
            selected_terms[term['id']] = term
            if term['base_kind'] in ('hourly', 'salary'):
                groups[(employee, term['id'])].append(entry)
        norm = self._norm(period, version, errors, sources) if any(term['base_kind'] == 'salary' for term in selected_terms.values()) else None
        for (employee, term_id), entries in sorted(groups.items()):
            term = selected_terms[term_id]
            hours = exact_sum(decimal_hours(entry['hours_nanos']) for entry in entries)
            kind = term['base_kind']
            amount = term['hourly_rate'] if kind == 'hourly' else term['salary']
            if amount is None:
                errors.append('Работник {}: отсутствует ставка или оклад'.format(employee))
                continue
            if kind == 'salary' and norm is None:
                continue
            with localcontext() as context:
                context.prec = 80
                money = multiply(decimal_value(amount), hours)
                if kind == 'salary':
                    money /= norm
            lines.append(dict(employee_id=employee, component='base_' + kind, amount_minor=to_minor(money),
                currency=term['currency'], term_revision_id=term_id, source=_snapshot(dict(term=term,
                    hours=hours, monthly_norm=norm if kind == 'salary' else None, entries=entries)), comment=''))
        for item in piece_lines or []:
            line = dict(item)
            if not isinstance(line.get('amount_minor'), int) or isinstance(line.get('amount_minor'), bool) or line.get('currency') not in CURRENCIES:
                errors.append('Некорректная сдельная строка начисления')
                continue
            line['component'] = 'piece'
            line.setdefault('source', {})
            line.setdefault('comment', '')
            lines.append(line)
        global_hours = defaultdict(lambda: defaultdict(list))
        for entry in attendance:
            if entry['counts_as_work'] and entry['hours_nanos'] > 0:
                global_hours[entry['employee_id']][entry['project_id']].append(decimal_hours(entry['hours_nanos']))
        bonus_dates = {employee: max(entry['work_date'] for entry in entries) for employee, entries in by_employee.items()}
        piece_dates = defaultdict(list)
        for line in lines:
            if line['component'] == 'piece':
                root = line['source'].get('snapshot', {}).get('dependencies', {}).get('root', {})
                piece_dates[line['employee_id']].append(root.get('confirmed_date') or end)
        for employee, dates in piece_dates.items():
            bonus_dates[employee] = max([bonus_dates[employee]] + dates) if employee in bonus_dates else max(dates)
        performance_projection = {}  # Shared within this call; never reused after input changes.
        fixed_terms = None
        for employee, last in sorted(bonus_dates.items()):
            term = self._term(known_terms, employee, contractor, last)
            if term is None:
                errors.append('Работник {}: отсутствуют действующие условия оплаты на {}'.format(employee, last))
                continue
            sources['bonus_terms'].append(term)
            base_source = dict(term=term, last_worked_date=last)
            bonus_kind = term['bonus_kind']
            if bonus_kind in ('percent', 'performance'):
                basis = [line for line in lines if line['employee_id'] == employee
                         and ((term['bonus_basis'] == 'base' and line['component'] in ('base_hourly', 'base_salary'))
                              or (term['bonus_basis'] == 'piece' and line['component'] == 'piece'))]
                if any(line['currency'] != term['currency'] for line in basis):
                    errors.append('Работник {}: валюта базы процентной премии не совпадает с условиями'.format(employee))
                    continue
                can_bonus = True
                if bonus_kind == 'performance':
                    ratio, source = self._performance(employee, contractor, start, end, known, attendance, performance_projection)
                    sources['performance'].append(source)
                    base_source['performance'] = source
                    can_bonus = ratio is not None and ratio >= decimal_value(term['performance_threshold'])
                    if ratio is None:
                        warnings.append('Работник {}: премия за выработку исключена: полная персональная атрибуция и распределение часов не подтверждены'.format(employee))
                if can_bonus and basis:
                    basis_minor = sum(line['amount_minor'] for line in basis)
                    with localcontext() as context:
                        context.prec = 80
                        amount = to_minor(multiply(from_minor(basis_minor), decimal_value(term['bonus_value'])) / Decimal(100))
                    if amount:
                        lines.append(dict(employee_id=employee, component='bonus', amount_minor=amount,
                            currency=term['currency'], term_revision_id=term['id'], source=_snapshot(dict(base_source,
                            bonus_basis=term['bonus_basis'], basis_minor=basis_minor, bonus_value=term['bonus_value'])), comment=''))
            weights = {project: exact_sum(values) for project, values in global_hours[employee].items()}
            for component, value in [('bonus', term['bonus_value'] if bonus_kind == 'fixed' else '0'), ('supplement', term['fixed_supplement'])]:
                total = to_minor(value)
                if total:
                    if term['valid_from'] > end or (term['valid_to'] is not None and term['valid_to'] <= start):
                        label = 'премия' if component == 'bonus' else 'доплата'
                        warnings.append('Работник {}: фиксированная {} исходных условий не действует в расчётном месяце; '
                            'для отдельного начисления добавьте ручную премию или доплату с обоснованием'.format(employee, label))
                        continue
                    allocation_basis = 'approved_project_hours'
                    if weights:
                        shares = split_money(total, weights)
                    else:
                        if fixed_terms is None:
                            fixed_terms = [dict(row) for row in self.db.execute(
                                'SELECT v.*,r.project_id,r.employee_id,r.contractor_id FROM payroll_terms v '
                                'JOIN payroll_term_roots r ON r.id=v.root_id WHERE r.contractor_id=? '
                                "AND v.status='active' AND v.valid_from<=? AND (v.valid_to IS NULL OR v.valid_to>?) "
                                'AND v.recorded_at<=? AND NOT EXISTS(SELECT 1 FROM payroll_terms n WHERE n.root_id=v.root_id '
                                'AND n.revision_no>v.revision_no AND n.recorded_at<=?) ORDER BY r.project_id,v.root_id',
                                (contractor, end, start, known, known))]
                            sources['fixed_amount_terms'] = fixed_terms
                        fixed_projects = {row['project_id'] for row in fixed_terms if row['employee_id'] == employee
                            and (decimal_value(row['fixed_supplement']) > 0 or (row['bonus_kind'] == 'fixed' and decimal_value(row['bonus_value']) > 0))}
                        if fixed_projects != {self.project_id}:
                            errors.append('Работник {}: фиксированные суммы на нескольких объектах без утверждённых часов требуют явного ручного распределения'.format(employee))
                            continue
                        shares = {self.project_id: total}
                        allocation_basis = 'sole_fixed_condition_project'
                    amount = shares.get(self.project_id, 0)
                    if amount:
                        lines.append(dict(employee_id=employee, component=component, amount_minor=amount,
                            currency=term['currency'], term_revision_id=term['id'], source=_snapshot(dict(base_source,
                            monthly_total_minor=total, allocation_basis=allocation_basis,
                            approved_project_hours=weights, project_shares_minor=shares)), comment=''))
        for adjustment in adjustments or []:
            if not isinstance(adjustment.get('amount_minor'), int) or isinstance(adjustment.get('amount_minor'), bool) or adjustment.get('currency') not in CURRENCIES:
                errors.append('Некорректная ручная корректировка начисления')
                continue
            lines.append(dict(employee_id=adjustment['employee_id'], component=adjustment.get('kind', 'adjustment'),
                amount_minor=adjustment['amount_minor'], currency=adjustment['currency'], source=_snapshot(dict(adjustment)),
                comment=adjustment.get('reason', '')))
        lines = sorted(lines, key=lambda row: (row['employee_id'], row['currency'], row['component'], row.get('term_revision_id') or 0,
            row.get('rate_revision_id') or 0, row.get('work_item_id') or 0, str(row['source'])))
        totals, workers = defaultdict(int), {}
        names = {row['employee_id']: row['full_name'] for row in roster}
        for line in lines:
            if line.get('full_name'):
                names.setdefault(line['employee_id'], line['full_name'])
        for line in lines:
            totals[line['currency']] += line['amount_minor']
            key = line['employee_id'], line['currency']
            worker = workers.setdefault(key, dict(employee_id=key[0], full_name=names.get(key[0], str(key[0])),
                currency=key[1], total_minor=0))
            worker[line['component']] = worker.get(line['component'], 0) + line['amount_minor']
            worker['total_minor'] += line['amount_minor']
        return dict(lines=lines, errors=list(dict.fromkeys(errors)), warnings=list(dict.fromkeys(warnings)),
            sources=_snapshot(sources), totals=dict(sorted(totals.items())), workers=list(workers.values()))
