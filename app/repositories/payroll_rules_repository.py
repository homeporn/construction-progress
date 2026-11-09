"""Append-only pay conditions and prices with half-open business intervals.

Within each exact scope active intervals cannot overlap. A contractor-specific
price takes precedence over a project-wide price. Technological operations use
only an explicit operation price: a whole-work price is never its fallback.
"""
from decimal import Decimal

from app.decimal_utils import canonical, decimal_value
from app.services.payroll_money import to_minor
from app.time_utils import day, instant
from .base import ValidationError, transaction
from .payroll_base import PayrollBase
from .project_repository import clean


CURRENCIES = {'RUB', 'USD', 'EUR'}


def _decimal(value, label, *, positive=False):
    try:
        if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
            raise ValueError()
        number = decimal_value(value)
        if number < 0 or (positive and number == 0) or abs(to_minor(number)) > 9000000000000000:
            raise ValueError()
        return canonical(number)
    except (ValueError, TypeError, ArithmeticError):
        raise ValidationError(label + ': укажите допустимое неотрицательное десятичное число') from None


def _dates(data):
    try:
        start, end = day(data.get('valid_from')), day(data['valid_to']) if data.get('valid_to') else None
        if end is not None and end <= start:
            raise ValueError()
        return start, end
    except (ValueError, TypeError):
        raise ValidationError('Укажите корректный полуоткрытый период действия [начало, окончание)') from None


def _currency(data):
    value = data.get('currency', 'RUB')
    if value not in CURRENCIES:
        raise ValidationError('Поддерживаются валюты RUB, USD и EUR')
    return value


