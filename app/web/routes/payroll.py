"""Local management accruals. Payroll repositories own all accounting writes."""
from datetime import date
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for

from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.payroll_repository import PayrollRepository
from app.repositories.payroll_rules_repository import PayrollRulesRepository
from app.repositories.payroll_piece_repository import PayrollPieceRepository
from app.services.payroll_xlsx import export_payroll_xlsx
from app.web import current_project, current_project_id, get_db
from app.web.forms import InputError, business_date, integer, quantity, text
from app.web.pagination import paginate

bp = Blueprint("payroll", __name__)
TABS = {"overview": "Сводка", "terms": "Условия оплаты", "rates": "Расценки",
        "operations": "Технологические операции", "periods": "Расчётные периоды",
        "claims": "Сопоставление подтверждений"}
SYSTEMS = {"hourly": "Почасовая", "salary": "Оклад", "piece": "Сдельная", "combined": "Комбинированная"}
STATUSES = {"draft": "Черновик", "calculated": "Рассчитано", "submitted": "На проверке",
            "approved": "Утверждено", "closed": "Закрыто"}
COMPONENTS = {"base_hourly": "Почасовая часть", "base_salary": "Окладная часть", "piece": "Сдельная часть",
              "bonus": "Премия", "supplement": "Доплата", "adjustment": "Корректировка"}
METHODS = {"equal": "Поровну", "hours": "По утверждённым часам", "ktu": "По КТУ", "manual": "Ручные суммы"}
BONUS_KINDS = {"none": "Нет", "fixed": "Фиксированная", "percent": "Процент", "performance": "За выработку"}
ACTIONS = {"created": "Создание периода", "calculated": "Расчёт", "inputs_changed": "Изменение данных",
           "submit": "Передача на проверку", "return": "Возврат на доработку", "approve": "Утверждение",
           "close": "Закрытие", "correction_created": "Создание исправления", "piece_confirmed": "Сдельное распределение",
           "piece_cancelled": "Отмена сдельного распределения"}


def repository(cls=PayrollRepository):
    return cls(get_db(), current_project_id())


def _choice(data, name, choices, label, default=None):
    value = data.get(name, default)
    if value not in choices:
        raise InputError(label + ": выберите значение из списка.")
    return value


def _revision(data):
    value = data.get("expected_revision", "0")
    if not value.isascii() or not value.isdigit() or len(value) > 9:
        raise InputError("Редакция изменилась. Обновите страницу.")
    return int(value)


def _key():
    return text(request.form, "operation_key", "Ключ операции", limit=100)


def _writable():
    if request.form.get("workspace_id") != str(current_project_id()):
        raise InputError("Рабочее пространство изменилось. Обновите страницу.")
    if current_project()["status"] != "active":
        raise InputError("Объект в архиве: начисления доступны только для чтения.")


def _scope_person(employee_id, contractor_id=None):
    person = PersonnelRepository(get_db(), current_project_id()).employee(employee_id)
    if contractor_id and person["contractor_id"] != contractor_id:
        raise InputError("Работник относится к другому подрядчику.")
    return person


def _catalog(catalog_id):
    if get_db().execute("SELECT id FROM work_catalog WHERE id=?", (catalog_id,)).fetchone() is None:
        raise InputError("Код работы не найден.")


def _selectors(*, contractor_id=None, employee_id=None, catalog_id=None, operation_id=None):
    """Bound form choices in SQL; explicit edited IDs remain visible after a search."""
    db, pid = get_db(), current_project_id()
    search = text(request.args, "q", "Поиск", required=False)
    match = "%" + search + "%"
    people = db.execute("SELECT e.id,e.full_name,e.contractor_id,c.name AS contractor_name FROM employees e "
        "JOIN employee_projects ep ON ep.employee_id=e.id LEFT JOIN contractors c ON c.id=e.contractor_id "
        "WHERE ep.project_id=? AND (? IS NULL OR e.contractor_id=?) "
        "AND (e.full_name LIKE ? OR CAST(e.id AS TEXT)=? OR e.id=?) ORDER BY e.full_name,e.id LIMIT 100",
        (pid, contractor_id, contractor_id, match, search, employee_id)).fetchall()
    catalog = db.execute("SELECT id,code,name,unit FROM work_catalog WHERE code LIKE ? OR name LIKE ? OR id=? "
        "ORDER BY code,id LIMIT 100", (match, match, catalog_id)).fetchall()
    brigades = db.execute("SELECT b.id,b.number,b.contractor_id,c.name AS contractor_name FROM brigades b "
        "JOIN contractors c ON c.id=b.contractor_id JOIN contractor_projects cp ON cp.contractor_id=c.id "
        "WHERE cp.project_id=? AND (? IS NULL OR b.contractor_id=?) ORDER BY c.name,b.number,b.id LIMIT 100",
        (pid, contractor_id, contractor_id)).fetchall()
    return dict(people=people, catalog=catalog, brigades=brigades, search=search,
        contractors=PersonnelRepository(db, pid).list_contractors(),
        operations=db.execute("SELECT id,code,name,catalog_id FROM payroll_tech_operations WHERE project_id=? "
            "AND (code LIKE ? OR name LIKE ? OR id=? OR catalog_id=?) ORDER BY code,id LIMIT 100",
            (pid, match, match, operation_id, catalog_id)).fetchall())


