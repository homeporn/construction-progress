from app.calculation_engine.aggregation import aggregate, group_by
from app.calculation_engine.historical import HistoricalCalculator, read_snapshot
from app.calculation_engine.models import GroupProgress, Status
from app.models import Discipline
from app.decimal_utils import subtract
from app.repositories.event_repository import EventRepository
from app.repositories.work_repository import WorkRepository


class ProgressService:
    """Select baselines and call the existing engine; no independent formulas."""
    def __init__(self, db, project_id=1):
        self.db = db
        self.work = WorkRepository(db, project_id)
        self.calculator = HistoricalCalculator(db, project_id)

    def dashboard(self, report_date, knowledge_cutoff):
        with read_snapshot(self.db):
            buildings = self.work.list_buildings()
            sections = []
            for discipline in (Discipline.CONSTRUCTION, Discipline.DESIGN):
                version = self.work.get_active_plan_version(
                    discipline, report_date=report_date, knowledge_cutoff=knowledge_cutoff)
                report = self.calculator.calculate_as_of(report_date, knowledge_cutoff, version["id"]) if version else None
                total = report.total if report and report.total else GroupProgress(status=Status.NO_DATA)
                groups = group_by(report.items, "building_id") if report else {}
                sections.append(dict(discipline=discipline, version=version, report=report, total=total,
                                     buildings={b["id"]: groups.get((discipline, b["id"]),
                                                 GroupProgress(status=Status.NO_DATA)) for b in buildings}))
            errors = [issue for section in sections if section["report"]
                      for issue in section["report"].issues if issue.status == Status.INVALID]
            return dict(buildings=buildings, sections=sections, errors=errors)

    def plan_view(self, report_date, knowledge_cutoff, version_id, *, preview=False):
        if version_id is None:
            return None
        calculate = self.calculator.preview_draft_as_of if preview else self.calculator.calculate_as_of
        return calculate(report_date, knowledge_cutoff, version_id)

    def event_rows(self, report, *, filters=None, confirmation=False, pending_only=False):
        """Derived work queues, never copies of work items or independent progress math."""
        if report is None or not report.items:
            return []
        filters = filters or {}
        with read_snapshot(self.db):
            metadata = {row["id"]: dict(row) for row in self.work.list_work_items(
                discipline=report.discipline.value if report.discipline else None, **filters)}
            actual_ids = EventRepository(self.db, self.work.project_id).cumulative_quantities(
                "fact", report.report_date, report.knowledge_cutoff,
                discipline=report.discipline.value if report.discipline else None)
            rows = []
            for result in self.filtered_items(report, filters):
                source = result.source
                if not source.in_plan or source.work_item_id not in metadata:
                    continue
                if confirmation and source.work_item_id not in actual_ids:
                    continue
                pending = subtract(source.actual, source.confirmed)
                if pending_only and pending <= 0:
                    continue
                row = metadata[source.work_item_id]
                row.update(planned_quantity=source.planned_quantity, actual=source.actual,
                           confirmed=source.confirmed, remaining=result.remaining_quantity,
                           pending=pending, result=result)
                rows.append(row)
            return rows

    @staticmethod
    def filtered_items(report, filters):
        if report is None:
            return []
        return [item for item in report.items if all(
            value in (None, "") or getattr(item.source, key) == value for key, value in filters.items())]

    @staticmethod
    def group(items):
        return aggregate(items)
