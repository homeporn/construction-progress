"""Labor allocation and productivity screens; mutations stay in the repository."""
from datetime import date
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.labor_repository import LaborRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.work_repository import WorkRepository
from app.services.labor_productivity import LaborProductivityService
from app.services.labor_xlsx import export_labor_xlsx
from app.time_utils import end_of_day, instant
from app.web import current_project, current_project_id, get_db
from app.web.forms import InputError, business_date, discipline, integer, quantity, text
from app.web.pagination import paginate

bp = Blueprint("labor", __name__)
TABS = {"overview": "Сводка производительности", "allocations": "Распределение часов",
        "attributions": "Исполнители объёмов", "works": "Выработка по работам",
        "contractors": "Подрядчики", "brigades": "Бригады", "history": "История операций"}
PAGE_SIZE = 50


def repository():
    return LaborRepository(get_db(), current_project_id())


def _revision(data, name="expected_revision"):
    raw = data.get(name, "0")
    if not raw.isascii() or not raw.isdigit() or len(raw) > 9:
        raise InputError("Редакция изменена. Обновите страницу.")
    return int(raw)


def _page():
    raw = request.args.get("page", "1")
    if not raw.isascii() or not raw.isdigit() or len(raw) > 6 or int(raw) < 1:
        raise InputError("Страница: укажите положительный номер.")
    return int(raw)


def _context(*, include_report=True):
    data = request.args
    db, project_id = get_db(), current_project_id()
    today = instant()[:10]
    start = business_date(data, "start_date", default=today[:8] + "01")
    end = business_date(data, "end_date", default=today)
    if end < start or (date.fromisoformat(end) - date.fromisoformat(start)).days > 3660:
        raise InputError("Период: выберите от 1 до 3661 календарного дня.")
    direction = discipline(data)
    mode = data.get("mode", "actual")
    if mode not in ("actual", "confirmed"):
        raise InputError("Выберите фактический или подтверждённый объём.")
    try:
        cutoff = instant(data.get("knowledge_cutoff") or None)
    except (ValueError, TypeError, OverflowError):
        raise InputError("Отсечение знаний: укажите дату и время UTC, например 2026-10-31T18:00:00Z.") from None
    supplied = integer(data, "project_id", "Объект", optional=True)
    if supplied is not None and supplied != project_id:
        raise InputError("Выберите объект через переключатель рабочего пространства.")
    filters = {key: integer(data, key, label, optional=True) for key, label in
               (("building_id", "Корпус"), ("floor_id", "Этаж"), ("catalog_id", "Код работы"),
                ("contractor_id", "Подрядчик"), ("brigade_id", "Бригада"))}
    filters["element"] = text(data, "element", "Конструктив", required=False)
    work = WorkRepository(db, project_id)
    people = PersonnelRepository(db, project_id)
    if filters["building_id"]:
        work._building(filters["building_id"])
    if filters["floor_id"]:
        floor = work._floor(filters["floor_id"])
        if filters["building_id"] and floor["building_id"] != filters["building_id"]:
            raise InputError("Этаж относится к другому корпусу.")
    if filters["contractor_id"]:
        people.contractor(filters["contractor_id"])
    if filters["catalog_id"] and db.execute("SELECT 1 FROM work_catalog WHERE id=?", (filters["catalog_id"],)).fetchone() is None:
        raise InputError("Код работы не найден.")
    if filters["brigade_id"]:
        brigade = BrigadeRepository(db, project_id).get(filters["brigade_id"])
        if filters["contractor_id"] and brigade["contractor_id"] != filters["contractor_id"]:
            raise InputError("Бригада относится к другому подрядчику.")
    plan_version_id = integer(data, "plan_version_id", "Версия плана", optional=True)
    if plan_version_id:
        version = work._version(plan_version_id)
        if version["discipline"] != direction.value or version["status"] != "APPROVED" or version["approved_at"] > cutoff:
            raise InputError("Выберите доступную утверждённую версию плана текущего направления.")
    report = (LaborProductivityService(db, project_id).build(start, end, knowledge_cutoff=cutoff,
        mode=mode, discipline=direction.value, filters=filters, plan_version_id=plan_version_id)
        if include_report else dict(cutoff=cutoff, warnings=[], allocations=[], summary=dict(conflict_count=0)))
    query = dict(start_date=start, end_date=end, discipline=direction.value, mode=mode)
    query.update({key: value for key, value in filters.items() if value not in (None, "")})
    if data.get("knowledge_cutoff"):
        query["knowledge_cutoff"] = cutoff
    if plan_version_id:
        query["plan_version_id"] = plan_version_id
    # Read brigade metadata in one query; list() also loads membership per row.
    brigades = db.execute("SELECT b.id,b.number,b.contractor_id,c.name AS contractor_name FROM brigades b "
        "JOIN contractors c ON c.id=b.contractor_id JOIN contractor_projects cp ON cp.contractor_id=b.contractor_id "
        "WHERE cp.project_id=? ORDER BY c.name,b.number,b.id", (project_id,)).fetchall()
    return dict(report=report, query=query, filters=filters, mode=mode, direction=direction.value,
        start_date=start, end_date=end, knowledge_cutoff=data.get("knowledge_cutoff", ""),
        read_only=bool(data.get("knowledge_cutoff")) or current_project()["status"] != "active",
        filter_buildings=work.list_buildings(), floors=work.list_floors(filters["building_id"]),
        catalog=work.list_catalog(), contractors=people.list_contractors(), brigades=brigades,
        versions=[v for v in work.list_plan_versions(direction) if v["status"] == "APPROVED"],
        plan_version_id=plan_version_id, tabs=TABS, new_key=uuid4().hex)


