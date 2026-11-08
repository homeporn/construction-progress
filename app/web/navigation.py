"""Interface-only navigation; existing application routes remain canonical."""

from flask import current_app, request, session, url_for


GROUPS = (
    ("home", "Главная", "⌂", (("home", "Сводка объекта", "dashboard.index", {}),)),
    ("objects", "Объекты и объёмы", "▥", (
        ("projects", "Строительные объекты", "projects.index", {}),
        ("buildings", "Корпуса и конструктивы", "buildings.index", {}),
        ("volumes", "Ведомости объёмов", "volumes.index", {}),
        ("catalog", "Кодификатор видов работ", "catalog.index", {}))),
    ("production", "Производство", "✓", (
        ("facts", "Фактическое выполнение", "events.facts", {}),
        ("confirmations", "Подтверждение объёмов", "events.confirmations", {}),
        ("orders", "Наряды-задания", "work_orders.index", {}))),
    ("personnel", "Персонал и бригады", "♙", (
        ("workers", "Работники", "personnel_management.workers", {}),
        ("brigades", "Бригады", "personnel_management.brigades", {}),
        ("import", "Импорт Excel", "personnel_import.start", {}),
        ("imports", "История импортов", "personnel_import.history", {}))),
    ("time", "Рабочее время", "◷", (
        ("timesheets", "Табели", "timesheets.index", {}),
        ("timesheet_create", "Создать табель", "timesheets.create", {}),
        ("timesheet_history", "История и утверждённые табели", "timesheets.index", {"history": "1"}))),
    ("productivity", "Производительность", "↗", (
        ("overview", "Сводка производительности", "labor.overview", {}),
        ("allocations", "Распределение часов", "labor.allocations", {}),
        ("attributions", "Исполнители объёмов", "labor.attributions", {}),
        ("works", "Выработка по работам", "labor.works", {}),
        ("contractors", "Подрядчики", "labor.contractors", {}),
        ("brigades", "Бригады", "labor.brigades", {}))),
    ("analytics", "Аналитика", "▤", (
        ("summary", "Физическая готовность", "analytics.index", {"tab": "summary"}),
        ("natural", "Натуральные объёмы", "analytics.index", {"tab": "volumes"}),
        ("groups", "Аналитические группы", "analytics.index", {"tab": "groups"}),
        ("dynamics", "Динамика выполнения", "analytics.index", {"tab": "dynamics"}),
        ("deviations", "Отклонения", "analytics.index", {"tab": "deviations"}),
        ("export", "Экспорт отчётов", "analytics.index", {"tab": "export"}))),
    ("settings", "Справочники и настройки", "⚙", (
        ("contractors", "Подрядчики", "personnel.index", {"section": "contractors"}),
        ("specialties", "Специальности", "personnel.index", {"section": "specialties"}),
        ("settings", "Настройки приложения", "navigation.settings", {}))),
)
GROUP_KEYS = frozenset(row[0] for row in GROUPS)


def active_page():
    """Map both old URLs and nested actions to one navigation item."""
    endpoint = request.endpoint or ""
    args = request.view_args or {}
    if endpoint.startswith("dashboard."):
        return "home", "home"
    if endpoint.startswith("projects."):
        return "objects", "projects"
    if endpoint.startswith("buildings."):
        return "objects", "buildings"
    if endpoint.startswith("volumes."):
        return "objects", "volumes"
    if endpoint.startswith("catalog."):
        return "objects", "catalog"
    if endpoint.startswith("events."):
        return "production", "confirmations" if "confirmation" in endpoint else "facts"
    if endpoint.startswith("work_orders."):
        return "production", "orders"
    if endpoint.startswith("personnel_import."):
        return "personnel", "imports" if endpoint.endswith("history") else "import"
    if endpoint.startswith("personnel_management."):
        return "personnel", "brigades" if "brigade" in endpoint else "workers"
    if endpoint.startswith("personnel."):
        kind = args.get("kind") or request.args.get("section")
        if kind == "employees":
            return "personnel", "workers"
        return "settings", "specialties" if kind == "specialties" else "contractors"
    if endpoint.startswith("timesheets."):
        if endpoint.endswith("create"):
            return "time", "timesheet_create"
        return "time", "timesheet_history" if request.args.get("history") == "1" else "timesheets"
    if endpoint.startswith("labor."):
        item = endpoint.split(".", 1)[1]
        return "productivity", "overview" if item in ("history", "export") else item
    if endpoint.startswith("progress."):
        return "analytics", "summary"
    if endpoint.startswith("analytics."):
        tab = args.get("tab", "summary")
        if "group_" in endpoint:
            tab = "groups"
        elif endpoint.endswith("download"):
            tab = "export"
        return "analytics", {"volumes": "natural"}.get(tab, tab)
    if endpoint.startswith("navigation."):
        return "settings", "settings"
    return None, None


def navigation_context():
    active_group, active_item = active_page()
    saved = session.get("navigation", {})
    opened = saved.get("opened", [active_group] if active_group != "home" else ["objects"])
    groups = []
    current = None
    current_group = None
    for key, label, symbol, descriptors in GROUPS:
        items = []
        for item_key, item_label, endpoint, values in descriptors:
            # Optional modules are omitted until their blueprint is registered.
            # Never advertise a future page or fabricate a working URL.
            if endpoint not in current_app.view_functions:
                continue
            item = dict(key=item_key, label=item_label, url=url_for(endpoint, **values),
                        active=key == active_group and item_key == active_item)
            items.append(item)
            if item["active"]:
                current = item
        if items:
            group = dict(key=key, label=label, symbol=symbol, items=items,
                         active=key == active_group, opened=key in opened)
            groups.append(group)
            if group["active"]:
                current_group = group

    crumbs = [dict(label="Главная", url=url_for("dashboard.index"))]
    if current_group and current_group["key"] != "home":
        crumbs.append(dict(label=current_group["label"], url=None))
        if current:
            crumbs.append(dict(label=current["label"], url=current["url"]))
    endpoint = request.endpoint or ""
    tail = None
    if endpoint == "projects.create":
        tail = "Создание объекта"
    elif endpoint == "projects.detail":
        tail = "Карточка объекта"
    elif endpoint == "personnel.edit":
        tail = "Редактирование" if (request.view_args or {}).get("record_id") else "Новая запись"
    elif endpoint == "personnel_management.worker_detail":
        tail = "Карточка работника"
    elif endpoint == "personnel_management.brigade_detail":
        tail = "Состав бригады"
    elif endpoint == "personnel_import.wizard":
        tail = "Мастер импорта"
    elif endpoint == "analytics.group_form":
        tail = "Состав группы"
    elif endpoint.startswith("work_orders.") and endpoint != "work_orders.index":
        tail = "Наряд-задание"
    elif endpoint == "progress.index":
        tail = "История прогресса"
    elif endpoint.startswith("timesheets.") and endpoint not in ("timesheets.index", "timesheets.create"):
        tail = "Табель" if endpoint == "timesheets.detail" else "Операция с табелем"
    if tail:
        crumbs.append(dict(label=tail, url=None))
    if crumbs:
        crumbs[-1]["url"] = None
    return dict(navigation_groups=groups, navigation_breadcrumbs=crumbs,
                navigation_collapsed=bool(saved.get("collapsed", False)))
