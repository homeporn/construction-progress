"""Local timesheet screens; all validation and totals belong to the repository."""
from datetime import date, timedelta
from io import BytesIO
import json
from uuid import uuid4

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for

from app.repositories.base import ValidationError
from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.timesheet_repository import TimesheetRepository, decimal_hours
from app.services.timesheet_xlsx import (
    MAX_UPLOAD, export_timesheet_xlsx, make_timesheet_template, parse_timesheet_xlsx,
)
from app.web import current_project_id, get_db
from app.web.forms import business_date, integer, text

bp = Blueprint("timesheets", __name__)
STATUS_LABELS = {"draft": "Черновик", "submitted": "Передан на проверку",
                 "approved": "Утверждён", "cancelled": "Отменён"}
EVENT_LABELS = {"created": "Создан черновик", "workers_added": "Добавлены работники",
    "entries_changed": "Изменены записи", "worker_note": "Изменено примечание работника",
    "comment": "Изменён комментарий табеля",
    "submit": "Передан на проверку", "approve": "Утверждён", "return": "Возвращён в черновик",
    "cancel": "Версия отменена", "correction_created": "Создана корректирующая версия",
    "bulk_applied": "Применено массовое заполнение", "copy_applied": "Применено копирование",
    "import_applied": "Применён импорт Excel", "preview_cancelled": "Предварительный просмотр отменён"}


def repository():
    return TimesheetRepository(get_db(), current_project_id())


def revision(data):
    value = data.get("revision", "")
    if not value.isascii() or not value.isdigit() or len(value) > 10:
        raise ValidationError("Редакция табеля не определена. Обновите страницу.")
    return int(value)


def selected_ids(data, name="employee_id"):
    values = data.getlist(name)
    return [integer({"value": item}, "value", "Работник") for item in values]


def operation_key(data):
    return text(data, "operation_key", "Идентификатор операции", limit=100)


def hours(data):
    # Decimal parsing, precision and code/hour compatibility are checked by the
    # repository. Empty hours remains distinct from an explicitly entered zero.
    value = text(data, "hours", "Часы", required=False, limit=40)
    return value.replace(",", ".") if value else None


def date_range(start, end):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first or (last - first).days > 30:
        raise ValidationError("Выберите диапазон не более одного календарного месяца.")
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def common_context():
    return dict(timesheet_status_labels=STATUS_LABELS, new_key=uuid4().hex)


def present_events(report):
    """Translate the audit for readers; do not expose raw implementation JSON."""
    for event in report["events"]:
        event["label"] = EVENT_LABELS.get(event["operation"], "Действие с табелем")
        details = json.loads(event["details"])
        event["summary"] = details.get("reason", "")
        event["changes"] = []
        if "old_status" in details:
            event["summary"] = "{} → {}. {}".format(STATUS_LABELS.get(details["old_status"], ""),
                STATUS_LABELS.get(details.get("new_status"), ""), details.get("reason", ""))
        if event["operation"] == "created":
            event["summary"] = "Период: {:02d}.{}".format(details["month"], details["year"])
        elif event["operation"] == "workers_added":
            event["summary"] = "ID работников: " + ", ".join(str(value) for value in details.get("employee_ids", []))
        elif event["operation"] == "worker_note":
            event["summary"] = "Работник ID {}. Прежде: {}. Теперь: {}".format(
                details.get("employee_id"), details.get("before", ""), details.get("after", ""))
        elif event["operation"] == "comment":
            event["summary"] = "Прежде: {}. Теперь: {}".format(details.get("before", ""), details.get("after", ""))
        elif event["operation"] == "correction_created":
            event["summary"] = "Основание: утверждённая версия ID {}. {}".format(
                details.get("base_version_id"), details.get("reason", ""))
        for phase, label in (("before", "До изменения"), ("after", "После изменения")):
            if not isinstance(details.get(phase), list):
                continue
            for change in details[phase]:
                event["changes"].append(dict(phase=label, employee_id=change.get("employee_id"),
                    work_date=change.get("work_date", ""), code=change.get("code_snapshot", change.get("code", "")),
                    hours=decimal_hours(change["hours_nanos"]) if "hours_nanos" in change else change.get("hours"),
                    comment="Запись очищена" if change.get("clear") else change.get("comment", "")))


@bp.get("/timesheets")
def index():
    contractor_id = integer(request.args, "contractor_id", "Подрядчик", optional=True)
    history = request.args.get("history") == "1"
    people = PersonnelRepository(get_db(), current_project_id())
    return render_template("timesheets.html", sheets=repository().list_timesheets(
        history=history, contractor_id=contractor_id), contractors=people.list_contractors(),
        contractor_id=contractor_id, history=history, **common_context())


