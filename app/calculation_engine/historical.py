from contextlib import contextmanager
from decimal import Decimal

from app.decimal_utils import subtract
from app.models import Discipline
from app.time_utils import day, end_of_day, instant
from app.repositories.event_repository import EventRepository
from .aggregation import aggregate
from .models import DateComparison, Issue, Snapshot, Status, WorkInput
from .progress import calculate_item


@contextmanager
def read_snapshot(db):
    """A single SQLite read snapshot covers plan, metadata and both journals."""
    own = not db.in_transaction
    if own:
        db.execute("BEGIN")
    try:
        yield
    finally:
        if own:
            db.rollback()  # read-only transaction


class HistoricalCalculator:
    def __init__(self, db, project_id=1):
        self.db = db
        self.project_id = project_id

    def calculate_as_of(self, report_date, knowledge_cutoff, plan_version_id):
        report_date, knowledge_cutoff = day(report_date), instant(knowledge_cutoff)
        with read_snapshot(self.db):
            return self._calculate(report_date, knowledge_cutoff, plan_version_id)

    def preview_draft_as_of(self, report_date, knowledge_cutoff, plan_version_id):
        """Explicit draft preview; never used to select an active approved baseline."""
        report_date, knowledge_cutoff = day(report_date), instant(knowledge_cutoff)
        with read_snapshot(self.db):
            return self._calculate(report_date, knowledge_cutoff, plan_version_id, allow_draft=True)

    def _calculate(self, report_date, knowledge_cutoff, version_id, allow_draft=False):
        version = self.db.execute("SELECT * FROM plan_versions WHERE id=? AND project_id=?",
                                  (version_id, self.project_id)).fetchone()
        discipline = Discipline(version["discipline"]) if version else None
        available_at = (version["created_at"] if allow_draft and version and version["status"] == "DRAFT"
                        else version["approved_at"] if version else None)
        if (version is None or available_at is None
                or (version["status"] != "APPROVED" and not allow_draft)
                or available_at > knowledge_cutoff or available_at > end_of_day(report_date)):
            return Snapshot(report_date, knowledge_cutoff, version_id, discipline, Status.INVALID,
                            issues=(Issue("PLAN_UNAVAILABLE", "Версия не утверждена или недоступна на дату среза"),))
        rows = self.db.execute(
            "SELECT w.id,w.building_id,w.floor_id,w.discipline,c.element,c.work_type,c.unit," 
            "p.id AS line_id,p.planned_quantity,p.labor_norm,p.unit AS plan_unit "
            "FROM work_items w JOIN work_catalog c ON c.id=w.catalog_id "
            "JOIN buildings b ON b.id=w.building_id "
            "LEFT JOIN plan_lines p ON p.work_item_id=w.id AND p.plan_version_id=? "
            "WHERE w.discipline=? AND b.project_id=? ORDER BY w.id",
            (version_id, discipline.value, self.project_id)).fetchall()
        actual = self._volumes("fact_events", "work_date", discipline, report_date, knowledge_cutoff)
        confirmed = self._volumes("confirmation_events", "confirmed_date", discipline, report_date, knowledge_cutoff)
        results = []
        for row in rows:
            item_id = row["id"]
            # Visible events outside the baseline must invalidate the report even
            # when corrections have brought their cumulative quantity back to zero.
            if row["line_id"] is None and item_id not in actual and item_id not in confirmed:
                continue
            source = WorkInput(
                work_item_id=item_id, discipline=discipline,
                planned_quantity=Decimal(row["planned_quantity"]) if row["line_id"] else None,
                labor_norm=Decimal(row["labor_norm"]) if row["labor_norm"] is not None else None,
                actual=actual.get(item_id, Decimal(0)), confirmed=confirmed.get(item_id, Decimal(0)),
                unit=row["unit"], plan_unit=row["plan_unit"] or row["unit"],
                building_id=row["building_id"], floor_id=row["floor_id"],
                element=row["element"], work_type=row["work_type"], in_plan=row["line_id"] is not None,
                project_id=self.project_id)
            results.append(calculate_item(source))
        total = aggregate(results)
        return Snapshot(report_date, knowledge_cutoff, version_id, discipline, total.status,
                        tuple(results), total, total.issues)

    def _volumes(self, table, date_column, discipline, report_date, knowledge_cutoff):
        kind = "fact" if table == "fact_events" else "confirmation"
        return EventRepository(self.db, self.project_id).cumulative_quantities(
            kind, report_date, knowledge_cutoff, discipline=discipline.value)

    def compare_dates(self, before_date, after_date, knowledge_cutoff, plan_version_id):
        """Same approved baseline and knowledge time: changes reflect events only."""
        before_date, after_date, cutoff = day(before_date), day(after_date), instant(knowledge_cutoff)
        if before_date > after_date:
            raise ValueError("before_date must not follow after_date")
        with read_snapshot(self.db):
            before = self._calculate(before_date, cutoff, plan_version_id)
            after = self._calculate(after_date, cutoff, plan_version_id)
        left = {x.source.work_item_id: x for x in before.items}
        right = {x.source.work_item_id: x for x in after.items}
        deltas = []
        for field in ("actual", "confirmed"):
            deltas.append({item_id: subtract(
                getattr(right[item_id].source, field) if item_id in right else Decimal(0),
                getattr(left[item_id].source, field) if item_id in left else Decimal(0))
                for item_id in sorted(set(left) | set(right))})
        valid = before.status in (Status.VALID, Status.WARNING) and after.status in (Status.VALID, Status.WARNING)
        return DateComparison(
            before, after, deltas[0], deltas[1],
            subtract(after.total.actual_earned_labor, before.total.actual_earned_labor) if valid else None,
            subtract(after.total.confirmed_earned_labor, before.total.confirmed_earned_labor) if valid else None)


def calculate_as_of(report_date, knowledge_cutoff, plan_version_id, *, db, project_id=1):
    return HistoricalCalculator(db, project_id).calculate_as_of(report_date, knowledge_cutoff, plan_version_id)
