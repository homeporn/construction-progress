from app.calculation_engine.aggregation import aggregate, group_by
from app.calculation_engine.historical import HistoricalCalculator, read_snapshot
from app.calculation_engine.models import GroupProgress, Status
from app.models import Discipline
from app.repositories.work_repository import WorkRepository


class ProgressService:
    """Select baselines and call the existing engine; no independent formulas."""
    def __init__(self, db):
        self.db = db
        self.work = WorkRepository(db)
        self.calculator = HistoricalCalculator(db)

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

    @staticmethod
    def filtered_items(report, filters):
        if report is None:
            return []
        return [item for item in report.items if all(
            value in (None, "") or getattr(item.source, key) == value for key, value in filters.items())]

    @staticmethod
    def group(items):
        return aggregate(items)
