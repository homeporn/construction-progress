"""Eight-sheet management accrual snapshot using the existing OOXML writer."""
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from hashlib import sha256
import json
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

from .xlsx_export import NS, column, pack_workbook, sheet, xml
from .payroll_money import from_minor

SHEETS = ("01. Сводка", "02. По работникам", "03. Почасовая часть", "04. Окладная часть",
          "05. Сдельная часть", "06. Премии и корректировки", "07. Применённые расценки", "08. Источники")
N = {"s": NS}
PRIVATE = frozenset(("phone", "email", "inn", "contact_person", "address", "birth_date", "passport",
                     "bank_account", "position", "specialty_name", "specialty_id", "needs_details"))
CONTINUATION_HEADERS = ["Исходная ячейка", "SHA-256 текста UTF-8", "Номер части", "Всего частей"]
TEXT_CHUNK_SIZE = 15000  # At most 30000 UTF-16 units, including astral characters.
JSON_CHUNK_SIZE = 4999  # A control character can expand to six JSON characters.


def _safe(value):
    """Keep accounting dependencies; omit unrelated personnel/contact attributes."""
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items() if key not in PRIVATE}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def _json(value):
    return json.dumps(_safe(value), ensure_ascii=False, sort_keys=True, default=str)


def _day(value):
    return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else value


