"""Offline productivity workbook, using the application's exact OOXML writer."""
from datetime import date, datetime
from decimal import Decimal
import json

from .xlsx_export import pack_workbook, sheet


SHEETS = ("01. Сводка", "02. Распределения часов", "03. Выработка",
          "04. Подрядчики", "05. Бригады", "06. Нераспределённые данные",
          "07. Исходные данные")


def export_labor_xlsx(report):
    """Keep quantities numeric, dates typed and unrelated personnel data absent."""
    def day(value):
        return date.fromisoformat(value) if isinstance(value, str) else value

    metadata_headers = ["Объект", "Начало периода", "Конец периода", "Режим",
                        "Направление", "Отсечение знаний (UTC, точно)",
                        "Отсечение знаний (дата UTC)", "Фильтры", "Версия плана (ID)"]
    cutoff = report["cutoff"]
    metadata = [report["project"]["name"], day(report["start_date"]), day(report["end_date"]),
                "Факт" if report["mode"] == "actual" else "Подтверждённые объёмы",
                "СМР" if report["discipline"] == "CONSTRUCTION" else "Проектирование",
                cutoff, datetime.fromisoformat(cutoff.replace("Z", "+00:00")),
                json.dumps(report.get("filters", {}), ensure_ascii=False, sort_keys=True),
                report.get("plan_version_id", report.get("filters", {}).get("plan_version_id"))]

    def table(headers, rows, percentages=()):
        # Include metadata even when the selection has no data rows.
        rows = list(rows)
        return sheet(headers + metadata_headers,
                     [list(row) + metadata for row in rows] or [[None] * len(headers) + metadata],
                     percentages)

    summary = report["summary"]
    labels = (("worked_hours", "Отработано, чел.-ч"), ("allocated_hours", "Распределено, чел.-ч"),
              ("unallocated_hours", "Не распределено, чел.-ч"), ("normative_hours", "Нормативная трудоёмкость, чел.-ч"),
              ("unique_workers", "Уникальных работников"), ("average_daily_workers", "Средняя численность за календарный день"),
              ("conflict_count", "Конфликтов"))
    bodies = [table(["Показатель", "Значение"], [[label, summary.get(key)] for key, label in labels])]
    works = report.get("works", [])
    bodies.append(table(["Дата", "Работник (ID)", "Позиция (ID)", "Код", "Работа", "Ед.",
                         "Подрядчик (ID)", "Бригада (ID)", "Утверждённая запись (ID)", "Часы",
                         "Редакция", "Версия норматива (ID)", "Норматив, чел.-ч/ед.", "Без конфликта"],
                        [[day(r.get("work_date")), r.get("employee_id"), r.get("work_item_id"),
                          r.get("code"), r.get("name"), r.get("unit"), r.get("contractor_id"),
                          r.get("brigade_id"), r.get("timesheet_entry_id"), r.get("hours"),
                          r.get("revision_no"), r.get("plan_version_id"), r.get("labor_norm"),
                          "Да" if r.get("valid", True) else "Нет"] for r in report.get("allocations", [])]))
    metric_headers = ["Код", "Работа", "Ед.", "Объём", "Часы", "Фактическая выработка, ед./чел.-ч",
                      "Нормативная выработка, ед./чел.-ч", "Выполнение нормы", "Полнота"]

    def metrics(row):
        return [row.get(key) for key in ("code", "name", "unit", "quantity", "hours", "actual_output", "norm_output", "norm_ratio")] + [
            "Полные данные" if row.get("complete") else "Неполные данные"]

    bodies.append(table(metric_headers, [metrics(r) for r in works], (8,)))
    for key in ("contractors", "brigades"):
        bodies.append(table(["Исполнитель (ID)", "Исполнитель"] + metric_headers,
                            [[r.get("entity_id"), r.get("entity_name")] + metrics(r) for r in report.get(key, [])], (10,)))
    unassigned = []
    for row in report.get("available_hours", []):
        if Decimal(str(row.get("remaining", 0))) == 0:
            continue
        unassigned.append(["Часы", day(row.get("work_date")), row.get("employee_id"), None, None,
                           None, "чел.-ч", row.get("hours"), row.get("allocated"), row.get("remaining"),
                           row.get("contractor_id"), row.get("brigade_id"), row.get("entry_id", row.get("timesheet_entry_id")),
                           "Да" if row.get("valid", True) else "Нет"])
    for row in report.get("events", []):
        if Decimal(str(row.get("remaining", 0))) == 0:
            continue
        unassigned.append(["Объём", day(row.get("work_date")), None, row.get("id"), row.get("work_item_id"),
                           row.get("code"), row.get("unit"), row.get("available"), row.get("assigned"), row.get("remaining"),
                           None, None, None, "Да" if row.get("valid", True) else "Нет"])
    bodies.append(table(["Источник", "Дата", "Работник (ID)", "Факт (ID)", "Позиция (ID)", "Код", "Ед.",
                         "Доступно", "Распределено", "Остаток", "Подрядчик (ID)", "Бригада (ID)",
                         "Утверждённая запись (ID)", "Без конфликта"], unassigned))
    raw = []
    for row in report.get("allocations", []):
        raw.append(["Часы", day(row.get("work_date")), row.get("work_item_id"), row.get("employee_id"), row.get("contractor_id"),
                    row.get("brigade_id"), row.get("timesheet_entry_id"), None, row.get("hours"), None,
                    row.get("revision_no"), "Да" if row.get("valid", True) else "Нет",
                    row.get("plan_version_id"), row.get("labor_norm")])
    for row in report.get("attributions", []):
        raw.append(["Объём", day(row.get("work_date")), row.get("work_item_id"), row.get("employee_id"), row.get("contractor_id"),
                    row.get("brigade_id"), None, row.get("fact_event_id"), None, row.get("quantity"),
                    row.get("revision_no"), "Да" if row.get("valid", True) else "Нет",
                    row.get("plan_version_id"), row.get("labor_norm")])
    bodies.append(table(["Источник", "Дата", "Позиция (ID)", "Работник (ID)", "Подрядчик (ID)", "Бригада (ID)",
                         "Запись табеля (ID)", "Факт (ID)", "Часы", "Объём", "Редакция", "Без конфликта",
                         "Версия норматива (ID)", "Норматив, чел.-ч/ед."], raw))
    return pack_workbook(SHEETS, bodies)