def _history(table, roots, root_id):
    if root_id is None:
        return []
    raw = request.args.get("history_page", "1")
    if not raw.isascii() or not raw.isdigit() or len(raw) > 6 or int(raw) < 1:
        raise InputError("Страница истории: укажите положительный номер.")
    # Names are private constants from terms/rates routes, never request input.
    clause = " FROM " + table + " v JOIN " + roots + " r ON r.id=v.root_id WHERE r.project_id=? AND r.id=?"
    count = get_db().execute("SELECT count(*)" + clause, (current_project_id(), root_id)).fetchone()[0]
    rows = get_db().execute("SELECT v.*" + clause + " ORDER BY v.revision_no DESC LIMIT 50 OFFSET ?",
        (current_project_id(), root_id, (int(raw) - 1) * 50)).fetchall()
    return paginate(rows, "history_page", total_count=count)


def _render(template, tab, **context):
    return render_template(template, tab=tab, tabs=TABS, systems=SYSTEMS, statuses=STATUSES,
        components=COMPONENTS, methods=METHODS, bonus_kinds=BONUS_KINDS, actions=ACTIONS,
        term_states={"active": "Действует", "archived": "Архив"}, batch_states={"active": "Действует", "cancelled": "Отменено"},
        new_key=uuid4().hex, read_only=current_project()["status"] != "active", **context)


def _money(data, name, label, default="0"):
    return str(quantity({name: data.get(name) or default}, name, label))


@bp.get("/payroll")
def overview():
    report = repository().dashboard(year=integer(request.args,'year','Год',optional=True),
        month=integer(request.args,'month','Месяц',optional=True),contractor_id=integer(request.args,'contractor_id','Подрядчик',optional=True))
    groups = {}
    for key in ("by_contractor", "by_method", "by_work"):
        values = report.get(key, report.get("rows", {}).get(key, []) if isinstance(report.get("rows"), dict) else [])
        groups[key] = [dict(row, label=(row.get("name") or row.get("contractor_name")
            or COMPONENTS.get(row.get("component")) or SYSTEMS.get(row.get("system"))
            or row.get("code") or ("Позиция ID " + str(row["work_item_id"]) if row.get("work_item_id") else "Без привязки к работе")),
            total=row.get("total", row.get("amount", Decimal(0)))) for row in values]
    return _render("payroll_overview.html", "overview", report=report,
        groups=groups, period_rows=paginate(list(report.get("periods", []))),contractors=PersonnelRepository(get_db(),current_project_id()).list_contractors())