def _datetime(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


def _amount(row):
    return Decimal(str(row["amount"])) if row.get("amount") is not None else from_minor(row.get("amount_minor", 0))


def _decimal(value):
    return Decimal(str(value)) if value not in (None, "") else None


def _money_sheet(headers, rows, money_columns):
    root = ET.fromstring(sheet(headers, rows))
    for row in root.findall("s:sheetData/s:row", N)[1:]:
        for cell in row.findall("s:c", N):
            column = "".join(ch for ch in cell.get("r") if ch.isalpha())
            if column in money_columns and cell.get("t") == "n":
                cell.set("s", "7")
    return xml(root)


def _split_oversized_cells(tables, metadata_count):
    """Reference long text in place and retain every ordered part on sheet 08.

    Process all original cells before appending continuation rows. Their shared
    metadata then reuses the already bounded sheet-08 metadata, avoiding a
    recursive expansion when metadata itself contains oversized text.
    """
    continuations = []
    reference_no = 0
    for sheet_index, table in enumerate(tables):
        for row_index, row in enumerate(table["rows"], 2):
            for column_index, value in enumerate(row, 1):
                if not isinstance(value, str) or len(value.encode("utf-16-le")) <= 32767 * 2:
                    continue
                reference_no += 1
                reference = "TEXT-{:06d}".format(reference_no)
                origin = "{}!{}{}".format(SHEETS[sheet_index], column(column_index), row_index)
                checksum = sha256(value.encode("utf-8")).hexdigest()
                # XML normalizes CR and the shared writer replaces forbidden
                # controls. JSON-string parts preserve such text byte for byte;
                # decode each part before concatenating and checking SHA-256.
                encoded = any(ord(char) < 32 and char not in ("\n", "\t") for char in value)
                chunk_size = JSON_CHUNK_SIZE if encoded else TEXT_CHUNK_SIZE
                chunks = [value[start:start + chunk_size] for start in range(0, len(value), chunk_size)]
                row[column_index - 1] = ("Текст {} · лист «08. Источники» · частей {} · SHA-256 {}"
                                         .format(reference, len(chunks), checksum))
                for part, chunk in enumerate(chunks, 1):
                    kind = "Продолжение текста (JSON-строка)" if encoded else "Продолжение текста"
                    fragment = json.dumps(chunk, ensure_ascii=False) if encoded else chunk
                    continuations.append([kind, reference, "", "", fragment,
                                          origin, checksum, part, len(chunks)])
    bounded_metadata = tables[-1]["rows"][0][-metadata_count:]
    tables[-1]["rows"].extend(row + bounded_metadata for row in continuations)


def _with_money_style(payload):
    result = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(result, "w", ZIP_DEFLATED) as target:
        for member in source.infolist():
            body = source.read(member.filename)
            if member.filename == "xl/styles.xml":
                root = ET.fromstring(body)
                formats = root.find("s:numFmts", N)
                ET.SubElement(formats, "{" + NS + "}numFmt", numFmtId="168", formatCode="0.00;[Red]-0.00")
                formats.set("count", str(len(formats)))
                cells = root.find("s:cellXfs", N)
                ET.SubElement(cells, "{" + NS + "}xf", numFmtId="168", fontId="0", fillId="0", borderId="0", xfId="0", applyNumberFormat="1")
                cells.set("count", str(len(cells)))
                body = xml(root)
            target.writestr(member, body)
    return result.getvalue()


def export_payroll_xlsx(report):
    """Export stored run values only; currencies and historical versions stay explicit."""
    period, version = report["period"], report["version"]
    lines = [dict(row) for row in report.get("lines", [])]
    rows = [dict(row) for row in report.get("rows", [])]
    names = {row["employee_id"]: row.get("full_name", "") for row in rows}
    meta_headers = ["Объект", "Объект (ID)", "Подрядчик", "Подрядчик (ID)", "Год", "Месяц",
                    "Период (ID)", "Версия (ID)", "Версия (№)", "Статус", "Расчёт-снимок (ID)",
                    "Норма полного месяца, ч", "Базовая версия (ID)", "Утверждено UTC", "Причина исправления",
                    "Отсечение знаний UTC (точно)", "Отпечаток источников", "Рассчитано UTC"]
    meta = [period.get("project_name", version.get("project_name", "")), str(period.get("project_id", "")),
            period.get("contractor_name", ""), str(period.get("contractor_id", "")), period["year"], period["month"],
            str(period["id"]), str(version["id"]), version.get("version_no", 1), version["status"],
            str(version.get("current_run_id") or ""), Decimal(str(version["norm_hours"])) if version.get("norm_hours") else None,
            str(version.get("base_version_id") or ""), _datetime(version.get("approved_at")), version.get("correction_reason", ""),
            (report.get("run") or {}).get("cutoff", ""), (report.get("run") or {}).get("source_digest", ""),
            _datetime(version.get("calculated_at"))]

    def table(headers, values, money=()):
        values = list(values)
        # A metadata row also makes an empty sheet useful and preserves its scope.
        return dict(headers=headers + meta_headers,
                    rows=[list(row) + meta for row in values] or [[None] * len(headers) + meta], money=money)

    totals = report.get("totals", {})
    summary = [["Управленческие начисления", currency, Decimal(str(total)), Decimal(str(report.get("delta", {}).get(currency, 0))),
                len([row for row in rows if row.get("currency") == currency]), _json(report.get("errors", [])),
                _json(report.get("warnings", [])), "Да" if report.get("source_changed") else "Нет"]
               for currency, total in sorted(totals.items())]
    bodies = [table(["Назначение", "Валюта", "Начислено", "Разница к базовой версии", "Работников", "Ошибки", "Предупреждения", "Источники изменились"], summary, ("C", "D"))]
    bodies.append(table(["Работник (ID)", "ФИО", "Система оплаты", "Валюта", "Базовая часть", "Сдельная часть", "Премия", "Доплата", "Корректировка", "Итого"],
        [[str(row["employee_id"]), row.get("full_name", ""), row.get("system", ""), row.get("currency", ""),
          *[Decimal(str(row.get(key, 0))) for key in ("base", "piece", "bonus", "supplement", "adjustment", "total")]] for row in rows], ("E", "F", "G", "H", "I", "J")))
    detail_headers = ["Строка (ID)", "Работник (ID)", "ФИО", "Компонент", "Валюта", "Начислено", "Редакция условий (ID)", "Редакция расценки (ID)", "Позиция (ID)", "Дата источника", "Источник-снимок", "Комментарий", "Часы", "Норма полного месяца, ч", "Ставка / оклад", "Распределённый объём", "Расценка за единицу", "Единица"]

    def detail_values(selected):
        values = []
        for row in selected:
            source = row.get("source", {})
            dependencies = source.get("dependencies", source.get("snapshot", {}).get("dependencies", {}))
            term = source.get("term", {})
            rate = dependencies.get("rate", source.get("rate", {}))
            root = dependencies.get("root", {})
            request = source.get("request", source.get("snapshot", {}).get("request", {}))
            values.append([str(row.get("id", "")), str(row["employee_id"]), names.get(row["employee_id"], row.get("full_name", "")),
                 row["component"], row["currency"], _amount(row), str(row.get("term_revision_id") or ""),
                 str(row.get("rate_revision_id") or ""), str(row.get("work_item_id") or ""),
                 _day(source.get("work_date", source.get("effective_date", root.get("confirmed_date")))),
                 _json(source), row.get("comment", ""), _decimal(source.get("hours")), _decimal(source.get("monthly_norm")),
                 _decimal(term.get("hourly_rate") or term.get("salary")), _decimal(request.get("quantity", source.get("quantity"))),
                 _decimal(rate.get("amount")), rate.get("unit", root.get("unit", ""))])
        return values

    for components in (("base_hourly",), ("base_salary",), ("piece",), ("bonus", "supplement", "adjustment")):
        bodies.append(table(detail_headers, detail_values([row for row in lines if row.get("component") in components]), ("F", "O", "Q")))
    applied = {}
    for row in lines:
        for kind, field in (("Условия", "term_revision_id"), ("Расценка", "rate_revision_id")):
            if row.get(field) is not None:
                identity = (kind, row[field], row["currency"])
                item = applied.setdefault(identity, dict(employees=set(), positions=set(), sources=[], snapshot={}))
                item["employees"].add(row["employee_id"])
                if row.get("work_item_id"):
                    item["positions"].add(row["work_item_id"])
                source = _safe(row.get("source", {}))
                dependencies = source.get("dependencies", source.get("snapshot", {}).get("dependencies", {}))
                if kind == "Условия":
                    candidates = [source.get("term", {})] + dependencies.get("terms", [])
                    item["snapshot"] = next((candidate for candidate in candidates if candidate.get("id") == row[field]), item["snapshot"])
                else:
                    candidate = dependencies.get("rate", source.get("rate", {}))
                    if candidate.get("id") == row[field]:
                        item["snapshot"] = candidate
                if source not in item["sources"]:
                    item["sources"].append(source)
    bodies.append(table(["Тип", "Редакция (ID)", "Валюта", "Работники (ID)", "Позиции (ID)", "Часовая ставка", "Оклад полного месяца", "Сдельная расценка", "Размер премии (сумма / %)", "Фиксированная доплата", "Действует с", "Действует до (не включая)", "Единица", "Ставки и условия из снимка"],
        [[kind, str(revision_id), currency, ", ".join(map(str, sorted(item["employees"]))),
          ", ".join(map(str, sorted(item["positions"]))), *[_decimal(item["snapshot"].get(key)) for key in ("hourly_rate", "salary", "amount", "bonus_value", "fixed_supplement")],
          _day(item["snapshot"].get("valid_from")), _day(item["snapshot"].get("valid_to")), item["snapshot"].get("unit", ""), _json(item["sources"])]
         for (kind, revision_id, currency), item in sorted(applied.items())], ("F", "G", "H", "J")))
    source_rows = [["Строка начисления", str(row.get("id", "")), str(row["employee_id"]), row["currency"], _json(row.get("source", {}))] for row in lines]
    if report.get("sources"):
        source_rows.append(["Зависимости расчёта", str(version.get("current_run_id") or ""), "", "", _json(report["sources"])])
    for run in report.get("runs", []):
        source_rows.append(["Расчёт: отсечение / digest", str(run["id"]), "", "", _json({key: run.get(key) for key in ("run_no", "cutoff", "source_digest", "created_at")})])
    bodies.append(table(["Тип источника", "Источник (ID)", "Работник (ID)", "Валюта", "Снимок / метаданные"] + CONTINUATION_HEADERS,
                        [row + [None] * len(CONTINUATION_HEADERS) for row in source_rows]))
    _split_oversized_cells(bodies, len(meta))
    worksheets = [_money_sheet(body["headers"], body["rows"], body["money"]) for body in bodies]
    return _with_money_style(pack_workbook(SHEETS, worksheets))
