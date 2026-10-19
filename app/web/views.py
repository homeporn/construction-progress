from flask import request

from app.calculation_engine.models import GroupProgress, Status
from app.repositories.work_repository import WorkRepository
from app.services.progress_service import ProgressService
from . import get_db, current_project_id
from .forms import InputError, discipline, integer, item_filters, report_dates


def work_context(*, historical=False):
    db = get_db()
    repo = WorkRepository(db, current_project_id())
    direction, filters = discipline(request.args), item_filters(request.args)
    report_date, cutoff = report_dates(request.args)
    floors, buildings, catalog = repo.list_floors(), repo.list_buildings(), repo.list_catalog()
    entry_buildings = [b for b in buildings if b["status"] == "active"]
    active_ids = {b["id"] for b in entry_buildings}
    entry_floors = [f for f in floors if f["building_id"] in active_ids]
    if filters["building_id"] and filters["building_id"] not in {b["id"] for b in buildings}:
        raise InputError("Выбранный корпус не найден.")
    if filters["floor_id"] and not any(f["id"] == filters["floor_id"] and
            (filters["building_id"] is None or f["building_id"] == filters["building_id"]) for f in floors):
        raise InputError("Этаж не найден в выбранном корпусе.")
    versions = repo.list_plan_versions(direction)
    selected_id = integer(request.args, "version_id", "Версия плана", optional=True)
    if selected_id:
        version = repo.get_plan_version(selected_id)
        if version["discipline"] != direction.value:
            raise InputError("Версия плана относится к другому направлению.")
    elif historical:
        version = repo.get_active_plan_version(direction, report_date=report_date, knowledge_cutoff=cutoff)
    else:
        version = versions[0] if versions else None
    preview = bool(version and version["status"] == "DRAFT" and not historical)
    service = ProgressService(db, current_project_id())
    report = service.plan_view(report_date, cutoff, version["id"] if version else None, preview=preview)
    report_items = service.filtered_items(report, filters)
    total = (service.group(report_items) if report and report.items else
             report.total if report and report.total else GroupProgress(status=Status.INVALID if report else Status.NO_DATA))
    catalog_norms={c['id']:dict(c).get('reference_labor_norm') for c in catalog}
    positions=[dict(row,reference_labor_norm=catalog_norms.get(row['catalog_id'])) for row in repo.list_work_items(discipline=direction.value, **filters)]
    return dict(discipline=direction, filters=filters, buildings=buildings, floors=floors,
                entry_buildings=entry_buildings, entry_floors=entry_floors,
                catalog=catalog, elements=sorted({c["element"] for c in catalog}),
                work_types=sorted({c["work_type"] for c in catalog}), versions=versions,
                version=version, report_date=report_date, cutoff=cutoff, report=report,
                results={i.source.work_item_id: i for i in report_items}, total=total,
                items=positions,
                lines={line["work_item_id"]: line for line in repo.list_plan_lines(version["id"])} if version else {},
                preview=preview)
