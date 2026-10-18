from collections import defaultdict
from decimal import Decimal

from app.decimal_utils import ZERO, canonical, exact_sum, subtract
from app.time_utils import day, instant
from .base import ValidationError, transaction
from .work_repository import WorkRepository


class EventRepository:
    def __init__(self, db, project_id=1):
        self.db = db
        self.project_id = project_id

    def _supports_order_links(self):
        # Historical/migration readers can still open a pre-007 database.
        return any(row['name'] == 'work_order_line_id' for row in self.db.execute('PRAGMA table_info(fact_events)'))

    def add_fact(self, work_item_id, quantity_delta, work_date, *, event_key,
                 recorded_at=None, comment="", correction_of_id=None, unit=None,
                 source_type="manual", require_current_plan=False, plan_version_id=None,
                 work_order_line_id=None, allow_assignment_overrun=False, overrun_reason=""):
        return self._append("fact_events", "work_date", "comment", work_item_id,
                            quantity_delta, work_date, event_key, recorded_at,
                            comment, correction_of_id, unit, source_type=source_type,
                            require_current_plan=require_current_plan, plan_version_id=plan_version_id,
                            work_order_line_id=work_order_line_id, allow_assignment_overrun=allow_assignment_overrun,
                            overrun_reason=overrun_reason)

    def add_confirmation(self, work_item_id, quantity_delta, confirmed_date, *, event_key,
                         document_ref, recorded_at=None, correction_of_id=None, unit=None, comment="",
                         source_type="manual", require_current_plan=False, plan_version_id=None, work_order_line_id=None):
        return self._append("confirmation_events", "confirmed_date", "document_ref", work_item_id,
                            quantity_delta, confirmed_date, event_key, recorded_at,
                            document_ref, correction_of_id, unit, comment, source_type,
                            require_current_plan, plan_version_id, work_order_line_id=work_order_line_id)

    def _append(self, table, date_column, metadata_column, item_id, quantity, business_date,
                event_key, recorded_at, metadata, correction_of_id, unit, confirmation_comment="",
                source_type="manual", require_current_plan=False, plan_version_id=None, work_order_line_id=None,
                allow_assignment_overrun=False, overrun_reason=""):
        # Table/column names above are private constants, never caller SQL.
        quantity, business_date = canonical(quantity), day(business_date)
        if correction_of_id is None and Decimal(quantity) <= ZERO:
            raise ValidationError("Обычный объём должен быть положительным; используйте корректировку")
        with transaction(self.db):
            linked_schema = self._supports_order_links()
            if work_order_line_id is not None and not linked_schema:
                raise ValidationError('Сначала обновите схему базы для связи с нарядом')
            # Assign automatic knowledge time after acquiring the writer lock.
            recorded_at = instant(recorded_at)
            work = WorkRepository(self.db, self.project_id)
            work._writable()
            item = work._item(item_id)
            if correction_of_id is not None:
                original = self.db.execute('SELECT ' + ('work_order_line_id' if linked_schema else 'NULL AS work_order_line_id') + ' FROM ' + table + ' WHERE id=? AND work_item_id=?',
                                           (correction_of_id, item_id)).fetchone()
                if original is None:
                    raise ValidationError('Исходное событие корректировки не найдено для выбранной работы')
                if work_order_line_id is not None and work_order_line_id != original['work_order_line_id']:
                    raise ValidationError('Корректировка не может изменять связь исходного события с нарядом')
                work_order_line_id = original['work_order_line_id']
            if correction_of_id is None and item["building_status"] != "active":
                raise ValidationError("Корпус архивирован; доступны только корректировки истории")
            if (require_current_plan or work_order_line_id is not None) and correction_of_id is None:
                current = work.get_active_plan_version(item["discipline"], report_date=recorded_at[:10],
                                                       knowledge_cutoff=recorded_at)
                if current is None or (plan_version_id is not None and current["id"] != plan_version_id):
                    raise ValidationError("Действующая версия плана изменилась или отсутствует. Обновите страницу")
                if not self.db.execute("SELECT 1 FROM plan_lines WHERE plan_version_id=? AND work_item_id=?",
                                       (current["id"], item_id)).fetchone():
                    raise ValidationError("Позиция не входит в действующий утверждённый план")
            if work_order_line_id is not None:
                from .project_repository import clean
                from .work_order_workflow_repository import WorkOrderWorkflowRepository
                overrun_reason = clean(overrun_reason, 'Причина превышения задания', 2000, False)
                if correction_of_id is not None and not (confirmation_comment if table == 'confirmation_events' else metadata).strip():
                    raise ValidationError('Укажите комментарий к корректировке, связанной с нарядом')
                WorkOrderWorkflowRepository(self.db, self.project_id).validate_link(work_order_line_id, item_id,
                    'fact' if table == 'fact_events' else 'confirmation', business_date, recorded_at,
                    correction=correction_of_id is not None, quantity=Decimal(quantity),
                    allow_overrun=allow_assignment_overrun, overrun_reason=overrun_reason)
            if table == "confirmation_events" and correction_of_id is None:
                actual = self.cumulative_quantities("fact", business_date, recorded_at, work_item_id=item_id).get(item_id, ZERO)
                confirmed = self.cumulative_quantities("confirmation", business_date, recorded_at, work_item_id=item_id).get(item_id, ZERO)
                if Decimal(quantity) > subtract(actual, confirmed):
                    raise ValidationError("confirmation exceeds actual: доступный остаток на дату подтверждения "
                                          + format(subtract(actual, confirmed), "f"))
            columns = "work_item_id,quantity_delta,{},{},recorded_at,correction_of_id,event_key,unit,source_type".format(
                date_column, metadata_column)
            values = [item_id, quantity, business_date, metadata, recorded_at,
                      correction_of_id, event_key, item["unit"] if unit is None else unit, source_type]
            if linked_schema:
                columns += ',work_order_line_id'
                values.append(work_order_line_id)
            if table == "confirmation_events":
                columns += ",comment"
                values.append(confirmation_comment)
            elif linked_schema:
                columns += ',overrun_reason'
                values.append(overrun_reason if work_order_line_id is not None else '')
            cursor = self.db.execute("INSERT INTO {}({}) VALUES ({})".format(
                table, columns, ",".join("?" for _ in values)), values)
            self._validate_timeline(item_id)
            if work_order_line_id is not None:
                self._validate_timeline(item_id, work_order_line_id=work_order_line_id)
            return cursor.lastrowid

    def list_events(self, kind, *, work_item_id=None, report_date=None, knowledge_cutoff=None):
        if kind not in ("fact", "confirmation"):
            raise ValueError("Unknown event journal")
        table = "fact_events" if kind == "fact" else "confirmation_events"
        date_column = "work_date" if kind == "fact" else "confirmed_date"
        linked = self._supports_order_links()
        order_fields = 'o.order_number,o.shift_date AS order_shift_date,o.id AS work_order_id ' if linked else 'NULL AS order_number,NULL AS order_shift_date,NULL AS work_order_id '
        order_joins = 'LEFT JOIN work_order_lines l ON l.id=e.work_order_line_id LEFT JOIN work_orders o ON o.id=l.work_order_id ' if linked else ''
        return self.db.execute((
            "SELECT e.*,e.{} AS business_date,c.code,c.name,c.element,c.work_type,"
            "b.name AS building_name,f.floor_code,w.discipline,w.building_id,w.floor_id,"
            + order_fields +
            "FROM {} e JOIN work_items w ON w.id=e.work_item_id "
            "JOIN work_catalog c ON c.id=w.catalog_id JOIN buildings b ON b.id=w.building_id "
            "JOIN floors f ON f.id=w.floor_id " + order_joins + "WHERE b.project_id=? AND (? IS NULL OR e.work_item_id=?) "
            "AND (? IS NULL OR e.{}<=?) AND (? IS NULL OR e.recorded_at<=?) "
            "ORDER BY e.recorded_at DESC,e.id DESC").format(date_column, table, date_column),
            (self.project_id, work_item_id, work_item_id, report_date, day(report_date) if report_date else None,
             knowledge_cutoff, instant(knowledge_cutoff) if knowledge_cutoff else None)).fetchall()

    def cumulative_quantities(self, kind, report_date, knowledge_cutoff, *, discipline=None, work_item_id=None, work_order_line_id=None):
        """Exact shared journal projection; keys also record zero-net event histories."""
        if kind not in ("fact", "confirmation"):
            raise ValueError("Unknown event journal")
        table, date_column = ("fact_events", "work_date") if kind == "fact" else ("confirmation_events", "confirmed_date")
        linked_filter = 'e.work_order_line_id' if self._supports_order_links() else 'NULL'
        if work_order_line_id is not None and linked_filter == 'NULL':
            raise ValidationError('Сначала обновите схему базы для связи с нарядом')
        rows = self.db.execute(
            "SELECT e.work_item_id,e.quantity_delta FROM {} e JOIN work_items w ON w.id=e.work_item_id "
            "JOIN buildings b ON b.id=w.building_id WHERE b.project_id=? AND e.{}<=? AND e.recorded_at<=? "
            "AND (? IS NULL OR w.discipline=?) AND (? IS NULL OR w.id=?) AND (? IS NULL OR {}=?) ORDER BY e.id".format(table, date_column, linked_filter),
            (self.project_id, day(report_date), instant(knowledge_cutoff), discipline, discipline, work_item_id, work_item_id,
             work_order_line_id, work_order_line_id))
        values = defaultdict(list)
        for row in rows:
            values[row["work_item_id"]].append(Decimal(row["quantity_delta"]))
        return {item: exact_sum(deltas) for item, deltas in values.items()}

    def _validate_timeline(self, item_id, *, work_order_line_id=None):
        """Check every business-date / knowledge-time frontier with exact sums.

        This includes later confirmations when a backdated fact correction arrives.
        BEGIN IMMEDIATE serializes concurrent writers through validation and commit.
        """
        field = 'work_order_line_id' if self._supports_order_links() else 'NULL'
        events = self.db.execute((
            "SELECT work_date AS business_date,recorded_at,quantity_delta,'actual' AS kind "
            "FROM fact_events WHERE work_item_id=? AND (? IS NULL OR {field}=?) UNION ALL "
            "SELECT confirmed_date,recorded_at,quantity_delta,'confirmed' "
            "FROM confirmation_events WHERE work_item_id=? AND (? IS NULL OR {field}=?) ORDER BY recorded_at,business_date").format(field=field),
            (item_id, work_order_line_id, work_order_line_id, item_id, work_order_line_id, work_order_line_id)).fetchall()
        by_knowledge = defaultdict(list)
        for event in events:
            by_knowledge[event["recorded_at"]].append(event)
        by_date = defaultdict(lambda: {"actual": [], "confirmed": []})
        for knowledge, batch in sorted(by_knowledge.items()):
            for event in batch:
                by_date[event["business_date"]][event["kind"]].append(Decimal(event["quantity_delta"]))
            actual, confirmed = ZERO, ZERO
            for business, deltas in sorted(by_date.items()):
                actual = exact_sum([actual] + deltas["actual"])
                confirmed = exact_sum([confirmed] + deltas["confirmed"])
                if actual < 0 or confirmed < 0 or (work_order_line_id is None and confirmed > actual):
                    raise ValidationError(
                        "INVALID: negative cumulative volume or confirmation exceeds actual "
                        "at report_date={}, knowledge_cutoff={}".format(business, knowledge))