@bp.route("/payroll/terms", methods=["GET", "POST"])
def terms():
    repo = repository(PayrollRulesRepository)
    if request.method == "POST":
        _writable()
        data = request.form
        employee_id = integer(data, "employee_id", "Работник")
        contractor_id = integer(data, "contractor_id", "Подрядчик")
        PersonnelRepository(get_db(), current_project_id()).contractor(contractor_id)
        _scope_person(employee_id, contractor_id)
        values = dict(employee_id=employee_id, contractor_id=contractor_id,
            valid_from=business_date(data, "valid_from"),
            valid_to=business_date(data, "valid_to") if data.get("valid_to") else None,
            system=_choice(data, "system", SYSTEMS, "Система оплаты"),
            base_kind=_choice(data, "base_kind", ("hourly", "salary", "none"), "Базовая часть", "hourly"),
            currency=_choice(data, "currency", ("RUB", "USD", "EUR"), "Валюта"),
            piece_enabled=data.get("piece_enabled") == "1",
            bonus_kind=_choice(data, "bonus_kind", ("none", "fixed", "percent", "performance"), "Премия", "none"),
            bonus_basis=_choice(data, "bonus_basis", ("base", "piece"), "База премии", "base"),
            status=_choice(data, "status", ("active", "archived"), "Статус", "active"),
            comment=text(data, "comment", "Комментарий", required=False, limit=2000))
        for name, label, default in (("hourly_rate", "Часовая ставка", "0"), ("salary", "Оклад", "0"),
                ("bonus_value", "Размер премии", "0"), ("performance_threshold", "Порог выработки", "1"),
                ("fixed_supplement", "Фиксированная доплата", "0")):
            values[name] = _money(data, name, label, default)
        repo.save_terms(values, term_id=integer(data, "term_id", "Условия", optional=True),
            expected_revision=_revision(data), operation_key=_key())
        flash("Создана неизменяемая редакция условий оплаты.", "success")
        return redirect(url_for("payroll.terms"), code=303)
    rows = [dict(r) for r in repo.list_terms()]
    edit_id = integer(request.args, "term_id", "Условия", optional=True)
    edit = next((r for r in rows if r.get("root_id", r["id"]) == edit_id), None) if edit_id else None
    if edit_id and edit is None:
        raise InputError("Условия не принадлежат текущему объекту.")
    return _render("payroll_terms.html", "terms", rows=paginate(rows), edit=edit or {},
        revisions=_history("payroll_terms", "payroll_term_roots", edit_id),
        **_selectors(employee_id=edit.get("employee_id") if edit else None))


@bp.route("/payroll/rates", methods=["GET", "POST"])
def rates():
    repo = repository(PayrollRulesRepository)
    if request.method == "POST":
        _writable()
        data = request.form
        catalog_id = integer(data, "catalog_id", "Код работы")
        _catalog(catalog_id)
        contractor_id = integer(data, "contractor_id", "Подрядчик", optional=True)
        if contractor_id:
            PersonnelRepository(get_db(), current_project_id()).contractor(contractor_id)
        values = dict(catalog_id=catalog_id, contractor_id=contractor_id,
            operation_id=integer(data, "operation_id", "Операция", optional=True),
            amount=_money(data, "amount", "Расценка"), unit=text(data, "unit", "Единица измерения", limit=80),
            currency=_choice(data, "currency", ("RUB", "USD", "EUR"), "Валюта"),
            valid_from=business_date(data, "valid_from"),
            valid_to=business_date(data, "valid_to") if data.get("valid_to") else None,
            status=_choice(data, "status", ("active", "archived"), "Статус", "active"),
            comment=text(data, "comment", "Комментарий", required=False, limit=2000))
        repo.save_rate(values, rate_id=integer(data, "rate_id", "Расценка", optional=True),
            expected_revision=_revision(data), operation_key=_key())
        flash("Создана неизменяемая редакция расценки.", "success")
        return redirect(url_for("payroll.rates"), code=303)
    rows = [dict(r) for r in repo.list_rates()]
    edit_id = integer(request.args, "rate_id", "Расценка", optional=True)
    edit = next((r for r in rows if r.get("root_id", r["id"]) == edit_id), None) if edit_id else None
    if edit_id and edit is None:
        raise InputError("Расценка не принадлежит текущему объекту.")
    return _render("payroll_rates.html", "rates", rows=paginate(rows), edit=edit or {},
        revisions=_history("payroll_rates", "payroll_rate_roots", edit_id),
        **_selectors(catalog_id=edit.get("catalog_id") if edit else None, operation_id=edit.get("operation_id") if edit else None))