def _writable(context):
    if context["read_only"] or request.form.get("knowledge_cutoff"):
        raise InputError("Исторический срез доступен только для чтения. Откройте текущие данные для распределения.")
    if request.form.get("workspace_id") != str(current_project_id()):
        raise InputError("Рабочее пространство изменилось. Обновите страницу.")


def _allocation_revision(report, employee_id, work_item_id, work_date):
    return next((r["revision_no"] for r in report["allocations"] if r["employee_id"] == employee_id
                 and r["work_item_id"] == work_item_id and r["work_date"] == work_date), 0)


def _work_items(context, work_date):
    filters = context["filters"]
    return get_db().execute("WITH selected_plan AS (SELECT id FROM plan_versions WHERE project_id=? AND discipline=? "
        "AND status='APPROVED' AND approved_at<=? AND approved_at<=? AND (? IS NULL OR id=?) ORDER BY version_no DESC LIMIT 1) "
        "SELECT w.id,c.code,c.name,c.unit,b.name AS building_name,f.floor_code "
        "FROM work_items w JOIN work_catalog c ON c.id=w.catalog_id JOIN buildings b ON b.id=w.building_id "
        "JOIN floors f ON f.id=w.floor_id JOIN plan_lines pl ON pl.work_item_id=w.id "
        "JOIN selected_plan p ON p.id=pl.plan_version_id WHERE b.project_id=? AND w.discipline=? AND b.status='active' "
        "AND (? IS NULL OR w.building_id=?) AND (? IS NULL OR w.floor_id=?) "
        "AND (? IS NULL OR w.catalog_id=?) AND (?='' OR c.element=?) ORDER BY b.id,f.sort_order,c.code,w.id LIMIT 100",
        (current_project_id(), context["direction"], context["report"]["cutoff"], end_of_day(work_date),
         context["plan_version_id"], context["plan_version_id"], current_project_id(), context["direction"], filters["building_id"], filters["building_id"],
         filters["floor_id"], filters["floor_id"], filters["catalog_id"], filters["catalog_id"],
         filters["element"], filters["element"])).fetchall()


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="labor-bulk-preview-v1")


@bp.get("/labor")
def overview():
    return _show("overview")


@bp.get("/labor/works")
def works():
    return _show("works")


@bp.get("/labor/contractors")
def contractors():
    return _show("contractors")


@bp.get("/labor/brigades")
def brigades():
    return _show("brigades")


