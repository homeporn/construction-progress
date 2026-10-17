import secrets
from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.repositories.event_repository import EventRepository
from app.repositories.work_repository import WorkRepository
from app.services.progress_service import ProgressService
from app.calculation_engine.historical import read_snapshot
from app.calculation_engine.models import Status
from app.time_utils import instant
from app.web import get_db, current_project_id, current_project
from app.web.forms import InputError, business_date, discipline, integer, quantity, text
from app.web.views import work_context

bp = Blueprint("events", __name__)


def journal(kind):
    repo, event_repo = WorkRepository(get_db(), current_project_id()), EventRepository(get_db(), current_project_id())
    direction = discipline(request.args if request.method == "GET" else request.form)
    confirmation = kind == "confirmation"
    if request.method == "POST":
        data = request.form
        item_id = integer(data, "work_item_id", "Учётная позиция")
        building_id, floor_id = integer(data, "building_id", "Корпус"), integer(data, "floor_id", "Этаж")
        items = repo.list_work_items(discipline=direction.value, building_id=building_id, floor_id=floor_id)
        if item_id not in {row["id"] for row in items}:
            raise InputError("Позиция не относится к выбранному направлению, корпусу или этажу.")
        delta = quantity(data, "quantity_delta", "Объём", signed=True)
        correction = integer(data, "correction_of_id", "Исходное событие", optional=True)
        if delta == 0 or (delta < 0 and not correction):
            raise InputError("Объём должен быть ненулевым. Для уменьшения выберите исходное событие корректировки.")
        date = business_date(data, "business_date")
        if date > instant()[:10]:
            raise InputError("Дата события не может быть позже сегодняшней даты регистрации (UTC).")
        kwargs = dict(event_key=text(data, "event_key", "Ключ записи", limit=80),
                      correction_of_id=correction, comment=text(data, "comment", "Комментарий", required=False, limit=2000),
                      source_type="manual", require_current_plan=True,
                      plan_version_id=integer(data, "version_id", "Версия плана", optional=True))
        if confirmation:
            event_repo.add_confirmation(item_id, delta, date,
                                        document_ref=text(data, "document_ref", "Документ-основание", limit=200), **kwargs)
        else:
            event_repo.add_fact(item_id, delta, date, **kwargs)
        flash("Подтверждение зарегистрировано." if confirmation else "Фактическое выполнение зарегистрировано.", "success")
        return redirect(url_for("events.confirmations" if confirmation else "events.facts", discipline=direction.value), code=303)
    with read_snapshot(get_db()):
        context = work_context(historical=True)
        filters, report, version = context["filters"], context["report"], context["version"]
        tab = request.args.get("tab", "pending")
        if tab not in ("pending", "all"):
            raise InputError("Выберите вкладку «Ожидают подтверждения» или «Все работы».")
        correction_mode = request.args.get("correction") == "1"
        service = ProgressService(get_db(), current_project_id())
        queue_rows = service.event_rows(report, filters=filters, confirmation=confirmation,
                                        pending_only=confirmation and tab == "pending")
        current = repo.get_active_plan_version(direction, report_date=instant()[:10], knowledge_cutoff=instant())
        read_only = (context["report_date"] != instant()[:10] or bool(request.args.get("knowledge_cutoff"))
                     or current_project()["status"] != "active"
                     or (not correction_mode and (not version or not current or version["id"] != current["id"])))
        if correction_mode:
            rows = event_repo.list_events(kind, report_date=context["report_date"], knowledge_cutoff=context["cutoff"])
            ids = {row["work_item_id"] for row in rows}
            items = [dict(item) for item in context["items"] if item["id"] in ids]
        else:
            active_buildings = {b["id"] for b in context["buildings"] if b["status"] == "active"}
            items = [row for row in queue_rows if row["result"].status in (Status.VALID, Status.WARNING)
                     and row["building_id"] in active_buildings and (not confirmation or row["pending"] > 0)]
        selected_id = integer(request.args, "work_item_id", "Учётная позиция", optional=True)
        selected = next((row for row in items if row["id"] == selected_id), None)
        if selected_id and selected is None:
            raise InputError("Выбранная работа недоступна в этом объекте, версии или срезе.")
        rows = [row for row in event_repo.list_events(kind, report_date=context["report_date"], knowledge_cutoff=context["cutoff"])
                if row["discipline"] == direction.value and all(value in (None, "") or row[key] == value for key, value in filters.items())]
        query = {key: value for key, value in request.args.items() if key in
                 ("discipline", "version_id", "report_date", "knowledge_cutoff", "building_id", "floor_id", "element", "work_type")}
        context.update(confirmation=confirmation, items=items, events=rows, queue_rows=queue_rows,
                       tab=tab, correction_mode=correction_mode, read_only=read_only, selected=selected,
                       query=query, event_key=secrets.token_urlsafe(24))
        return render_template("events.html", **context)


@bp.route("/facts", methods=["GET", "POST"])
def facts():
    return journal("fact")


@bp.route("/confirmations", methods=["GET", "POST"])
def confirmations():
    return journal("confirmation")