@bp.route("/payroll/operations", methods=["GET", "POST"])
def operations():
    repo = repository(PayrollRulesRepository)
    if request.method == "POST":
        _writable()
        data = request.form
        catalog_id = integer(data, "catalog_id", "Код работы")
        _catalog(catalog_id)
        repo.save_operation(catalog_id, text(data, "code", "Код операции", limit=80),
            text(data, "name", "Название операции"),
            operation_id=integer(data, "operation_id", "Операция", optional=True), is_active=data.get("is_active") == "1")
        flash("Технологическая операция сохранена.", "success")
        return redirect(url_for("payroll.operations"), code=303)
    rows = [dict(r) for r in repo.list_operations()]
    edit_id = integer(request.args, "operation_id", "Операция", optional=True)
    edit = next((r for r in rows if r["id"] == edit_id), None) if edit_id else None
    if edit_id and edit is None:
        raise InputError("Операция не принадлежит текущему объекту.")
    return _render("payroll_operations.html", "operations", rows=paginate(rows), edit=edit or {},
        **_selectors(catalog_id=edit.get("catalog_id") if edit else None))


@bp.get("/payroll/periods")
def periods():
    return _render("payroll_periods.html", "periods", rows=paginate(list(repository().list_periods())))


@bp.route("/payroll/periods/new", methods=["GET", "POST"])
def create():
    if request.method == "POST":
        _writable()
        data = request.form
        contractor_id = integer(data, "contractor_id", "Подрядчик")
        PersonnelRepository(get_db(), current_project_id()).contractor(contractor_id)
        norm = quantity(data, "norm_hours", "Норма часов полного месяца")
        if norm <= 0:
            raise InputError("Норма часов полного месяца должна быть положительной.")
        pid = repository().create_period(contractor_id, integer(data, "year", "Год"), integer(data, "month", "Месяц"),
            norm_hours=str(norm), comment=text(data, "comment", "Комментарий", required=False, limit=2000), operation_key=_key())
        return redirect(url_for("payroll.detail", period_id=pid), code=303)
    return _render("payroll_create.html", "periods", **_selectors())


def _report(period_id, version_id=None):
    report = dict(repository().report(period_id, version_id))
    for key in ("rows", "lines", "errors", "warnings", "versions", "events", "runs", "adjustments", "batches"):
        report.setdefault(key, [])
    for key in ("totals", "delta", "sources"):
        report.setdefault(key, {})
    report.setdefault("source_changed", False)
    report["rows"] = [dict(row) for row in report["rows"]]
    for row in report["rows"]:
        row["system_label"] = " + ".join(SYSTEMS.get(part, COMPONENTS.get(part, part)) for part in (row.get("system") or "—").split(" + "))
    for collection in ("lines", "adjustments", "batches"):
        report[collection] = [dict(row) for row in report[collection]]
        for row in report[collection]:
            row.setdefault("amount", Decimal(row.get("amount_minor", row.get("total_minor", 0))) / Decimal(100))
    return report


def _version_report(version_id, *, writable=False):
    # The backend resolves a version only inside the current project.
    version = repository().require_version(version_id)
    report = _report(version["period_id"], version_id)
    versions = report["versions"]
    latest = max((v["id"] for v in versions), default=version_id)
    if writable and (version_id != latest or report["version"]["status"] not in ("draft", "calculated")):
        raise InputError("Предыдущая или утверждённая версия доступна только для чтения. Создайте корректирующую версию.")
    return report


@bp.get("/payroll/periods/<int:period_id>")
def detail(period_id):
    report = _report(period_id, integer(request.args, "version_id", "Версия", optional=True))
    latest = max((v["id"] for v in report["versions"]), default=report["version"]["id"])
    editable = (report["version"]["id"] == latest and report["version"]["status"] in ("draft", "calculated")
                and current_project()["status"] == "active")
    report["rows"] = paginate(report["rows"], "worker_page")
    report["lines"] = paginate(report["lines"], "line_page")
    report["events"] = paginate(report["events"], "event_page")
    report["runs"] = paginate(report["runs"], "run_page")
    report["adjustments"] = paginate(report["adjustments"], "adjustment_page")
    report["batches"] = paginate(report["batches"], "batch_page")
    report['employee_deltas']=paginate(report.get('employee_deltas',[]),'difference_page')
    claim_search=text(request.args,'claim_q','Поиск сопоставления',required=False)
    claim_rows=[r for r in repository(PayrollPieceRepository).list_claims() if r['contractor_id']==report['period']['contractor_id']
        and (not claim_search or claim_search.casefold() in (str(r['claim_id'])+' '+r.get('code','')+' '+r.get('name','')).casefold())]
    return _render("payroll_detail.html", "periods", report=report, editable=editable, latest=report["version"]["id"] == latest,
        claims=claim_rows[:100],claim_search=claim_search,
        **_selectors(contractor_id=report["period"]["contractor_id"]))


