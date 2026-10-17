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

    def add_fact(self, work_item_id, quantity_delta, work_date, *, event_key,
                 recorded_at=None, comment="", correction_of_id=None, unit=None,
                 source_type="manual", require_current_plan=False, plan_version_id=None):
        return self._append("fact_events", "work_date", "comment", work_item_id,
                            quantity_delta, work_date, event_key, recorded_at,
                            comment, correction_of_id, unit, source_type=source_type,
                            require_current_plan=require_current_plan, plan_version_id=plan_version_id)

    def add_confirmation(self, work_item_id, quantity_delta, confirmed_date, *, event_key,
                         document_ref, recorded_at=None, correction_of_id=None, unit=None, comment="",
                         source_type="manual", require_current_plan=False, plan_version_id=None):
        return self._append("confirmation_events", "confirmed_date", "document_ref", work_item_id,
                            quantity_delta, confirmed_date, event_key, recorded_at,
                            document_ref, correction_of_id, unit, comment, source_type,
                            require_current_plan, plan_version_id)

    def _append(self, table, date_column, metadata_column, item_id, quantity, business_date,
                event_key, recorded_at, metadata, correction_of_id, unit, confirmation_comment="",
                source_type="manual", require_current_plan=False, plan_version_id=None):
        # Table/column names above are private constants, never caller SQL.
        quantity, business_date = canonical(quantity), day(business_date)
        if correction_of_id is None and Decimal(quantity) <= ZERO:
            raise ValidationError("Обычный объём должен быть положительным; используйте корректировку")
        with transaction(self.db):
            # Assign automatic knowledge time after acquiring the writer lock.
            recorded_at = instant(recorded_at)
            work = WorkRepository(self.db, self.project_id)
            work._writable()
            item = work._item(item_id)
            if correction_of_id is None and item["building_status"] != "active":
                raise ValidationError("Корпус архивирован; доступны только корректировки истории")
            if require_current_plan and correction_of_id is None:
                current = work.get_active_plan_version(item["discipline"], report_date=recorded_at[:10],
                                                       knowledge_cutoff=recorded_at)
                if current is None or (plan_version_id is not None and current["id"] != plan_version_id):
                    raise ValidationError("Действующая версия плана изменилась или отсутствует. Обновите страницу")
                if not self.db.execute("SELECT 1 FROM plan_lines WHERE plan_version_id=? AND work_item_id=?",
                                       (current["id"], item_id)).fetchone():
                    raise ValidationError("Позиция не входит в действующий утверждённый план")
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
            if table == "confirmation_events":
                columns += ",comment"
                values.append(confirmation_comment)
            cursor = self.db.execute("INSERT INTO {}({}) VALUES ({})".format(
                table, columns, ",".join("?" for _ in values)), values)
            self._validate_timeline(item_id)
            return cursor.lastrowid

    def list_events(self, kind, *, work_item_id=None, report_date=None, knowledge_cutoff=None):
        if kind not in ("fact", "confirmation"):
            raise ValueError("Unknown event journal")
        table = "fact_events" if kind == "fact" else "confirmation_events"
        date_column = "work_date" if kind == "fact" else "confirmed_date"
        return self.db.execute(
            "SELECT e.*,e.{} AS business_date,c.code,c.name,c.element,c.work_type,"
            "b.name AS building_name,f.floor_code,w.discipline,w.building_id,w.floor_id "
            "FROM {} e JOIN work_items w ON w.id=e.work_item_id "
            "JOIN work_catalog c ON c.id=w.catalog_id JOIN buildings b ON b.id=w.building_id "
            "JOIN floors f ON f.id=w.floor_id WHERE b.project_id=? AND (? IS NULL OR e.work_item_id=?) "
            "AND (? IS NULL OR e.{}<=?) AND (? IS NULL OR e.recorded_at<=?) "
            "ORDER BY e.recorded_at DESC,e.id DESC".format(date_column, table, date_column),
            (self.project_id, work_item_id, work_item_id, report_date, day(report_date) if report_date else None,
             knowledge_cutoff, instant(knowledge_cutoff) if knowledge_cutoff else None)).fetchall()

    def cumulative_quantities(self, kind, report_date, knowledge_cutoff, *, discipline=None, work_item_id=None):
        """Exact shared journal projection; keys also record zero-net event histories."""
        if kind not in ("fact", "confirmation"):
            raise ValueError("Unknown event journal")
        table, date_column = ("fact_events", "work_date") if kind == "fact" else ("confirmation_events", "confirmed_date")
        rows = self.db.execute(
            "SELECT e.work_item_id,e.quantity_delta FROM {} e JOIN work_items w ON w.id=e.work_item_id "
            "JOIN buildings b ON b.id=w.building_id WHERE b.project_id=? AND e.{}<=? AND e.recorded_at<=? "
            "AND (? IS NULL OR w.discipline=?) AND (? IS NULL OR w.id=?) ORDER BY e.id".format(table, date_column),
            (self.project_id, day(report_date), instant(knowledge_cutoff), discipline, discipline, work_item_id, work_item_id))
        values = defaultdict(list)
        for row in rows:
            values[row["work_item_id"]].append(Decimal(row["quantity_delta"]))
        return {item: exact_sum(deltas) for item, deltas in values.items()}

    def _validate_timeline(self, item_id):
        """Check every business-date / knowledge-time frontier with exact sums.

        This includes later confirmations when a backdated fact correction arrives.
        BEGIN IMMEDIATE serializes concurrent writers through validation and commit.
        """
        events = self.db.execute(
            "SELECT work_date AS business_date,recorded_at,quantity_delta,'actual' AS kind "
            "FROM fact_events WHERE work_item_id=? UNION ALL "
            "SELECT confirmed_date,recorded_at,quantity_delta,'confirmed' "
            "FROM confirmation_events WHERE work_item_id=? ORDER BY recorded_at,business_date",
            (item_id, item_id)).fetchall()
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
                if actual < 0 or confirmed < 0 or confirmed > actual:
                    raise ValidationError(
                        "INVALID: negative cumulative volume or confirmation exceeds actual "
                        "at report_date={}, knowledge_cutoff={}".format(business, knowledge))