@bp.route("/timesheets/new", methods=["GET", "POST"])
def create():
    if request.method == "POST":
        sheet_id = repository().create(
            integer(request.form, "contractor_id", "Подрядчик"),
            integer(request.form, "year", "Год"), integer(request.form, "month", "Месяц"),
            comment=text(request.form, "comment", "Комментарий", required=False, limit=2000),
            operation_key=operation_key(request.form))
        flash("Черновик создан. Список работников загружен без явок и часов.", "success")
        return redirect(url_for("timesheets.detail", timesheet_id=sheet_id), code=303)
    now = date.today()
    return render_template("timesheet_create.html", contractors=PersonnelRepository(
        get_db(), current_project_id()).list_contractors(active_only=True),
        year=now.year, month=now.month, **common_context())


@bp.route("/timesheets/<int:timesheet_id>", methods=["GET", "POST"])
def detail(timesheet_id):
    repo = repository()
    data = request.form if request.method == "POST" else request.args
    version_id = integer(data, "version_id", "Версия табеля", optional=True)
    report = repo.report(timesheet_id, version_id=version_id)
    if request.method == "POST":
        action = text(data, "action", "Действие")
        expected = revision(data)
        key = operation_key(data)
        version_id = report["version"]["id"]
        if action == "cell":
            change = dict(employee_id=integer(data, "employee_id", "Работник"),
                          work_date=business_date(data, "work_date"),
                          code=text(data, "code", "Код явки", required=False, limit=20),
                          hours=hours(data), clear=data.get("clear") == "1",
                          comment=text(data, "comment", "Комментарий", required=False, limit=2000))
            repo.set_entries(timesheet_id, [change], version_id=version_id,
                             expected_revision=expected, operation_key=key)
        elif action == "add_workers":
            repo.add_workers(timesheet_id, selected_ids(data), version_id=version_id,
                             expected_revision=expected, operation_key=key)
        elif action == "worker_note":
            repo.set_worker_note(timesheet_id, integer(data, "employee_id", "Работник"),
                text(data, "note", "Примечание работника", required=False, limit=2000),
                version_id=version_id, expected_revision=expected, operation_key=key)
        elif action == "comment":
            repo.set_comment(timesheet_id,
                text(data, "comment", "Комментарий табеля", required=False, limit=4000),
                version_id=version_id, expected_revision=expected, operation_key=key)
        elif action in ("bulk_preview", "copy_preview"):
            if expected != report["version"]["revision"]:
                raise ValidationError("Табель изменён в другом окне. Обновите страницу.")
            if action == "bulk_preview":
                preview_id = repo.preview_bulk(
                    timesheet_id, selected_ids(data), business_date(data, "start_date"),
                    business_date(data, "end_date"), text(data, "code", "Код явки", limit=20),
                    hours(data), comment=text(data, "comment", "Комментарий", required=False, limit=2000),
                    brigade_id=integer(data, "brigade_id", "Бригада", optional=True), version_id=version_id,
                    expected_revision=expected)
            else:
                target = date_range(business_date(data, "target_start"), business_date(data, "target_end"))
                preview_id = repo.preview_copy(timesheet_id, business_date(data, "source_date"), target,
                    employee_ids=selected_ids(data) or None, version_id=version_id, expected_revision=expected)
            return redirect(url_for("timesheets.preview", timesheet_id=timesheet_id,
                                    preview_id=preview_id), code=303)
        elif action == "correction":
            new_version = repo.create_correction(timesheet_id,
                text(data, "reason", "Причина корректировки", limit=2000),
                version_id=version_id, expected_revision=expected, operation_key=key)
            flash("Создана корректирующая версия. Первоначальный утверждённый табель сохранён.", "success")
            return redirect(url_for("timesheets.detail", timesheet_id=timesheet_id,
                                    version_id=new_version), code=303)
        elif action in ("submit", "approve", "return", "cancel"):
            repo.transition(timesheet_id, action, version_id=version_id,
                reason=text(data, "reason", "Комментарий к действию", required=False, limit=2000),
                expected_revision=expected, operation_key=key)
        else:
            raise ValidationError("Неизвестное действие с табелем.")
        flash("Изменения табеля сохранены.", "success")
        return redirect(url_for("timesheets.detail", timesheet_id=timesheet_id, version_id=version_id), code=303)
    brigade_repo = BrigadeRepository(get_db(), current_project_id())
    brigade_id = integer(request.args, "brigade_id", "Бригада", optional=True)
    as_of = business_date(request.args, "as_of", default=report["days"][0])
    bulk_workers = report["workers"]
    if brigade_id:
        brigade = brigade_repo.get(brigade_id)
        if brigade["contractor_id"] != report["timesheet"]["contractor_id"]:
            raise ValidationError("Бригада относится к другому подрядчику.")
        members = {r["employee_id"] for r in brigade_repo.members(brigade_id, as_of=as_of)}
        bulk_workers = [worker for worker in bulk_workers if worker["employee_id"] in members]
    present_events(report)
    live_versions = [version for version in report["versions"] if version["status"] != "cancelled"]
    current_version = live_versions[0] if live_versions else report["versions"][0]
    from app.web.pagination import paginate
    report["worker_count"]=len(report["workers"])
    report["workers"]=paginate(report["workers"],"worker_page")
    report["eligible_workers"]=paginate(report["eligible_workers"],"eligible_page")
    bulk_workers=paginate(bulk_workers,"bulk_page")
    return render_template("timesheet_detail.html", report=report,
        brigades=brigade_repo.list(contractor_id=report["timesheet"]["contractor_id"]),
        brigade_id=brigade_id, as_of=as_of, bulk_workers=bulk_workers,
        current_version=current_version,
        weekend_days={day for day in report["days"] if date.fromisoformat(day).weekday() >= 5},
        **common_context())