@bp.post("/payroll/versions/<int:version_id>/action")
def action(version_id):
    _writable()
    data = request.form
    action_name = text(data, "action", "Действие")
    report = _version_report(version_id, writable=action_name in ("settings", "adjustment", "calculate"))
    repo = repository()
    reason = text(data, "reason", "Причина", required=False, limit=2000)
    if action_name == "settings":
        norm = quantity(data, "norm_hours", "Норма часов полного месяца")
        if norm <= 0:
            raise InputError("Норма часов полного месяца должна быть положительной.")
        repo.update_settings(version_id, str(norm), text(data, "comment", "Комментарий", required=False, limit=2000),
            expected_revision=_revision(data), operation_key=_key())
    elif action_name == "adjustment":
        employee_id = integer(data, "employee_id", "Работник")
        _scope_person(employee_id, report["period"]["contractor_id"])
        repo.add_adjustment(version_id, employee_id, str(quantity(data, "amount", "Сумма", signed=True)),
            kind=_choice(data, "kind", ("bonus", "adjustment"), "Вид начисления"),
            currency=_choice(data, "currency", ("RUB", "USD", "EUR"), "Валюта"),
            effective_date=business_date(data, "effective_date"),
            reason=text(data, "reason", "Причина", limit=2000), operation_key=_key())
    elif action_name == "calculate":
        repo.calculate(version_id, operation_key=_key())
    elif action_name == "correction":
        if report["version"]["status"] not in ("approved", "closed"):
            raise InputError("Корректирующая версия создаётся из утверждённого периода.")
        new_id = repo.create_correction(report["period"]["id"], text(data, "reason", "Причина исправления", limit=2000), operation_key=_key())
        return redirect(url_for("payroll.detail", period_id=report["period"]["id"], version_id=new_id), code=303)
    elif action_name in ("submit", "return", "approve", "close"):
        if not max((v["id"] for v in report["versions"]), default=version_id) == version_id:
            raise InputError("Предыдущая версия доступна только для чтения.")
        repo.transition(version_id, action_name, reason=reason, operation_key=_key())
    elif action_name == "cancel_batch":
        _version_report(version_id, writable=True)
        repository(PayrollPieceRepository).cancel_batch(version_id, integer(data, "batch_id", "Распределение"),
            text(data, "reason", "Причина отмены", limit=2000), operation_key=_key())
    else:
        raise InputError("Неизвестное действие начисления.")
    flash("Операция начисления сохранена.", "success")
    return redirect(url_for("payroll.detail", period_id=report["period"]["id"], version_id=version_id), code=303)


@bp.route("/payroll/claims", methods=["GET", "POST"])
def claims():
    repo = repository(PayrollPieceRepository)
    if request.method == "POST":
        _writable()
        data = request.form
        contractor_id = integer(data, "contractor_id", "Подрядчик")
        PersonnelRepository(get_db(), current_project_id()).contractor(contractor_id)
        employee_id = integer(data, "employee_id", "Работник", optional=True)
        brigade_id = integer(data, "brigade_id", "Бригада", optional=True)
        if not employee_id and not brigade_id:
            raise InputError("Явно выберите работника или бригаду исполнителя.")
        if employee_id:
            _scope_person(employee_id, contractor_id)
        if brigade_id and BrigadeRepository(get_db(), current_project_id()).get(brigade_id)["contractor_id"] != contractor_id:
            raise InputError("Бригада относится к другому подрядчику.")
        confirmation_id = integer(data, "confirmation_event_id", "Корневое подтверждение")
        source = get_db().execute("SELECT e.id FROM confirmation_events e JOIN work_items w ON w.id=e.work_item_id "
            "JOIN buildings b ON b.id=w.building_id WHERE e.id=? AND b.project_id=? AND e.correction_of_id IS NULL",
            (confirmation_id, current_project_id())).fetchone()
        if source is None:
            raise InputError("Выберите корневое подтверждение текущего объекта.")
        repo.save_claim(dict(confirmation_event_id=confirmation_id, contractor_id=contractor_id,
            employee_id=employee_id, brigade_id=brigade_id,
            operation_id=integer(data, "operation_id", "Технологическая операция", optional=True),
            quantity=str(quantity(data, "quantity", "Подтверждённый для начисления объём")),
            proof_ref=text(data, "proof_ref", "Основание исполнителя", limit=2000),
            reason=text(data, "reason", "Причина изменения", required=False, limit=2000)),
            claim_id=integer(data, "claim_id", "Сопоставление", optional=True),
            expected_revision=_revision(data), operation_key=_key())
        flash("Явное сопоставление подтверждения сохранено.", "success")
        return redirect(url_for("payroll.claims"), code=303)
    rows = [dict(r) for r in repo.list_claims()]
    edit_id = integer(request.args, "claim_id", "Сопоставление", optional=True)
    edit = next((r for r in rows if r.get("root_id", r["id"]) == edit_id), None) if edit_id else None
    if edit_id and edit is None:
        raise InputError("Сопоставление не принадлежит текущему объекту.")
    source_id = integer(request.args, "source_id", "Корневое подтверждение", optional=True) or (edit.get("confirmation_event_id") if edit else None)
    sources = get_db().execute("SELECT e.id,e.confirmed_date AS effective_date,e.quantity_delta,c.code,c.name,c.unit,b.name AS building_name "
        "FROM confirmation_events e JOIN work_items w ON w.id=e.work_item_id JOIN buildings b ON b.id=w.building_id "
        "JOIN work_catalog c ON c.id=w.catalog_id WHERE b.project_id=? AND e.correction_of_id IS NULL "
        "AND (? IS NULL OR e.id=?) ORDER BY e.id DESC LIMIT 100", (current_project_id(), source_id, source_id)).fetchall()
    return _render("payroll_claims.html", "claims", rows=paginate(rows), edit=edit or {}, sources=sources,
        **_selectors(employee_id=edit.get("employee_id") if edit else None))