def _show(tab):
    context = _context()
    if tab in ("works", "contractors", "brigades"):
        context["metric_rows"] = paginate(context["report"][tab])
    else:
        context["dynamic_rows"] = paginate(context["report"]["dynamics"], "day_page")
    return render_template("labor.html", tab=tab, **context)


@bp.route("/labor/allocations", methods=["GET", "POST"])
def allocations():
    context = _context(include_report=request.method == "GET")
    repo = repository()
    work_date = business_date(request.args, "work_date", default=context["end_date"])
    if not context["start_date"] <= work_date <= context["end_date"]:
        raise InputError("Дата распределения должна входить в выбранный период отчёта.")
    work_item_id = integer(request.args, "work_item_id", "Учётная позиция", optional=True)
    context["query"].update(work_date=work_date)
    if work_item_id:
        context["query"]["work_item_id"] = work_item_id
    available = [dict(r) for r in repo.available_hours(work_date,
        contractor_id=context["filters"]["contractor_id"], brigade_id=context["filters"]["brigade_id"],
        cutoff=context["report"]["cutoff"])] if request.method == "GET" or request.form.get("action") == "bulk_preview" else []
    if request.method == "POST":
        _writable(context)
        data = request.form
        action = text(data, "action", "Действие")
        if action == "bulk_confirm":
            try:
                payload = _serializer().loads(text(data, "preview_token", "Предварительный просмотр", limit=30000), max_age=1800)
            except (BadSignature, SignatureExpired):
                raise InputError("Предварительный просмотр устарел. Сформируйте его заново.") from None
            if payload["project_id"] != current_project_id() or payload["query"] != context["query"]:
                raise InputError("Рабочее пространство или фильтры изменились. Сформируйте просмотр заново.")
            if data.get("confirm_changes") != "1":
                raise InputError("Подтвердите применение перечисленных распределений.")
            repo.allocate(payload["work_item_id"], payload["work_date"],
                [{key: row[key] for key in ("employee_id", "hours", "expected_revision", "expected_timesheet_entry_id", "expected_brigade_id")} for row in payload["rows"]],
                operation_key=payload["operation_key"], reason=payload["reason"], plan_version_id=context["plan_version_id"])
        elif action in ("allocate", "bulk_preview"):
            target = integer(data, "work_item_id", "Учётная позиция")
            when = business_date(data, "work_date")
            if when != work_date or target != work_item_id:
                raise InputError("Позиция или дата изменились. Выберите их и обновите список часов.")
            reason = text(data, "reason", "Причина изменения", required=False, limit=2000)
            key = text(data, "operation_key", "Ключ операции", limit=100)
            if action == "allocate":
                rows = [dict(employee_id=integer(data, "employee_id", "Работник"),
                    hours=str(quantity(data, "hours", "Часы")), expected_revision=_revision(data))]
                if data.get("expected_timesheet_entry_id"):
                    rows[0]["expected_timesheet_entry_id"] = integer(data, "expected_timesheet_entry_id", "Утверждённая запись табеля")
                repo.allocate(target, when, rows, operation_key=key, reason=reason, plan_version_id=context["plan_version_id"])
            else:
                if not context["filters"]["brigade_id"]:
                    raise InputError("Для пакетного распределения выберите исторический состав бригады.")
                selected = data.getlist("employee_id")
                if not selected or len(selected) > PAGE_SIZE or len(set(selected)) != len(selected):
                    raise InputError("Выберите от 1 до 50 разных работников на текущей странице.")
                context["report"]["allocations"] = repo.hour_rows(context["report"]["cutoff"], when, when)
                by_employee = {row["employee_id"]: row for row in available}
                rows = []
                for value in selected:
                    employee_id = integer({"id": value}, "id", "Работник")
                    source = by_employee.get(employee_id)
                    if source is None:
                        raise InputError("Работник отсутствует в историческом составе с утверждёнными часами.")
                    amount = quantity(data, "hours_" + value, "Часы работника")
                    revision = _revision(data, "revision_" + value)
                    if revision != _allocation_revision(context["report"], employee_id, target, when):
                        raise InputError("Распределение изменилось. Обновите список перед предварительным просмотром.")
                    if integer(data, "entry_" + value, "Утверждённая запись табеля") != source["entry_id"]:
                        raise InputError("Утверждённый табель изменился. Обновите список работников.")
                    if revision and not reason:
                        raise InputError("Для исправления распределения укажите причину перед просмотром.")
                    if not revision and not amount:
                        raise InputError("Новое распределение должно содержать положительные часы.")
                    existing = next((Decimal(str(r["hours"])) for r in context["report"]["allocations"]
                        if r["employee_id"] == employee_id and r["work_item_id"] == target and r["work_date"] == when), Decimal(0))
                    if amount > Decimal(str(source["remaining"])) + existing and not (revision and reason and amount < existing):
                        raise InputError("Распределение превышает доступные часы работника.")
                    rows.append(dict(employee_id=employee_id, full_name=source["full_name"], hours=str(amount),
                                     expected_revision=revision, expected_timesheet_entry_id=source["entry_id"],
                                     expected_brigade_id=source["brigade_id"]))
                payload = dict(project_id=current_project_id(), query=context["query"], work_item_id=target,
                    work_date=when, operation_key=key, reason=reason, rows=rows)
                return render_template("labor_preview.html", tab="allocations", preview=payload,
                    preview_token=_serializer().dumps(payload), **context)
        else:
            raise InputError("Неизвестное действие распределения.")
        flash("Распределение часов сохранено. Утверждённые часы и факты сохранены в исходном виде.", "success")
        return redirect(url_for("labor.allocations", **context["query"]), code=303)
    current_allocations = [r for r in context["report"]["allocations"]
                           if r["work_item_id"] == work_item_id and r["work_date"] == work_date]
    assigned = {row["employee_id"]: row for row in current_allocations}
    for row in available:
        old = assigned.get(row["employee_id"], {})
        row["revision"] = old.get("revision_no", 0)
        row["assigned_hours"] = old.get("hours", Decimal(0))
    return render_template("labor_allocations.html", tab="allocations", work_date=work_date,
        work_item_id=work_item_id, work_items=_work_items(context, work_date), available=paginate(available),
        allocation_rows=paginate(current_allocations, "allocation_page"), **context)