@bp.route("/timesheets/<int:timesheet_id>/preview/<preview_id>", methods=["GET", "POST"])
def preview(timesheet_id, preview_id):
    repo = repository()
    planned = repo.get_preview(timesheet_id, preview_id)
    if request.method == "POST":
        action = text(request.form, "action", "Действие")
        if action == "apply":
            if request.form.get("confirm_changes") != "1":
                raise ValidationError("Подтвердите применение перечисленных изменений.")
            repo.apply_preview(timesheet_id, preview_id, operation_key=operation_key(request.form))
            flash("Согласованные изменения записаны одной транзакцией.", "success")
        elif action == "cancel":
            repo.cancel_preview(timesheet_id, preview_id)
            flash("Предварительный просмотр отменён. Табель не изменён.", "success")
        else:
            raise ValidationError("Неизвестное действие предварительного просмотра.")
        return redirect(url_for("timesheets.detail", timesheet_id=timesheet_id,
                                version_id=planned["version_id"]), code=303)
    report = repo.report(timesheet_id, version_id=planned["version_id"])
    return render_template("timesheet_preview.html", preview=planned, report=report, **common_context())


@bp.route("/timesheets/<int:timesheet_id>/import", methods=["GET", "POST"])
def import_data(timesheet_id):
    repo = repository()
    version_id = integer(request.values, "version_id", "Версия табеля", optional=True)
    report = repo.report(timesheet_id, version_id=version_id)
    error = None
    if request.method == "POST":
        try:
            if report["version"]["status"] != "draft":
                raise ValidationError("Импорт разрешён только в черновик или корректирующий черновик.")
            if revision(request.form) != report["version"]["revision"]:
                raise ValidationError("Табель изменён. Загрузите книгу после обновления страницы.")
            workbook = request.files.get("workbook")
            if workbook is None or not workbook.filename:
                raise ValidationError("Выберите файл .xlsx с листом «Табель».")
            payload = workbook.stream.read(MAX_UPLOAD + 1)
            rows = parse_timesheet_xlsx(payload, workbook.filename)
            preview_id = repo.preview_import(timesheet_id, rows, workbook.filename,
                                             version_id=report["version"]["id"], expected_revision=revision(request.form))
            return redirect(url_for("timesheets.preview", timesheet_id=timesheet_id,
                                    preview_id=preview_id), code=303)
        except ValueError as exc:
            error = str(exc)
    return render_template("timesheet_import.html", report=report, error=error, **common_context()), 400 if error else 200


@bp.get("/timesheets/<int:timesheet_id>/export.xlsx")
def export(timesheet_id):
    report = repository().report(timesheet_id,
        version_id=integer(request.args, "version_id", "Версия табеля", optional=True))
    payload = export_timesheet_xlsx(report)
    filename = "Табель_{}_{:02d}_v{}.xlsx".format(report["timesheet"]["year"],
        report["timesheet"]["month"], report["version"]["version_no"])
    return send_file(BytesIO(payload), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=filename)


@bp.get("/timesheets/template.xlsx")
def template():
    from app.branding import PRODUCT_SLUG
    return send_file(BytesIO(make_timesheet_template()),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True,
        download_name=PRODUCT_SLUG+"_Шаблон_табеля.xlsx")
