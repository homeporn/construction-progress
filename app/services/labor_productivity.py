"""Exact, read-only labor projection shared by the screen and spreadsheet.

Attendance belongs to a person/day, allocations to work, and quantities to the
immutable journals. None means that a measurement is unknown; zero is measured.
"""
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from app.calculation_engine.historical import read_snapshot
from app.decimal_utils import ZERO, exact_sum, multiply, ratio, subtract
from app.models import Discipline
from app.repositories.base import ValidationError
from app.repositories.labor_repository import LaborRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.work_repository import WorkRepository
from app.time_utils import day, end_of_day, instant


def _total(rows, field):
    return exact_sum(row[field] for row in rows)


def _metric(metadata, quantity, normative, hours, *, complete, conflict=False, reference_output=None):
    """Only like code/unit/direction quantities enter this function."""
    usable = hours is not None and hours > ZERO and not conflict
    actual = ratio(quantity, hours) if quantity is not None and usable else None
    norm_output = (ratio(quantity, normative) if quantity is not None and quantity > ZERO
                   and normative is not None and normative > ZERO else
                   reference_output if quantity is not None and quantity >= ZERO else None)
    return dict(code=metadata['code'], name=metadata['name'], unit=metadata['unit'],
        discipline=metadata['discipline'], quantity=quantity, normative_hours=normative,
        hours=None if conflict else hours, actual_output=actual, norm_output=norm_output,
        norm_ratio=ratio(normative, hours) if normative is not None and usable else None,
        complete=bool(complete and not conflict), conflict=conflict)


class _Norms:
    """All approved baselines loaded once; lookups never issue SQL."""
    def __init__(self, db, project_id, cutoff, discipline, explicit):
        versions = [dict(r) for r in db.execute(
            "SELECT id,approved_at,version_no FROM plan_versions WHERE project_id=? "
            "AND discipline=? AND status='APPROVED' AND approved_at<=? "
            "ORDER BY approved_at,version_no", (project_id, discipline, cutoff))]
        self.ids = {v['id'] for v in versions}
        if explicit is not None and explicit not in self.ids:
            raise ValidationError('Выберите доступную утверждённую версию плана текущего направления')
        self.explicit, self.versions = explicit, versions
        self.keys = [(v['approved_at'], v['version_no']) for v in versions]
        self.lines = {}
        for r in db.execute(
                "SELECT l.plan_version_id,l.work_item_id,l.labor_norm,l.unit FROM plan_lines l "
                "JOIN plan_versions v ON v.id=l.plan_version_id WHERE v.project_id=? "
                "AND v.discipline=? AND v.status='APPROVED' AND v.approved_at<=?",
                (project_id, discipline, cutoff)):
            self.lines[(r['plan_version_id'], r['work_item_id'])] = dict(r)

    def norm(self, metadata, business_date, frozen=None, *, use_explicit=True):
        version = self.explicit if use_explicit and self.explicit is not None else frozen
        if version is None:
            index = bisect_right(self.keys, (end_of_day(business_date), 2 ** 63)) - 1
            version = self.versions[index]['id'] if index >= 0 else None
        row = self.lines.get((version, metadata['id']))
        if row is None or row['labor_norm'] is None or row['unit'] != metadata['unit']:
            return None
        norm = Decimal(row['labor_norm'])
        return norm if norm > ZERO else None