class PayrollRulesRepository(PayrollBase):
    def _latest(self, table, roots, cutoff=None):
        known = instant(cutoff)
        extra = (',e.full_name,c.name AS contractor_name' if table == 'payroll_terms'
                 else ',c.name AS contractor_name,w.code,w.name')
        joins = (' JOIN employees e ON e.id=r.employee_id JOIN contractors c ON c.id=r.contractor_id'
                 if table == 'payroll_terms' else
                 ' LEFT JOIN contractors c ON c.id=r.contractor_id JOIN work_catalog w ON w.id=r.catalog_id')
        return [dict(row) for row in self.db.execute(
            'SELECT v.*,r.project_id,' + ('r.employee_id,r.contractor_id' if table == 'payroll_terms' else
            'r.contractor_id,r.catalog_id,r.operation_id,r.unit') + extra + ' FROM ' + table + ' v JOIN ' + roots +
            ' r ON r.id=v.root_id' + joins + ' WHERE r.project_id=? AND v.recorded_at<=? AND NOT EXISTS('
            'SELECT 1 FROM ' + table + ' n WHERE n.root_id=v.root_id AND n.revision_no>v.revision_no '
            'AND n.recorded_at<=?) ORDER BY v.root_id', (self.project_id, known, known))]

    def list_terms(self, cutoff=None):
        return self._latest('payroll_terms', 'payroll_term_roots', cutoff)

    def list_rates(self, cutoff=None):
        return self._latest('payroll_rates', 'payroll_rate_roots', cutoff)

    def terms_on(self, employee_id, contractor_id, date, cutoff=None):
        stamp = day(date)
        return next((row for row in self.list_terms(cutoff) if row['employee_id'] == employee_id
            and row['contractor_id'] == contractor_id and row['status'] == 'active'
            and row['valid_from'] <= stamp and (row['valid_to'] is None or stamp < row['valid_to'])), None)

    def rate_on(self, catalog_id, contractor_id, date, operation_id=None, cutoff=None):
        stamp = day(date)
        matches = [row for row in self.list_rates(cutoff) if row['catalog_id'] == catalog_id
            and row['operation_id'] == operation_id and row['contractor_id'] in (None, contractor_id)
            and row['status'] == 'active' and row['valid_from'] <= stamp
            and (row['valid_to'] is None or stamp < row['valid_to'])]
        return sorted(matches, key=lambda row: (row['contractor_id'] is None, row['root_id']))[0] if matches else None

    @staticmethod
    def _overlap(rows, candidate, identity, root_id):
        if candidate['status'] != 'active':
            return
        for row in rows:
            if row['root_id'] == root_id or row['status'] != 'active' or any(row[field] != candidate[field] for field in identity):
                continue
            if candidate['valid_from'] < (row['valid_to'] or '9999-12-32') and row['valid_from'] < (candidate['valid_to'] or '9999-12-32'):
                raise ValidationError('Активные периоды условий или расценок пересекаются')

    @staticmethod
    def _existing(rows, root_id, expected_revision):
        row = next((row for row in rows if row['root_id'] == root_id), None)
        if row is None:
            raise ValidationError('Условие или расценка не принадлежит текущему объекту')
        if expected_revision is not None and row['revision_no'] != int(expected_revision):
            raise ValidationError('Данные изменились после открытия формы. Обновите страницу')
        return row

    def _terms(self, data):
        start, end = _dates(data)
        system = data.get('system', 'hourly')
        if system not in ('hourly', 'salary', 'piece', 'combined'):
            raise ValidationError('Неизвестная система оплаты')
        base = data.get('base_kind') if system == 'combined' else {'hourly': 'hourly', 'salary': 'salary', 'piece': 'none'}[system]
        if base not in ('hourly', 'salary', 'none'):
            raise ValidationError('Комбинированная оплата требует явного основания: часы, оклад или без основания')
        bonus = data.get('bonus_kind', 'none')
        basis = data.get('bonus_basis')
        if bonus not in ('none', 'fixed', 'percent', 'performance'):
            raise ValidationError('Неизвестный вид премии')
        if bonus in ('percent', 'performance') and basis not in ('base', 'piece'):
            raise ValidationError('Укажите явную базу процентной премии: основная или сдельная часть')
        piece = data.get('piece_enabled', system == 'piece')
        if piece not in (False, True, 0, 1, '0', '1'):
            raise ValidationError('Некорректный признак сдельной оплаты')
        piece = int(piece)
        if system == 'piece':
            piece = 1
        elif system != 'combined' and piece:
            raise ValidationError('Сдельная часть доступна для сдельной или комбинированной системы')
        if bonus in ('percent', 'performance') and ((basis == 'piece' and not piece) or (basis == 'base' and base == 'none')):
            raise ValidationError('Указанная база премии отсутствует в условиях оплаты')
        status = data.get('status', 'active')
        if status not in ('active', 'archived'):
            raise ValidationError('Неизвестный статус условия')
        return dict(employee_id=int(data['employee_id']), contractor_id=int(data['contractor_id']), valid_from=start,
            valid_to=end, system=system, base_kind=base,
            hourly_rate=_decimal(data.get('hourly_rate'), 'Часовая ставка', positive=True) if base == 'hourly' else None,
            salary=_decimal(data.get('salary'), 'Месячный оклад', positive=True) if base == 'salary' else None,
            currency=_currency(data), piece_enabled=piece, bonus_kind=bonus, bonus_basis=basis or 'base',
            bonus_value=_decimal(data.get('bonus_value', '0'), 'Премия'),
            performance_threshold=_decimal(data.get('performance_threshold', '1'), 'Порог выработки', positive=True),
            fixed_supplement=_decimal(data.get('fixed_supplement', '0'), 'Доплата'), status=status,
            comment=clean(data.get('comment', ''), 'Комментарий', 2000, False))

    def save_terms(self, data, term_id=None, expected_revision=None, operation_key=None):
        payload = dict(data=data, term_id=term_id, expected_revision=expected_revision)
        with transaction(self.db):
            found, result = self.replay(operation_key, 'terms', payload)
            if found:
                return result
            self.scope()
            values = self._terms(data)
            rows = self.list_terms()
            old = self._existing(rows, term_id, expected_revision) if term_id is not None else None
            self.people.contractor(values['contractor_id'])
            employee = self.people.employee(values['employee_id'])
            if old is None and employee['contractor_id'] != values['contractor_id']:
                raise ValidationError('Работник относится к другому подрядчику')
            if old and any(values[key] != old[key] for key in ('employee_id', 'contractor_id')):
                raise ValidationError('Работник и подрядчик существующего условия неизменяемы')
            self._overlap(rows, values, ('employee_id', 'contractor_id'), term_id)
            now = instant()
            if old is None:
                term_id = self.db.execute('INSERT INTO payroll_term_roots(project_id,employee_id,contractor_id,created_at) VALUES (?,?,?,?)',
                    (self.project_id, values['employee_id'], values['contractor_id'], now)).lastrowid
            fields = [key for key in values if key not in ('employee_id', 'contractor_id')]
            columns = ['root_id', 'revision_no', 'previous_id'] + fields + ['recorded_at']
            self.db.execute('INSERT INTO payroll_terms(' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                [term_id, old['revision_no'] + 1 if old else 1, old['id'] if old else None] + [values[key] for key in fields] + [now])
            self.remember(operation_key, 'terms', payload, term_id)
            return term_id

    def _catalog(self, catalog_id):
        row = self.db.execute('SELECT * FROM work_catalog WHERE id=?', (catalog_id,)).fetchone()
        if row is None:
            raise ValidationError('Код работы не найден')
        return dict(row)

    def save_rate(self, data, rate_id=None, expected_revision=None, operation_key=None):
        payload = dict(data=data, rate_id=rate_id, expected_revision=expected_revision)
        with transaction(self.db):
            found, result = self.replay(operation_key, 'rate', payload)
            if found:
                return result
            self.scope()
            start, end = _dates(data)
            catalog = self._catalog(int(data['catalog_id']))
            contractor = int(data['contractor_id']) if data.get('contractor_id') not in (None, '') else None
            operation = int(data['operation_id']) if data.get('operation_id') not in (None, '') else None
            if contractor is not None:
                self.people.contractor(contractor)
            if operation is not None and not any(row['id'] == operation and row['catalog_id'] == catalog['id'] for row in self.list_operations()):
                raise ValidationError('Технологическая операция не соответствует коду работы или объекту')
            values = dict(catalog_id=catalog['id'], contractor_id=contractor, operation_id=operation,
                unit=clean(data.get('unit') or catalog['unit'], 'Единица измерения', 80),
                valid_from=start, valid_to=end, amount=_decimal(data.get('amount'), 'Расценка', positive=True),
                currency=_currency(data), status=data.get('status', 'active'),
                comment=clean(data.get('comment', ''), 'Комментарий', 2000, False))
            if values['status'] not in ('active', 'archived'):
                raise ValidationError('Неизвестный статус расценки')
            if values['unit'] != catalog['unit']:
                raise ValidationError('Единица расценки не соответствует справочнику работы')
            rows = self.list_rates()
            old = self._existing(rows, rate_id, expected_revision) if rate_id is not None else None
            identity = ('catalog_id', 'contractor_id', 'operation_id', 'unit')
            if old and any(values[key] != old[key] for key in identity):
                raise ValidationError('Контекст существующей расценки неизменяем')
            self._overlap(rows, values, identity, rate_id)
            now = instant()
            if old is None:
                rate_id = self.db.execute('INSERT INTO payroll_rate_roots(project_id,contractor_id,catalog_id,operation_id,unit,created_at) VALUES (?,?,?,?,?,?)',
                    (self.project_id, contractor, catalog['id'], operation, values['unit'], now)).lastrowid
            fields = ['valid_from', 'valid_to', 'amount', 'currency', 'status', 'comment']
            self.db.execute('INSERT INTO payroll_rates(root_id,revision_no,previous_id,' + ','.join(fields) + ',recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                [rate_id, old['revision_no'] + 1 if old else 1, old['id'] if old else None] + [values[key] for key in fields] + [now])
            self.remember(operation_key, 'rate', payload, rate_id)
            return rate_id

    def list_operations(self):
        return [dict(row) for row in self.db.execute('SELECT o.*,w.code AS catalog_code,w.name AS catalog_name,w.unit FROM payroll_tech_operations o '
            'JOIN work_catalog w ON w.id=o.catalog_id WHERE o.project_id=? ORDER BY o.catalog_id,o.code,o.id', (self.project_id,))]

    def save_operation(self, catalog_id, code, name, operation_id=None, is_active=True):
        with transaction(self.db):
            self.scope()
            self._catalog(catalog_id)
            code, name = clean(code, 'Код операции', 80), clean(name, 'Название операции', 240)
            if is_active not in (True, False, 0, 1):
                raise ValidationError('Некорректный статус операции')
            now = instant()
            if operation_id is None:
                return self.db.execute('INSERT INTO payroll_tech_operations(project_id,catalog_id,code,name,is_active,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                    (self.project_id, catalog_id, code, name, int(is_active), now, now)).lastrowid
            old = next((row for row in self.list_operations() if row['id'] == operation_id), None)
            if old is None or old['catalog_id'] != catalog_id:
                raise ValidationError('Операция не принадлежит объекту или коду работы')
            self.db.execute('UPDATE payroll_tech_operations SET code=?,name=?,is_active=?,updated_at=? WHERE id=?',
                (code, name, int(is_active), now, operation_id))
            return operation_id
