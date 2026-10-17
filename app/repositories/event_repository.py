from collections import defaultdict
from decimal import Decimal

from app.decimal_utils import ZERO, canonical, exact_sum
from app.time_utils import day, instant
from .base import ValidationError, transaction
from .work_repository import WorkRepository


class EventRepository:
    def __init__(self, db):
        self.db = db

    def add_fact(self, work_item_id, quantity_delta, work_date, *, event_key,
                 recorded_at=None, comment="", correction_of_id=None, unit=None):
        return self._append("fact_events", "work_date", "comment", work_item_id,
                            quantity_delta, work_date, event_key, recorded_at,
                            comment, correction_of_id, unit)

    def add_confirmation(self, work_item_id, quantity_delta, confirmed_date, *, event_key,
                         document_ref, recorded_at=None, correction_of_id=None, unit=None, comment=""):
        return self._append("confirmation_events", "confirmed_date", "document_ref", work_item_id,
                            quantity_delta, confirmed_date, event_key, recorded_at,
                            document_ref, correction_of_id, unit, comment)

    def _append(self, table, date_column, metadata_column, item_id, quantity, business_date,
                event_key, recorded_at, metadata, correction_of_id, unit, confirmation_comment=""):
        # Table/column names above are private constants, never caller SQL.
        quantity, business_date, recorded_at = canonical(quantity), day(business_date), instant(recorded_at)
        with transaction(self.db):
            item = WorkRepository(self.db)._item(item_id)
            columns = "work_item_id,quantity_delta,{},{},recorded_at,correction_of_id,event_key,unit".format(
                date_column, metadata_column)
            values = [item_id, quantity, business_date, metadata, recorded_at,
                      correction_of_id, event_key, item["unit"] if unit is None else unit]
            if table == "confirmation_events":
                columns += ",comment"
                values.append(confirmation_comment)
            cursor = self.db.execute("INSERT INTO {}({}) VALUES ({})".format(
                table, columns, ",".join("?" for _ in values)), values)
            self._validate_timeline(item_id)
            return cursor.lastrowid

    def list_events(self, kind, *, work_item_id=None):
        if kind not in ("fact", "confirmation"):
            raise ValueError("Unknown event journal")
        table = "fact_events" if kind == "fact" else "confirmation_events"
        date_column = "work_date" if kind == "fact" else "confirmed_date"
        return self.db.execute(
            "SELECT e.*,e.{} AS business_date,c.code,c.name,b.name AS building_name,f.floor_code,w.discipline "
            "FROM {} e JOIN work_items w ON w.id=e.work_item_id "
            "JOIN work_catalog c ON c.id=w.catalog_id JOIN buildings b ON b.id=w.building_id "
            "JOIN floors f ON f.id=w.floor_id WHERE (? IS NULL OR e.work_item_id=?) "
            "ORDER BY e.recorded_at DESC,e.id DESC".format(date_column, table),
            (work_item_id, work_item_id)).fetchall()

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