class LaborProductivityService:
    def __init__(self, db, project_id=1):
        self.db, self.project_id = db, project_id
        self.labor = LaborRepository(db, project_id)
        self.work = WorkRepository(db, project_id)

    def _filters(self, supplied):
        filters = {k: v for k, v in (supplied or {}).items() if v not in (None, '')}
        allowed = {'building_id', 'floor_id', 'catalog_id', 'element', 'contractor_id',
                   'brigade_id', 'plan_version_id'}
        if set(filters) - allowed:
            raise ValidationError('Неизвестный фильтр производительности')
        for field in allowed - {'element'}:
            if field in filters and (isinstance(filters[field], bool)
                                     or not isinstance(filters[field], int) or filters[field] < 1):
                raise ValidationError('Некорректный идентификатор фильтра')
        if filters.get('building_id'):
            self.work._building(filters['building_id'])
        if filters.get('floor_id'):
            floor = self.work._floor(filters['floor_id'])
            if filters.get('building_id') and floor['building_id'] != filters['building_id']:
                raise ValidationError('Этаж не относится к выбранному корпусу')
        if filters.get('catalog_id') and not self.db.execute(
                'SELECT 1 FROM work_catalog WHERE id=?', (filters['catalog_id'],)).fetchone():
            raise ValidationError('Код работы не найден')
        if filters.get('contractor_id') and not self.db.execute(
                'SELECT 1 FROM contractor_projects WHERE contractor_id=? AND project_id=?',
                (filters['contractor_id'], self.project_id)).fetchone():
            raise ValidationError('Подрядчик не относится к текущему объекту')
        if filters.get('brigade_id'):
            brigade = self.db.execute(
                'SELECT b.contractor_id FROM brigades b JOIN contractor_projects c '
                'ON c.contractor_id=b.contractor_id WHERE b.id=? AND c.project_id=?',
                (filters['brigade_id'], self.project_id)).fetchone()
            if brigade is None or (filters.get('contractor_id')
                                   and brigade['contractor_id'] != filters['contractor_id']):
                raise ValidationError('Бригада не относится к выбранному подрядчику или объекту')
        return filters

    @staticmethod
    def _executor(row, filters):
        return all(row.get(k) == filters[k] for k in ('contractor_id', 'brigade_id') if k in filters)

    def build(self, start_date, end_date, knowledge_cutoff=None, *, mode='actual',
              discipline='CONSTRUCTION', filters=None, plan_version_id=None):
        start, end, cutoff = day(start_date), day(end_date), instant(knowledge_cutoff)
        if start > end:
            raise ValidationError('Начало периода не может быть позже окончания')
        if mode not in ('actual', 'confirmed'):
            raise ValidationError('Неизвестный режим производительности')
        try:
            direction = Discipline(discipline).value
        except ValueError as exc:
            raise ValidationError('Неизвестное направление работ') from exc
        with read_snapshot(self.db):
            selected = self._filters(filters)
            if plan_version_id is not None:
                if isinstance(plan_version_id, bool) or not isinstance(plan_version_id, int) or plan_version_id < 1:
                    raise ValidationError('Некорректный идентификатор версии плана')
                if selected.get('plan_version_id', plan_version_id) != plan_version_id:
                    raise ValidationError('Выбраны разные версии плана')
                selected['plan_version_id'] = plan_version_id
            return self._build(start, end, cutoff, mode, direction, selected)

    def _build(self, start, end, cutoff, mode, direction, filters):
        project = dict(ProjectRepository(self.db).get_project(self.project_id))
        norms = _Norms(self.db, self.project_id, cutoff, direction, filters.get('plan_version_id'))
        metadata = {r['id']: dict(r) for r in self.labor.metadata()}
        spatial = {k: v for k, v in filters.items()
                   if k in ('building_id', 'floor_id', 'catalog_id', 'element')}
        chosen = {key: r for key, r in metadata.items() if r['discipline'] == direction
                  and all(r[k] == value for k, value in spatial.items())}
        all_time = [dict(r) for r in self.labor.approved_entries(start, end, cutoff)]
        attendance = [r for r in all_time if self._executor(r, filters)]
        capacity = defaultdict(list)
        for row in all_time:
            capacity[self._time_key(row)].append(row['hours'])
        capacity = {key: exact_sum(values) for key, values in capacity.items()}
        allocations = [dict(r) for r in self.labor.hour_rows(cutoff, start, end)]
        by_person = defaultdict(list)
        for row in allocations:
            by_person[self._time_key(row)].append(row)
        bad_time = {key for key, rows in by_person.items()
                    if _total(rows, 'hours') > capacity.get(key, ZERO)
                    or any(r['hours'] < ZERO for r in rows)}
        for row in allocations:
            row['valid'] = self._time_key(row) not in bad_time
            row.update({k: v for k, v in metadata.get(row['work_item_id'], {}).items() if k != 'id'})
            row['labor_norm'] = (norms.norm(metadata[row['work_item_id']], row['work_date'],
                row['plan_version_id'], use_explicit=False) if row['work_item_id'] in metadata else None)
        scoped_hours = [r for r in allocations if r['work_item_id'] in chosen and self._executor(r, filters)]

        roots = {r['id']: dict(r) for r in self.labor.fact_roots(end, cutoff)}
        attributes = [dict(r) for r in self.labor.attribution_rows(cutoff)]
        assigned = defaultdict(list)
        for row in attributes:
            assigned[row['fact_event_id']].append(row['quantity'])
        bad_volume = {root for root, values in assigned.items() if root not in roots
                      or exact_sum(values) > max(ZERO, roots[root]['available'])
                      or any(q < ZERO for q in values)}
        for row in attributes:
            root = roots.get(row['fact_event_id'])
            row['valid'] = row['fact_event_id'] not in bad_volume
            if root:
                row.update(work_item_id=root['work_item_id'], work_date=root['work_date'])
                row.update({k: v for k, v in metadata.get(root['work_item_id'], {}).items() if k != 'id'})
                row['labor_norm'] = (norms.norm(metadata[root['work_item_id']], root['work_date'],
                    row['plan_version_id'], use_explicit=False) if root['work_item_id'] in metadata else None)
        scoped_attributes = [r for r in attributes if r.get('work_item_id') in chosen
                             and start <= r['work_date'] <= end and self._executor(r, filters)]
        events = []
        for root in roots.values():
            if root['work_item_id'] not in chosen or not start <= root['work_date'] <= end:
                continue
            item = dict(root)
            item.update({k: v for k, v in chosen[root['work_item_id']].items() if k != 'id'})
            item.update(assigned=exact_sum(assigned[root['id']]), valid=root['id'] not in bad_volume)
            item['remaining'] = subtract(item['available'], item['assigned'])
            events.append(item)

        has_approved = bool(attendance)
        if not has_approved and not filters.get('brigade_id'):
            has_approved = self.db.execute(
                "SELECT 1 FROM timesheets t JOIN timesheet_versions v ON v.timesheet_id=t.id "
                "WHERE t.project_id=? AND v.status='approved' AND v.approved_at<=? "
                "AND printf('%04d-%02d',t.year,t.month) BETWEEN substr(?,1,7) AND substr(?,1,7) "
                "AND (? IS NULL OR t.contractor_id=?) LIMIT 1",
                (self.project_id, cutoff, start, end, filters.get('contractor_id'),
                 filters.get('contractor_id'))).fetchone() is not None
        worked = _total(attendance, 'hours') if has_approved else None
        allocated = _total(scoped_hours, 'hours') if has_approved or scoped_hours else None
        unallocated = subtract(worked, allocated) if worked is not None and allocated is not None else None
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        daily = defaultdict(set)
        daily_hours, daily_allocated = defaultdict(list), defaultdict(list)
        for row in attendance:
            daily_hours[row['work_date']].append(row['hours'])
            if row['hours'] > ZERO:
                daily[row['work_date']].add(row['employee_id'])
        for row in scoped_hours:
            daily_allocated[row['work_date']].append(row['hours'])
        workers = set().union(*daily.values()) if daily else set()
        average = ratio(Decimal(sum(map(len, daily.values()))), Decimal(days)) if has_approved else None
        dynamics, current = [], date.fromisoformat(start)
        for offset in range(days):
            stamp = (current + timedelta(days=offset)).isoformat()
            dynamics.append(dict(date=stamp, hours=exact_sum(daily_hours[stamp]) if has_approved else None,
                allocated_hours=exact_sum(daily_allocated[stamp]) if has_approved or scoped_hours else None,
                workers=len(daily[stamp]) if has_approved else None))

        journal = self._journal(start, end, cutoff, mode)
        general = defaultdict(list)
        for row in journal:
            if row['work_item_id'] in chosen:
                general[self._key(chosen[row['work_item_id']])].append(row)
        hour_groups, attr_groups, work_meta = defaultdict(list), defaultdict(list), {}
        for row in chosen.values():
            work_meta.setdefault(self._key(row), row)
        for row in scoped_hours:
            hour_groups[self._key(row)].append(row)
        for row in scoped_attributes:
            attr_groups[self._key(row)].append(row)
        executor_scope = any(key in filters for key in ('contractor_id', 'brigade_id'))
        self._daily_productivity(dynamics, journal, scoped_hours, scoped_attributes,
                                 chosen, norms, mode, executor_scope, start)
        work_rows = []
        for key, meta in sorted(work_meta.items()):
            hrows, arows, jrows = hour_groups[key], attr_groups[key], general[key]
            hours = _total(hrows, 'hours') if has_approved or hrows else None
            if executor_scope:
                quantity, earned = self._earned(arows, chosen, norms) if mode == 'actual' else (None, None)
            else:
                quantity = _total(jrows, 'quantity')
                earned = self._journal_earned(jrows, chosen, norms)
            conflict = any(not r['valid'] for r in hrows + arows)
            attr_quantity = _total(arows, 'quantity')
            journal_quantity = _total(jrows, 'quantity')
            complete = (has_approved and unallocated == ZERO and hours is not None and hours > ZERO
                        and quantity is not None and earned is not None
                        and mode == 'actual' and attr_quantity == journal_quantity
                        and not any(not r['valid'] for r in arows))
            work_rows.append(_metric(meta, quantity, earned, hours, complete=complete, conflict=conflict,
                reference_output=self._reference_output(hrows, chosen, norms)))

        contractor_names = {r['id']: r['name'] for r in self.db.execute(
            'SELECT c.id,c.name FROM contractors c JOIN contractor_projects p ON p.contractor_id=c.id '
            'WHERE p.project_id=?', (self.project_id,))}
        brigade_names = {r['id']: r['number'] for r in self.db.execute(
            'SELECT b.id,b.number FROM brigades b JOIN contractor_projects p ON p.contractor_id=b.contractor_id '
            'WHERE p.project_id=?', (self.project_id,))}
        contractors = self._entities('contractor_id', contractor_names, scoped_hours, scoped_attributes,
            attendance, chosen, norms, general, mode, has_approved)
        brigades = self._entities('brigade_id', brigade_names, scoped_hours, scoped_attributes,
            attendance, chosen, norms, general, mode, has_approved)
        warnings = []
        selected_bad_time = {self._time_key(r) for r in scoped_hours if not r['valid']}
        selected_bad_volume = {r['fact_event_id'] for r in scoped_attributes if not r['valid']}
        conflicts = len(selected_bad_time) + len(selected_bad_volume)
        if not has_approved:
            warnings.append('Нет утверждённого табеля на выбранный момент знания: часы и численность неизвестны.')
        elif not attendance:
            warnings.append('Утверждённый табель существует, но в выбранном периоде нет заполненных ячеек. Итог часов равен нулю; доступных записей для распределения нет.')
        if conflicts:
            warnings.append('Обнаружены конфликты часов или объёмов ({}). KPI затронутых исполнителей исключены; исправьте распределения.'.format(conflicts))
        if unallocated is not None and unallocated > ZERO:
            warnings.append('Есть нераспределённые часы: {}. Полнота выработки не подтверждена.'.format(unallocated))
        if spatial:
            warnings.append('Фильтр работ ограничивает распределения и объёмы. Табельные часы и численность относятся ко всему объекту для выбранного исполнителя.')
        if mode == 'confirmed':
            warnings.append('Подтверждённый журнал не связан с исполнителями: их подтверждённые объёмы и выработка неизвестны.')
        if any(not row['complete'] for row in work_rows):
            warnings.append('Связь часов и объёмов неполна. Показатели работ предварительные; отсутствие назначения не означает нулевой объём исполнителя.')
        if any(r['quantity'] is not None and r['normative_hours'] is None for r in work_rows):
            warnings.append('Нет совместимого норматива утверждённого плана на бизнес-дату. Текущий справочный норматив не используется.')
        if any(r['coverage'] is not None and r['coverage'] < Decimal(1) for r in contractors + brigades):
            warnings.append('Исполнитель имеет частичное покрытие объёма явными атрибуциями; выработка рассчитана только по назначенному объёму.')
        if any(r['hours'] is not None and r['hours'] > ZERO
               and (r['worked_hours'] is None or r['worked_hours'] < r['hours']) for r in brigades):
            warnings.append('Бригада закреплена в распределении часов. Позднейшее изменение её состава не переносит распределение; табельная численность показана по истории назначений.')
        # Root attribution uses the original production day. A correction dated
        # inside this period against an older root cannot explain this period's
        # executor output, even if an old attribution remains perfectly valid.
        cross_period = {self._key(chosen[r['work_item_id']]) for r in journal
                        if r['work_item_id'] in chosen and r['correction_of_id'] is not None
                        and r['root_work_date'] < start} if mode == 'actual' else set()
        if cross_period:
            warnings.append('В периоде есть корректировки фактов из более раннего периода. Атрибуции относятся к дате исходного факта; выработка исполнителей за период неизвестна.')
            for row in contractors + brigades + work_rows:
                if self._key(row) in cross_period:
                    row.update(actual_output=None, norm_ratio=None, complete=False)
        available_hours = []
        for row in attendance:
            item = dict(row)
            used = _total(by_person[self._time_key(row)], 'hours')
            remaining = subtract(capacity[self._time_key(row)], used)
            valid = self._time_key(row) not in bad_time
            item.update(allocated_hours=used, remaining_hours=remaining, allocated=used,
                        remaining=remaining, valid=valid, conflict=not valid)
            available_hours.append(item)
        normative = (exact_sum(row['normative_hours'] for row in work_rows)
                     if all(row['normative_hours'] is not None for row in work_rows) else None)
        return dict(project=project, start_date=start, end_date=end, cutoff=cutoff,
            mode=mode, discipline=direction, filters=filters,
            summary=dict(worked_hours=worked, allocated_hours=allocated, unallocated_hours=unallocated,
                normative_hours=normative, unique_workers=len(workers) if has_approved else None,
                average_daily_workers=average, has_approved_time=has_approved, conflict_count=conflicts),
            works=work_rows, contractors=contractors, brigades=brigades, allocations=scoped_hours,
            attributions=scoped_attributes, available_hours=available_hours, events=events,
            dynamics=dynamics, warnings=list(dict.fromkeys(warnings)))

    @staticmethod
    def _key(row):
        return row['code'], row['unit'], row['discipline']

    @staticmethod
    def _time_key(row):
        return row['employee_id'], row['work_date'], row['contractor_id']

    def _daily_productivity(self, points, journal, hours, attributes, metadata, norms,
                            mode, executor_scope, start):
        """Earned labor can be summed across unlike natural units, without SQL."""
        daily_hours, daily_attributes, daily_journal = (defaultdict(list) for _ in range(3))
        for row in hours:
            daily_hours[row['work_date']].append(row)
        for row in attributes:
            daily_attributes[row['work_date']].append(row)
        for row in journal:
            if row['work_item_id'] in metadata:
                daily_journal[row['work_date']].append(row)
        for point in points:
            stamp = point['date']
            hrows, arows, jrows = daily_hours[stamp], daily_attributes[stamp], daily_journal[stamp]
            conflict = any(not r['valid'] for r in hrows + arows)
            if executor_scope:
                earned = self._earned(arows, metadata, norms)[1] if mode == 'actual' else None
            else:
                earned = self._journal_earned(jrows, metadata, norms)
            allocated = _total(hrows, 'hours')
            cross_period = mode == 'actual' and any(
                r['correction_of_id'] is not None and r['root_work_date'] < start for r in jrows)
            point['normative_hours'] = None if conflict else earned
            point['norm_ratio'] = (ratio(earned, allocated) if earned is not None and allocated > ZERO
                                   and not conflict and not cross_period else None)

    def _journal(self, start, end, cutoff, mode):
        table, column = ('fact_events', 'work_date') if mode == 'actual' else ('confirmation_events', 'confirmed_date')
        rows = [dict(r, quantity=Decimal(r['quantity_delta'])) for r in self.db.execute(
            'SELECT e.id,e.work_item_id,e.quantity_delta,e.' + column + ' AS work_date,e.correction_of_id '
            'FROM ' + table + ' e JOIN work_items w ON w.id=e.work_item_id '
            'JOIN buildings b ON b.id=w.building_id WHERE b.project_id=? AND e.' + column + '<=? '
            'AND e.recorded_at<=? ORDER BY e.id', (self.project_id, end, cutoff))]
        parents = {r['id']: r for r in rows}
        root_dates = {}
        for row in rows:
            current, chain = row, []
            while current['id'] not in root_dates:
                chain.append(current['id'])
                if current['correction_of_id'] not in parents:
                    root_dates[current['id']] = current['work_date']
                    break
                current = parents[current['correction_of_id']]
            for event_id in chain:
                root_dates[event_id] = root_dates[current['id']]
            row['root_work_date'] = root_dates[row['id']]
        return [r for r in rows if r['work_date'] >= start]

    @staticmethod
    def _journal_earned(rows, metadata, norms):
        parts = []
        for row in rows:
            norm = norms.norm(metadata[row['work_item_id']], row['root_work_date'])
            if norm is None:
                return None
            parts.append(multiply(row['quantity'], norm))
        return exact_sum(parts)

    @staticmethod
    def _earned(rows, metadata, norms):
        # A zero revision revokes an assignment. It preserves history, without
        # creating a measurement that an executor produced zero units.
        rows = [row for row in rows if row['quantity'] > ZERO]
        if not rows or any(not r['valid'] for r in rows):
            return None, None
        parts = []
        for row in rows:
            norm = norms.norm(metadata[row['work_item_id']], row['work_date'], row['plan_version_id'])
            if norm is None:
                return _total(rows, 'quantity'), None
            parts.append(multiply(row['quantity'], norm))
        return _total(rows, 'quantity'), exact_sum(parts)

    @staticmethod
    def _reference_output(hours, metadata, norms):
        """A zero measured volume does not erase a known immutable norm."""
        values = {norms.norm(metadata[r['work_item_id']], r['work_date'], r['plan_version_id'])
                  for r in hours if r['hours'] > ZERO}
        if len(values) == 1 and None not in values:
            return ratio(Decimal(1), values.pop())
        return None

    def _entities(self, field, names, hours, attrs, attendance, metadata, norms, general, mode, has_time):
        hgroups, agroups, timegroups = defaultdict(list), defaultdict(list), defaultdict(list)
        for row in hours:
            if row.get(field) is not None:
                hgroups[(row[field], self._key(row))].append(row)
        for row in attrs:
            if row.get(field) is not None:
                agroups[(row[field], self._key(row))].append(row)
        for row in attendance:
            if row.get(field) is not None:
                timegroups[row[field]].append(row)
        allocated_by_entity = defaultdict(list)
        for (entity, _), rows in hgroups.items():
            allocated_by_entity[entity].append(_total(rows, 'hours'))
        allocated_by_entity = {entity: exact_sum(values) for entity, values in allocated_by_entity.items()}
        result = []
        for entity, key in sorted(set(hgroups) | set(agroups)):
            hrows, arows = hgroups[(entity, key)], agroups[(entity, key)]
            conflict = any(not r['valid'] for r in hrows + arows)
            quantity, earned = self._earned(arows, metadata, norms) if mode == 'actual' else (None, None)
            measured = _total(hrows, 'hours') if has_time or hrows else None
            whole = _total(general[key], 'quantity')
            coverage = ratio(quantity, whole) if quantity is not None and whole > ZERO else None
            available = _total(timegroups[entity], 'hours')
            entity_allocated = allocated_by_entity.get(entity, ZERO)
            hours_coverage = ratio(entity_allocated, available) if available > ZERO else None
            row = _metric(hrows[0] if hrows else arows[0], quantity, earned, measured,
                complete=(mode == 'actual' and has_time and measured is not None and measured > ZERO and bool(arows)
                          and earned is not None and entity_allocated == available and coverage == Decimal(1)),
                conflict=conflict, reference_output=self._reference_output(hrows, metadata, norms))
            row.update(entity_id=entity, entity_name=names.get(entity, str(entity)), coverage=coverage,
                       hours_coverage=hours_coverage, worked_hours=available if has_time else None)
            result.append(row)
        represented = {r['entity_id'] for r in result}
        for entity in sorted(set(timegroups) - represented):
            unknown = dict(code='—', name='Работа не назначена', unit='—', discipline='')
            row = _metric(unknown, None, None, None, complete=False)
            row.update(entity_id=entity, entity_name=names.get(entity, str(entity)),
                       coverage=None, hours_coverage=ZERO,
                       worked_hours=_total(timegroups[entity], 'hours') if has_time else None)
            result.append(row)
        return result