@bp.route("/labor/attributions", methods=["GET", "POST"])
def attributions():
    context = _context(include_report=request.method == "GET")
    if request.method == "POST":
        _writable(context)
        data = request.form
        repository().attribute(integer(data, "fact_event_id", "Исходный факт"),
            integer(data, "contractor_id", "Подрядчик"), quantity(data, "quantity", "Назначенный объём"),
            brigade_id=integer(data, "brigade_id", "Бригада", optional=True),
            employee_id=integer(data, "employee_id", "Работник", optional=True),
            expected_revision=_revision(data), operation_key=text(data, "operation_key", "Ключ операции", limit=100),
            reason=text(data, "reason", "Причина изменения", required=False, limit=2000),
            plan_version_id=context["plan_version_id"])
        flash("Исполнитель объёма сохранён. Исходный факт не изменён.", "success")
        return redirect(url_for("labor.attributions", **context["query"]), code=303)
    return render_template("labor_attributions.html", tab="attributions",
        events=paginate(context["report"]["events"]),
        attribution_rows=paginate(context["report"]["attributions"], "attribution_page"), **context)


@bp.get("/labor/history")
def history():
    context = _context(include_report=False)
    page = _page()
    rows = repository().history(limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE,
                                cutoff=context["report"]["cutoff"])
    # Repository fetches only this page and one sentinel row.
    has_next = len(rows) > PAGE_SIZE
    return render_template("labor_history.html", tab="history", history_rows=rows[:PAGE_SIZE],
        page=page, has_next=has_next, **context)


@bp.get("/labor/export.xlsx")
def export():
    report = _context()["report"]
    return send_file(BytesIO(export_labor_xlsx(report)), as_attachment=True,
        download_name="Производительность_{}_{}.xlsx".format(report["start_date"], report["end_date"]),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