@bp.post("/payroll/versions/<int:version_id>/piece-preview")
def piece_preview(version_id):
    _writable()
    report = _version_report(version_id, writable=True)
    data = request.form
    selected = data.getlist("employee_id")
    if not selected or len(selected) > 50 or len(set(selected)) != len(selected):
        raise InputError("Выберите от 1 до 50 разных работников.")
    method = _choice(data, "method", ("equal", "hours", "ktu", "manual"), "Способ распределения")
    recipients = []
    for value in selected:
        employee_id = integer({"id": value}, "id", "Работник")
        _scope_person(employee_id, report["period"]["contractor_id"])
        recipient = dict(employee_id=employee_id)
        if method == "ktu":
            recipient["weight"] = str(quantity(data, "weight_" + value, "КТУ"))
        elif method == "manual":
            recipient["amount"] = str(quantity(data, "amount_" + value, "Ручная сумма"))
        recipients.append(recipient)
    preview = repository(PayrollPieceRepository).preview(version_id,
        integer(data, "claim_id", "Сопоставление"), str(quantity(data, "quantity", "Распределяемый объём")),
        method, recipients, reason=text(data, "reason", "Пояснение", required=False, limit=2000), operation_key=_key())
    preview = dict(preview)
    shares = preview.get("shares", [])
    if isinstance(shares, dict):
        shares = [dict(employee_id=int(employee_id), amount_minor=amount) for employee_id, amount in shares.items()]
    preview["shares"] = [dict(row, amount=Decimal(row["amount_minor"]) / Decimal(100)) for row in shares]
    preview["total_amount"] = Decimal(preview["total_minor"]) / Decimal(100)
    preview.setdefault("source", {})
    return _render("payroll_preview.html", "periods", report=report, preview=preview)


@bp.post("/payroll/versions/<int:version_id>/piece-confirm")
def piece_confirm(version_id):
    _writable()
    report = _version_report(version_id, writable=True)
    if request.form.get("confirm_changes") != "1":
        raise InputError("Подтвердите распределение после проверки предварительного просмотра.")
    repository(PayrollPieceRepository).confirm(version_id, text(request.form, "preview_id", "Предварительный просмотр", limit=100), operation_key=_key())
    flash("Сдельное распределение подтверждено.", "success")
    return redirect(url_for("payroll.detail", period_id=report["period"]["id"]), code=303)


@bp.get("/payroll/periods/<int:period_id>/export.xlsx")
def export(period_id):
    report = _report(period_id, integer(request.args, "version_id", "Версия", optional=True))
    return send_file(BytesIO(export_payroll_xlsx(report)), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name="Начисления-{}-v{}.xlsx".format(period_id, report["version"].get("version_no", 1)))
