"""Versioned monthly time records; construction quantities are never touched.

Hours are exact integer billionths in SQLite and Decimal at the boundary.
Drafts reserve time. A correction reserves max(old approved, new draft) per
person/day until approval, so reducing a draft cannot free approved hours early.
"""
import calendar
import hashlib
import json
from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.decimal_utils import canonical, exact_sum, multiply
from app.calculation_engine.historical import read_snapshot
from app.time_utils import day, instant
from .base import ValidationError, transaction
from .brigade_repository import BrigadeRepository
from .personnel_repository import PersonnelRepository
from .project_repository import clean

SCALE = 1000000000
MAX_CHANGES = 20000


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def hours_value(value):
    try:
        if isinstance(value, (float, bool)) or not isinstance(value, (str, int, Decimal)):
            raise ValueError('decimal required')
        if isinstance(value, str) and len(value) > 80:
            raise ValueError('too long')
        result = Decimal(value)
        if not result.is_finite():
            raise ValueError('finite required')
    except (ValueError, TypeError, ArithmeticError):
        raise ValidationError("Часы: требуется десятичное число с точностью до 9 знаков") from None
    if not 0 <= result <= 24:
        raise ValidationError("Часы должны быть от 0 до 24 в календарные сутки")
    if result == 0:
        return 0
    digits, exponent = result.as_tuple().digits, result.as_tuple().exponent
    trailing = 0
    for digit in reversed(digits):
        if digit != 0:
            break
        trailing += 1
    if exponent + trailing < -9:
        raise ValidationError("Часы: допускается не более 9 дробных знаков")
    return int(multiply(result, Decimal(SCALE)))


def decimal_hours(nanos):
    value = Decimal(nanos).as_tuple()
    return Decimal((value.sign, value.digits, value.exponent - 9))


