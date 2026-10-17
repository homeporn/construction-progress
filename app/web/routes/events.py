import secrets
from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.repositories.event_repository import EventRepository
from app.repositories.work_repository import WorkRepository
from app.time_utils import instant
from app.web import get_db, current_project_id
from app.web.forms import InputError, business_date, discipline, integer, quantity, text, item_filters

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
        if correction is None and item_id not in {row["id"] for row in repo.available_work_items(direction)}:
            raise InputError("Позиция не входит в действующий утверждённый план. Утвердите ведомость или выберите корректировку истории.")
        if delta == 0 or (delta < 0 and not correction):
            raise InputError("Объём должен быть ненулевым. Для уменьшения выберите исходное событие корректировки.")
        date = business_date(data, "business_date")
        if date > instant()[:10]:
            raise InputError("Дата события не может быть позже сегодняшней даты регистрации (UTC).")
        kwargs = dict(event_key=text(data, "event_key", "Ключ записи", limit=80),
                      correction_of_id=correction, comment=text(data, "comment", "Комментарий", required=False, limit=2000))
        if confirmation:
            event_repo.add_confirmation(item_id, delta, date,
                                        document_ref=text(data, "document_ref", "Документ-основание", limit=200), **kwargs)
        else:
            event_repo.add_fact(item_id, delta, date, **kwargs)
        flash("Подтверждение зарегистрировано." if confirmation else "Фактическое выполнение зарегистрировано.", "success")
        return redirect(url_for("events.confirmations" if confirmation else "events.facts", discipline=direction.value), code=303)
    filters = item_filters(request.args)
    buildings, floors = repo.list_buildings(), repo.list_floors()
    if filters["building_id"] and filters["building_id"] not in {b["id"] for b in buildings}:
        raise InputError("Корпус не принадлежит текущему объекту.")
    if filters["floor_id"] and not any(f["id"] == filters["floor_id"] and
            (not filters["building_id"] or f["building_id"] == filters["building_id"]) for f in floors):
        raise InputError("Этаж не найден в выбранном корпусе.")
    rows = [r for r in event_repo.list_events(kind) if r["discipline"] == direction.value]
    correction_mode = request.args.get("correction") == "1"
    items = repo.list_work_items(discipline=direction.value, **filters) if correction_mode else repo.available_work_items(direction, **filters)
    if correction_mode:
        ids = {row["work_item_id"] for row in rows}
        items = [item for item in items if item["id"] in ids]
    rows = [r for r in rows if all(value in (None, "") or r[key] == value for key, value in filters.items())]
    catalog = repo.list_catalog()
    return render_template("events.html", confirmation=confirmation, discipline=direction, items=items,
                           buildings=buildings, floors=floors, events=rows, filters=filters,
                           elements=sorted({r["element"] for r in catalog}), work_types=sorted({r["work_type"] for r in catalog}),
                           correction_mode=correction_mode,
                           event_key=secrets.token_urlsafe(24))


@bp.route("/facts", methods=["GET", "POST"])
def facts():
    return journal("fact")


@bp.route("/confirmations", methods=["GET", "POST"])
def confirmations():
    return journal("confirmation")