class TimesheetRepository:
    def __init__(self, db, project_id=1):
        self.db, self.project_id = db, project_id
        self.people = PersonnelRepository(db, project_id)
        self.brigades = BrigadeRepository(db, project_id)

    def _writable(self):
        self.people.work._writable()

    def _header(self, sheet_id):
        row = self.db.execute(
            "SELECT t.*,p.name AS project_name,c.name AS contractor_name FROM timesheets t "
            "JOIN projects p ON p.id=t.project_id JOIN contractors c ON c.id=t.contractor_id "
            "WHERE t.id=? AND t.project_id=?", (sheet_id, self.project_id)).fetchone()
        if row is None:
            raise ValidationError("Табель не принадлежит текущему объекту")
        return dict(row)

    def _version(self, sheet_id, version_id=None):
        self._header(sheet_id)
        if version_id is None:
            row = self.db.execute("SELECT * FROM timesheet_versions WHERE timesheet_id=? "
                                  "ORDER BY (status!='cancelled') DESC,version_no DESC LIMIT 1", (sheet_id,)).fetchone()
        else:
            row = self.db.execute("SELECT * FROM timesheet_versions WHERE id=? AND timesheet_id=?", (version_id, sheet_id)).fetchone()
        if row is None:
            raise ValidationError("Версия табеля не найдена в выбранном объекте")
        return dict(row)

    def _draft(self, sheet_id, version_id=None, expected_revision=None):
        self._writable()
        v = self._version(sheet_id, version_id)
        if v['status'] != 'draft':
            raise ValidationError("Изменять можно только черновик табеля; для утверждённого создайте корректирующую версию")
        self._revision(v, expected_revision)
        return v

    @staticmethod
    def _revision(version, expected):
        if expected is not None and version['revision'] != int(expected):
            raise ValidationError("Табель изменился после открытия формы. Обновите страницу и проверьте изменения")

    @staticmethod
    def _days(header):
        return [date(header['year'], header['month'], n).isoformat()
                for n in range(1, calendar.monthrange(header['year'], header['month'])[1] + 1)]

    def codes(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM attendance_codes ORDER BY id')]

    def _event(self, version_id, operation, details):
        self.db.execute("INSERT INTO timesheet_events(version_id,operation,recorded_at,details) VALUES (?,?,?,?)",
                        (version_id, operation, instant(), _json(details)))

    def _touch(self, version_id):
        now = instant()
        self.db.execute("UPDATE timesheet_versions SET revision=revision+1,updated_at=? WHERE id=?", (now, version_id))
        self.db.execute("UPDATE timesheets SET updated_at=? WHERE id=(SELECT timesheet_id FROM timesheet_versions WHERE id=?)", (now, version_id))

    def _replay(self, key, kind, fingerprint, sheet_id=None):
        if not key:
            return None
        clean(key, 'Ключ операции', 200)
        row = self.db.execute('SELECT o.*,v.timesheet_id,t.project_id FROM timesheet_operations o '
                              'JOIN timesheet_versions v ON v.id=o.version_id JOIN timesheets t ON t.id=v.timesheet_id WHERE o.id=?', (key,)).fetchone()
        if row is None:
            return None
        if row['project_id'] != self.project_id or (sheet_id is not None and row['timesheet_id'] != sheet_id) or row['kind'] != kind or row['fingerprint'] != fingerprint:
            raise ValidationError("Ключ операции уже использован для другого запроса")
        return json.loads(row['result_json'])

    def _remember(self, key, version_id, kind, fingerprint, result):
        self.db.execute('INSERT INTO timesheet_operations(id,version_id,kind,fingerprint,result_json,created_at) VALUES (?,?,?,?,?,?)',
                        (key or uuid4().hex, version_id, kind, fingerprint, _json(result), instant()))

    def list_timesheets(self, *, history=False, contractor_id=None):
        if contractor_id is not None:
            self.people.contractor(contractor_id)
        rows = self.db.execute("SELECT t.*,c.name AS contractor_name,p.name AS project_name,v.id AS version_id,v.version_no,v.status,v.approved_at "
            "FROM timesheets t JOIN contractors c ON c.id=t.contractor_id JOIN projects p ON p.id=t.project_id "
            "JOIN timesheet_versions v ON v.timesheet_id=t.id WHERE t.project_id=? AND (? IS NULL OR t.contractor_id=?) "
            "AND (?=1 OR v.id=(SELECT vv.id FROM timesheet_versions vv WHERE vv.timesheet_id=t.id "
            "ORDER BY (vv.status!='cancelled') DESC,vv.version_no DESC LIMIT 1)) ORDER BY t.year DESC,t.month DESC,t.id,v.version_no DESC",
            (self.project_id, contractor_id, contractor_id, int(history))).fetchall()
        return [dict(r) for r in rows]

    def eligible_workers(self, sheet_id):
        h = self._header(sheet_id)
        days = self._days(h)
        # Intersection with the month, including closed historical availability.
        rows = self.db.execute("SELECT DISTINCT e.*,s.name AS specialty_name FROM employees e "
            "JOIN specialties s ON s.id=e.specialty_id JOIN employee_projects ep ON ep.employee_id=e.id "
            "JOIN employee_project_periods p ON p.employee_project_id=ep.id WHERE ep.project_id=? AND e.contractor_id=? "
            "AND COALESCE(p.start_date,'0001-01-01')<=? AND (p.end_date IS NULL OR p.end_date>?) "
            "AND (p.start_date IS NULL OR p.end_date IS NULL OR p.end_date>p.start_date) ORDER BY e.full_name,e.id",
            (self.project_id, h['contractor_id'], days[-1], days[0])).fetchall()
        return [dict(r) for r in rows if any(self.people.available(r['id'], d) for d in days)]

    def _add_workers(self, h, v, ids):
        eligible = {r['id']: r for r in self.eligible_workers(h['id'])}
        for employee_id in dict.fromkeys(ids):
            if self.db.execute('SELECT 1 FROM timesheet_workers WHERE version_id=? AND employee_id=?', (v['id'], employee_id)).fetchone():
                continue
            employee = eligible.get(employee_id)
            if employee is None:
                raise ValidationError("Работник не связан с подрядчиком и объектом в выбранном месяце")
            first = next((d for d in self._days(h) if self.people.available(employee_id, d)), None)
            if first is None:
                raise ValidationError("У работника нет доступных дат в месяце табеля")
            member = self.brigades.current(employee_id, first)
            self.db.execute('INSERT INTO timesheet_workers(version_id,employee_id,full_name,specialty_name,brigade_number) VALUES (?,?,?,?,?)',
                            (v['id'], employee_id, employee['full_name'], employee['specialty_name'], member['number'] if member else ''))

    def create(self, contractor_id, year, month, comment='', *, operation_key=None):
        if isinstance(year, bool) or isinstance(month, bool) or not isinstance(year, int) or not isinstance(month, int) or not 1900 <= year <= 9999 or not 1 <= month <= 12:
            raise ValidationError("Выберите год 1900–9999 и календарный месяц 1–12")
        comment = clean(comment, 'Комментарий', 4000, False)
        fingerprint = _digest([contractor_id, year, month, comment])
        with transaction(self.db):
            replay = self._replay(operation_key, 'create', fingerprint)
            if replay is not None:
                return replay
            self._writable()
            self.people.contractor(contractor_id, active=True)
            now = instant()
            existing = self.db.execute('SELECT id FROM timesheets WHERE project_id=? AND contractor_id=? AND year=? AND month=?',
                                       (self.project_id, contractor_id, year, month)).fetchone()
            number = 1
            if existing:
                sid = existing['id']
                if self.db.execute("SELECT 1 FROM timesheet_versions WHERE timesheet_id=? AND status!='cancelled'", (sid,)).fetchone():
                    raise ValidationError("Табель за этот месяц уже существует. Откройте его; после утверждения используйте корректирующую версию")
                number = self.db.execute('SELECT MAX(version_no)+1 FROM timesheet_versions WHERE timesheet_id=?', (sid,)).fetchone()[0]
            else:
                sid = self.db.execute('INSERT INTO timesheets(project_id,contractor_id,year,month,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                                      (self.project_id, contractor_id, year, month, now, now)).lastrowid
            vid = self.db.execute("INSERT INTO timesheet_versions(timesheet_id,version_no,status,comment,created_at,updated_at) VALUES (?,?,'draft',?,?,?)", (sid, number, comment, now, now)).lastrowid
            self.db.execute('UPDATE timesheets SET updated_at=? WHERE id=?', (now, sid))
            self._add_workers(self._header(sid), self._version(sid, vid), [r['id'] for r in self.eligible_workers(sid)])
            self._event(vid, 'created', dict(year=year, month=month))
            self._remember(operation_key, vid, 'create', fingerprint, sid)
            return sid

    def add_workers(self, sheet_id, employee_ids, *, version_id=None, expected_revision=None, operation_key=None):
        ids = list(dict.fromkeys(employee_ids))
        fingerprint = _digest([sheet_id, version_id, ids])
        with transaction(self.db):
            replay = self._replay(operation_key, 'add_workers', fingerprint, sheet_id)
            if replay is not None:
                return replay
            v = self._draft(sheet_id, version_id, expected_revision)
            self._add_workers(self._header(sheet_id), v, ids)
            self._touch(v['id'])
            self._event(v['id'], 'workers_added', dict(employee_ids=ids))
            self._remember(operation_key, v['id'], 'add_workers', fingerprint, True)
            return True

    def set_worker_note(self, sheet_id, employee_id, note, *, version_id=None, expected_revision=None, operation_key=None):
        note = clean(note, 'Примечание работника', 2000, False)
        fingerprint = _digest([sheet_id, version_id, employee_id, note])
        with transaction(self.db):
            replay = self._replay(operation_key, 'worker_note', fingerprint, sheet_id)
            if replay is not None:
                return replay
            v = self._draft(sheet_id, version_id, expected_revision)
            old = self.db.execute('SELECT note FROM timesheet_workers WHERE version_id=? AND employee_id=?', (v['id'], employee_id)).fetchone()
            if old is None:
                raise ValidationError("Работник не входит в выбранную версию табеля")
            self.db.execute('UPDATE timesheet_workers SET note=? WHERE version_id=? AND employee_id=?', (note, v['id'], employee_id))
            self._touch(v['id'])
            self._event(v['id'], 'worker_note', dict(employee_id=employee_id, before=old['note'], after=note))
            self._remember(operation_key, v['id'], 'worker_note', fingerprint, True)
            return True

    def set_comment(self, sheet_id, comment, *, version_id=None, expected_revision=None, operation_key=None):
        comment = clean(comment, 'Комментарий табеля', 4000, False)
        fingerprint = _digest([sheet_id, version_id, comment])
        with transaction(self.db):
            replay = self._replay(operation_key, 'comment', fingerprint, sheet_id)
            if replay is not None:
                return replay
            v = self._draft(sheet_id, version_id, expected_revision)
            self.db.execute('UPDATE timesheet_versions SET comment=? WHERE id=?', (comment, v['id']))
            self._touch(v['id'])
            self._event(v['id'], 'comment', dict(before=v['comment'], after=comment))
            self._remember(operation_key, v['id'], 'comment', fingerprint, True)
            return True

    def _normal_changes(self, h, v, changes):
        if not isinstance(changes, list) or len(changes) > MAX_CHANGES:
            raise ValidationError("Слишком много изменений: максимум 20 000 записей")
        result, seen = [], set()
        allowed_days = set(self._days(h))
        for source in changes:
            if isinstance(source.get('employee_id'), bool) or not isinstance(source.get('employee_id'), int):
                raise ValidationError("Укажите постоянный ID работника")
            eid = source['employee_id']
            try:
                when = day(source.get('work_date'))
            except (ValueError, TypeError):
                raise ValidationError("Дата записи: требуется действительная дата ГГГГ-ММ-ДД") from None
            if when not in allowed_days:
                raise ValidationError("Дата записи должна находиться в месяце табеля")
            if (eid, when) in seen:
                raise ValidationError("Повторная запись одного работника на одну дату")
            seen.add((eid, when))
            roster = self.db.execute('SELECT 1 FROM timesheet_workers WHERE version_id=? AND employee_id=?', (v['id'], eid)).fetchone()
            if not roster:
                raise ValidationError("Работник не входит в состав этой версии табеля")
            item = dict(employee_id=eid, work_date=when, clear=bool(source.get('clear', False)))
            if item['clear']:
                result.append(item)
                continue
            employee = self.people.employee(eid)
            if employee['contractor_id'] != h['contractor_id']:
                # Historical correction may retain a former employee's known days;
                # no employer history exists to authorise additional days silently.
                if not v['base_version_id'] or not self.db.execute('SELECT 1 FROM timesheet_entries WHERE version_id=? AND employee_id=? AND work_date=?', (v['base_version_id'], eid, when)).fetchone():
                    raise ValidationError("Работник относится к другому подрядчику; нет исторического основания для этой даты")
            if not self.people.available(eid, when):
                raise ValidationError("Работник не назначен на объект на дату записи")
            code = clean(source.get('code', ''), 'Код явки', 20).upper()
            c = self.db.execute('SELECT * FROM attendance_codes WHERE code=? AND is_active=1', (code,)).fetchone()
            if c is None:
                raise ValidationError("Выберите действующий код явки")
            raw_hours = source.get('hours')
            if raw_hours is None or raw_hours == '':
                if c['requires_hours']:
                    raise ValidationError("Для выбранного кода необходимо явно указать часы")
                raw_hours = 0
            nanos = hours_value(raw_hours)
            if not c['counts_as_work'] and nanos != 0:
                raise ValidationError("Код отсутствия или выходного не допускает отработанные часы")
            item.update(attendance_code_id=c['id'], code_snapshot=code, counts_as_work=c['counts_as_work'],
                        requires_hours=c['requires_hours'], hours_nanos=nanos,
                        comment=clean(source.get('comment', ''), 'Комментарий записи', 2000, False))
            result.append(item)
        return result

    def _write_changes(self, vid, changes):
        now = instant()
        for c in changes:
            args = (vid, c['employee_id'], c['work_date'])
            if c['clear']:
                self.db.execute('DELETE FROM timesheet_entries WHERE version_id=? AND employee_id=? AND work_date=?', args)
                continue
            self.db.execute('INSERT INTO timesheet_entries(version_id,employee_id,work_date,attendance_code_id,code_snapshot,counts_as_work,requires_hours,hours_nanos,comment,created_at,updated_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(version_id,employee_id,work_date) DO UPDATE SET attendance_code_id=excluded.attendance_code_id,code_snapshot=excluded.code_snapshot,'
                'counts_as_work=excluded.counts_as_work,requires_hours=excluded.requires_hours,hours_nanos=excluded.hours_nanos,comment=excluded.comment,updated_at=excluded.updated_at',
                args + (c['attendance_code_id'], c['code_snapshot'], c['counts_as_work'], c['requires_hours'], c['hours_nanos'], c['comment'], now, now))

    def _reserved(self, exclude_sheet=None):
        """One maximum per logical sheet/day, not the sum of historical versions."""
        rows = self.db.execute("SELECT t.id AS sheet_id,t.project_id,e.employee_id,e.work_date,e.hours_nanos FROM timesheets t "
            "JOIN timesheet_versions v ON v.timesheet_id=t.id JOIN timesheet_entries e ON e.version_id=v.id "
            "WHERE (? IS NULL OR t.id!=?) AND (v.status IN ('draft','submitted') OR "
            "(v.status='approved' AND v.version_no=(SELECT MAX(a.version_no) FROM timesheet_versions a WHERE a.timesheet_id=t.id AND a.status='approved')))" ,
            (exclude_sheet, exclude_sheet)).fetchall()
        amounts = {}
        for row in rows:
            key = (row['sheet_id'], row['employee_id'], row['work_date'])
            amounts[key] = max(amounts.get(key, 0), row['hours_nanos'])
        totals = {}
        for (_, employee, when), nanos in amounts.items():
            totals[(employee, when)] = totals.get((employee, when), 0) + nanos
        return totals

    def _validate_version(self, h, v, *, approval=False):
        totals = self._reserved(exclude_sheet=h['id'])
        own = {(r['employee_id'], r['work_date']): r['hours_nanos'] for r in self.db.execute('SELECT * FROM timesheet_entries WHERE version_id=?', (v['id'],))}
        if not approval:
            approved = self.db.execute("SELECT id FROM timesheet_versions WHERE timesheet_id=? AND status='approved' ORDER BY version_no DESC LIMIT 1", (h['id'],)).fetchone()
            if approved:
                for row in self.db.execute('SELECT * FROM timesheet_entries WHERE version_id=?', (approved['id'],)):
                    key = (row['employee_id'], row['work_date'])
                    own[key] = max(own.get(key, 0), row['hours_nanos'])
        for key, nanos in own.items():
            if totals.get(key, 0) + nanos > 24 * SCALE:
                raise ValidationError("Превышено 24 часа по работнику ID {} на {} с учётом других объектов и резервов черновиков".format(*key))
        allowed = set(self._days(h))
        for row in self.db.execute('SELECT * FROM timesheet_entries WHERE version_id=?', (v['id'],)):
            if row['work_date'] not in allowed or not self.people.available(row['employee_id'], row['work_date']):
                raise ValidationError("Есть запись вне допустимого периода назначения работника на объект")
            employee = self.people.employee(row['employee_id'])
            if employee['contractor_id'] != h['contractor_id'] and not (v['base_version_id'] and self.db.execute(
                    'SELECT 1 FROM timesheet_entries WHERE version_id=? AND employee_id=? AND work_date=?',
                    (v['base_version_id'], row['employee_id'], row['work_date'])).fetchone()):
                raise ValidationError("Подрядчик работника изменился после заполнения: проверьте контекст и историческое основание записи")
            if not 0 <= row['hours_nanos'] <= 24 * SCALE or (not row['counts_as_work'] and row['hours_nanos']):
                raise ValidationError("Код явки противоречит отработанным часам")

    def set_entries(self, sheet_id, changes, *, version_id=None, expected_revision=None, operation_key=None):
        fingerprint = _digest([sheet_id, version_id, changes])
        with transaction(self.db):
            replay = self._replay(operation_key, 'entries', fingerprint, sheet_id)
            if replay is not None:
                return replay
            v = self._draft(sheet_id, version_id, expected_revision)
            h = self._header(sheet_id)
            prepared = self._normal_changes(h, v, changes)
            before = [dict(r) for r in self.db.execute('SELECT employee_id,work_date,code_snapshot,hours_nanos,comment FROM timesheet_entries WHERE version_id=?', (v['id'],))
                      if (r['employee_id'], r['work_date']) in {(c['employee_id'], c['work_date']) for c in prepared}]
            self._write_changes(v['id'], prepared)
            self._validate_version(h, v)
            self._touch(v['id'])
            self._event(v['id'], 'entries_changed', dict(before=before, after=prepared))
            self._remember(operation_key, v['id'], 'entries', fingerprint, True)
            return True

    def report(self, sheet_id, version_id=None):
        with read_snapshot(self.db):
            return self._report(sheet_id, version_id)

    def _report(self, sheet_id, version_id=None):
        h, v = self._header(sheet_id), self._version(sheet_id, version_id)
        days = self._days(h)
        entries = {}
        for row in self.db.execute('SELECT * FROM timesheet_entries WHERE version_id=? ORDER BY work_date,employee_id', (v['id'],)):
            item = dict(row, hours=decimal_hours(row['hours_nanos']), code=row['code_snapshot'])
            entries.setdefault(row['employee_id'], {})[row['work_date']] = item
        workers, warnings = [], []
        for row in self.db.execute('SELECT * FROM timesheet_workers WHERE version_id=? ORDER BY full_name,employee_id', (v['id'],)):
            values = entries.get(row['employee_id'], {})
            worker = dict(row, entries=values,
                total_hours=exact_sum([r['hours'] for r in values.values() if r['counts_as_work']]),
                attendance_days=sum(r['counts_as_work'] and r['hours_nanos'] > 0 for r in values.values()),
                absence_days=sum(not r['counts_as_work'] for r in values.values()), missing_days=len(days) - len(values))
            workers.append(worker)
            if any(r['counts_as_work'] and not r['hours_nanos'] for r in values.values()):
                warnings.append('Работник ID {}: явка с явно указанными нулевыми часами'.format(row['employee_id']))
        # Informational only: without start/end times a daily sum cannot prove
        # that two intervals do not overlap. Never leak another object's names.
        overlap = self.db.execute("SELECT DISTINCT a.employee_id,a.work_date FROM timesheet_entries a JOIN timesheet_versions av ON av.id=a.version_id "
            "WHERE a.version_id=? AND a.hours_nanos>0 AND EXISTS(SELECT 1 FROM timesheet_entries b JOIN timesheet_versions bv ON bv.id=b.version_id "
            "JOIN timesheets bt ON bt.id=bv.timesheet_id WHERE b.employee_id=a.employee_id AND b.work_date=a.work_date AND b.hours_nanos>0 "
            "AND bt.project_id!=? AND (bv.status IN ('draft','submitted') OR (bv.status='approved' AND bv.version_no="
            "(SELECT MAX(bav.version_no) FROM timesheet_versions bav WHERE bav.timesheet_id=bt.id AND bav.status='approved'))))",
            (v['id'], self.project_id)).fetchall()
        if overlap:
            warnings.append('Есть рабочее время на нескольких объектах в одни сутки. Предел 24 часа проверяется; пересечения интервалов без времени начала/окончания определить нельзя.')
        return dict(timesheet=h, version=v, days=days, workers=workers, warnings=warnings,
            total_hours=exact_sum([w['total_hours'] for w in workers]), generated_at=instant(), codes=self.codes(),
            eligible_workers=self.eligible_workers(sheet_id),
            versions=[dict(r) for r in self.db.execute('SELECT * FROM timesheet_versions WHERE timesheet_id=? ORDER BY version_no DESC', (sheet_id,))],
            events=[dict(r) for r in self.db.execute('SELECT * FROM timesheet_events WHERE version_id=? ORDER BY id', (v['id'],))])

    def transition(self, sheet_id, action, *, version_id=None, reason='', expected_revision=None, operation_key=None):
        mapping = {'submit': ('draft', 'submitted'), 'approve': ('submitted', 'approved'),
                   'return': ('submitted', 'draft'), 'cancel': (None, 'cancelled')}
        if action not in mapping:
            raise ValidationError("Неизвестное действие над табелем")
        reason = clean(reason, 'Основание', 4000, False)
        fingerprint = _digest([sheet_id, version_id, action, reason])
        with transaction(self.db):
            replay = self._replay(operation_key, action, fingerprint, sheet_id)
            if replay is not None:
                return replay
            self._writable()
            h, v = self._header(sheet_id), self._version(sheet_id, version_id)
            self._revision(v, expected_revision)
            old, new = mapping[action]
            if (old and v['status'] != old) or (action == 'cancel' and v['status'] not in ('draft', 'submitted')):
                raise ValidationError("Недопустимый переход статуса табеля")
            if action in ('cancel', 'return') and not reason:
                raise ValidationError("Укажите основание отмены или возврата")
            if action in ('submit', 'approve'):
                if not self.db.execute('SELECT 1 FROM timesheet_workers WHERE version_id=?', (v['id'],)).fetchone():
                    raise ValidationError("Добавьте работников перед передачей табеля на проверку")
                self._validate_version(h, v, approval=action == 'approve')
            now = instant()
            field = {'submit': 'submitted_at', 'approve': 'approved_at', 'cancel': 'cancelled_at', 'return': 'submitted_at'}[action]
            self.db.execute('UPDATE timesheet_versions SET status=?,revision=revision+1,updated_at=?,' + field + '=? WHERE id=?',
                            (new, now, None if action == 'return' else now, v['id']))
            self.db.execute('UPDATE timesheets SET updated_at=? WHERE id=?', (now, sheet_id))
            self._event(v['id'], action, dict(old_status=v['status'], new_status=new, reason=reason,
                                            identity='local_operator_not_authenticated'))
            self._remember(operation_key, v['id'], action, fingerprint, True)
            return True

    def create_correction(self, sheet_id, reason, *, version_id=None, expected_revision=None, operation_key=None):
        reason = clean(reason, 'Причина корректировки', 4000)
        fingerprint = _digest([sheet_id, version_id, reason])
        with transaction(self.db):
            replay = self._replay(operation_key, 'correction', fingerprint, sheet_id)
            if replay is not None:
                return replay
            self._writable()
            h, old = self._header(sheet_id), self._version(sheet_id)
            if version_id is not None and old['id'] != version_id:
                raise ValidationError("Утверждённая версия изменилась. Откройте актуальный табель перед созданием корректировки")
            self._revision(old, expected_revision)
            if old['status'] != 'approved':
                raise ValidationError("Корректирующая версия создаётся после утверждения; незавершённый черновик уже существует либо нет утверждённого табеля")
            number = self.db.execute('SELECT MAX(version_no)+1 FROM timesheet_versions WHERE timesheet_id=?', (sheet_id,)).fetchone()[0]
            now = instant()
            vid = self.db.execute("INSERT INTO timesheet_versions(timesheet_id,version_no,status,base_version_id,correction_reason,comment,created_at,updated_at) VALUES (?,?,'draft',?,?,?,?,?)",
                (sheet_id, number, old['id'], reason, old['comment'], now, now)).lastrowid
            self.db.execute('INSERT INTO timesheet_workers(version_id,employee_id,full_name,specialty_name,brigade_number,note) '
                            'SELECT ?,employee_id,full_name,specialty_name,brigade_number,note FROM timesheet_workers WHERE version_id=?', (vid, old['id']))
            self.db.execute('INSERT INTO timesheet_entries(version_id,employee_id,work_date,attendance_code_id,code_snapshot,counts_as_work,requires_hours,hours_nanos,comment,created_at,updated_at) '
                'SELECT ?,employee_id,work_date,attendance_code_id,code_snapshot,counts_as_work,requires_hours,hours_nanos,comment,?,? FROM timesheet_entries WHERE version_id=?', (vid, now, now, old['id']))
            self.db.execute('UPDATE timesheets SET updated_at=? WHERE id=?', (now, sheet_id))
            self._event(vid, 'correction_created', dict(base_version_id=old['id'], reason=reason))
            self._remember(operation_key, vid, 'correction', fingerprint, vid)
            return vid

    def _preview(self, sheet_id, changes, kind, *, version_id=None, expected_revision=None, filename='', errors=None, metadata=None):
        if not isinstance(changes, list) or not changes or len(changes) > MAX_CHANGES:
            raise ValidationError("Выберите от 1 до 20 000 изменений для предварительного просмотра")
        with transaction(self.db):
            v = self._draft(sheet_id, version_id, expected_revision)
            h = self._header(sheet_id)
            errors = list(errors or [])
            normal, display, warnings = [], [], []
            for index, change in enumerate(changes):
                shown = dict(change)
                try:
                    prepared = self._normal_changes(h, v, [change])[0]
                    normal.append(prepared)
                    worker = self.db.execute('SELECT full_name FROM timesheet_workers WHERE version_id=? AND employee_id=?',
                                             (v['id'], prepared['employee_id'])).fetchone()
                    supplied_name = change.get('full_name', '')
                    if supplied_name and ' '.join(supplied_name.split()).casefold() != ' '.join(worker['full_name'].split()).casefold():
                        warnings.append('Строка {}: ФИО отличается от снимка табеля; проверьте выбранный ID {}'.format(change.get('row_number', index + 1), prepared['employee_id']))
                    shown.update(full_name=worker['full_name'], clear=prepared['clear'])
                    if not prepared['clear']:
                        shown.update(code=prepared['code_snapshot'], hours=canonical(decimal_hours(prepared['hours_nanos'])), comment=prepared['comment'])
                        if prepared['counts_as_work'] and not prepared['hours_nanos']:
                            warnings.append('Строка {}: явка с явно указанными нулевыми часами'.format(change.get('row_number', index + 1)))
                except (ValueError, TypeError) as exc:
                    errors.append('Строка {}: {}'.format(change.get('row_number', index + 1), str(exc)))
                display.append(shown)
            keys = [(r['employee_id'], r['work_date']) for r in normal]
            if len(set(keys)) != len(keys):
                errors.append('Повтор одного работника и даты в предварительном просмотре')
            # Validate proposed records and all cross-project reservations, then
            # roll back only the provisional cell writes, retaining the preview.
            self.db.execute('SAVEPOINT timesheet_preview')
            try:
                if not errors:
                    self._write_changes(v['id'], normal)
                    self._validate_version(h, v)
            except (ValueError, TypeError) as exc:
                errors.append(str(exc))
            finally:
                self.db.execute('ROLLBACK TO timesheet_preview')
                self.db.execute('RELEASE timesheet_preview')
            token = uuid4().hex
            context = dict(metadata or {}, prepared_digest=_digest(normal))
            summary = dict(errors=errors, warnings=warnings, count=len(changes), metadata=context)
            self.db.execute("INSERT INTO timesheet_previews(id,version_id,revision,kind,filename,changes_json,summary_json,status,created_at) VALUES (?,?,?,?,?,?,?,'preview',?)",
                            (token, v['id'], v['revision'], kind, clean(filename, 'Имя файла', 240, False), _json(display), _json(summary), instant()))
            return token

    def preview_bulk(self, sheet_id, employee_ids, start, end, code, hours, comment='', *, brigade_id=None, version_id=None, expected_revision=None):
        start, end = day(start), day(end)
        h = self._header(sheet_id)
        if start > end or start not in self._days(h) or end not in self._days(h):
            raise ValidationError("Выберите диапазон внутри календарного месяца табеля")
        ids = list(dict.fromkeys(employee_ids))
        if not ids:
            raise ValidationError("Выберите работников; остальным явка не назначается")
        dates = [d for d in self._days(h) if start <= d <= end]
        if len(dates) * len(ids) > MAX_CHANGES:
            raise ValidationError("Слишком много записей для одной операции")
        if brigade_id is not None:
            b = self.brigades.get(brigade_id)
            if b['contractor_id'] != h['contractor_id']:
                raise ValidationError("Бригада относится к другому подрядчику")
            for d in dates:
                allowed = {r['employee_id'] for r in self.brigades.members(brigade_id, d)}
                if not set(ids) <= allowed:
                    raise ValidationError("Выбранный работник не входит в бригаду на одну из дат диапазона")
        changes = [dict(employee_id=eid, work_date=d, code=code, hours=hours, comment=comment) for d in dates for eid in ids]
        return self._preview(sheet_id, changes, 'bulk', version_id=version_id, expected_revision=expected_revision,
                             metadata=dict(brigade_id=brigade_id, start=start, end=end))

    def preview_copy(self, sheet_id, source_date, target_dates, employee_ids=None, *, version_id=None, expected_revision=None):
        v = self._version(sheet_id, version_id)
        days = self._days(self._header(sheet_id))
        source_date = day(source_date)
        targets = list(dict.fromkeys(day(d) for d in target_dates))
        if source_date not in days or not targets or any(d not in days or d == source_date for d in targets):
            raise ValidationError("Копировать можно между разными датами одного месяца")
        rows = list(self.db.execute('SELECT * FROM timesheet_entries WHERE version_id=? AND work_date=?', (v['id'], source_date)))
        if employee_ids is not None:
            rows = [r for r in rows if r['employee_id'] in employee_ids]
        changes = [dict(employee_id=r['employee_id'], work_date=d, code=r['code_snapshot'], hours=canonical(decimal_hours(r['hours_nanos'])), comment=r['comment']) for r in rows for d in targets]
        if not changes:
            raise ValidationError("На исходную дату нет записей для копирования")
        return self._preview(sheet_id, changes, 'copy', version_id=v['id'], expected_revision=expected_revision)

    def preview_import(self, sheet_id, rows, filename, *, version_id=None, expected_revision=None):
        if not rows or len(rows) > MAX_CHANGES:
            raise ValidationError("Книга не содержит записей либо превышает 20 000 строк")
        errors = ['Строка {}: {}'.format(r['row_number'], error) for r in rows for error in r.get('errors', [])]
        return self._preview(sheet_id, rows, 'import', version_id=version_id, expected_revision=expected_revision, filename=filename, errors=errors)

    def get_preview(self, sheet_id, preview_id):
        self._header(sheet_id)
        row = self.db.execute('SELECT p.* FROM timesheet_previews p JOIN timesheet_versions v ON v.id=p.version_id WHERE p.id=? AND v.timesheet_id=?', (preview_id, sheet_id)).fetchone()
        if row is None:
            raise ValidationError("Предварительный просмотр не принадлежит выбранному табелю")
        result = dict(row)
        result.update(json.loads(row['summary_json']))
        result['changes'] = json.loads(row['changes_json'])
        return result

    def apply_preview(self, sheet_id, preview_id, *, operation_key=None):
        fingerprint = _digest([sheet_id, preview_id])
        with transaction(self.db):
            replay = self._replay(operation_key, 'preview_apply', fingerprint, sheet_id)
            if replay is not None:
                return replay
            p = self.get_preview(sheet_id, preview_id)
            if p['status'] == 'applied':
                return True
            if p['status'] != 'preview' or p['errors']:
                raise ValidationError("Исправьте ошибки предварительного просмотра; запись не выполнена")
            v = self._draft(sheet_id, p['version_id'], p['revision'])
            h = self._header(sheet_id)
            # Permanent membership may have changed since the preview.
            metadata = p.get('metadata', {})
            if p['kind'] == 'bulk' and metadata.get('brigade_id'):
                for c in p['changes']:
                    if c['employee_id'] not in {m['employee_id'] for m in self.brigades.members(metadata['brigade_id'], c['work_date'])}:
                        raise ValidationError("Состав бригады изменился; сформируйте новый предварительный просмотр")
            changes = self._normal_changes(h, v, p['changes'])
            if metadata.get('prepared_digest') != _digest(changes):
                raise ValidationError("Справочник кодов изменился после просмотра; проверьте новый предварительный просмотр")
            before = [dict(r) for r in self.db.execute('SELECT employee_id,work_date,code_snapshot,hours_nanos,comment FROM timesheet_entries WHERE version_id=?', (v['id'],))
                      if (r['employee_id'], r['work_date']) in {(c['employee_id'], c['work_date']) for c in changes}]
            self._write_changes(v['id'], changes)
            self._validate_version(h, v)
            self._touch(v['id'])
            self.db.execute("UPDATE timesheet_previews SET status='applied',completed_at=? WHERE id=?", (instant(), preview_id))
            self._event(v['id'], p['kind'] + '_applied', dict(preview_id=preview_id, before=before, after=changes))
            self._remember(operation_key, v['id'], 'preview_apply', fingerprint, True)
            return True

    def cancel_preview(self, sheet_id, preview_id):
        with transaction(self.db):
            p = self.get_preview(sheet_id, preview_id)
            self._draft(sheet_id, p['version_id'])
            if p['status'] != 'preview':
                raise ValidationError("Предварительный просмотр уже завершён")
            self.db.execute("UPDATE timesheet_previews SET status='cancelled',completed_at=? WHERE id=?", (instant(), preview_id))
            self._event(p['version_id'], 'preview_cancelled', dict(preview_id=preview_id))
