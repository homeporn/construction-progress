"""Real local form requests and snapshot exports on isolated temporary SQLite."""
from decimal import Decimal
from io import BytesIO
from hashlib import sha256
import json
import re
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.event_repository import EventRepository
from app.repositories.labor_repository import LaborRepository
from app.repositories.payroll_repository import PayrollRepository
from app.repositories.payroll_rules_repository import PayrollRulesRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.timesheet_repository import TimesheetRepository
from app.services.payroll_xlsx import SHEETS, export_payroll_xlsx
from test_web import ORIGIN, webapp, client, get, post

N = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@pytest.fixture
def payroll_case(db):
    people = PersonnelRepository(db)
    contractor = people.save_contractor(dict(name="Тестовый подрядчик начислений", phone="SECRET-CONTACT-44"))
    specialty = people.save_specialty("Тестовая специальность", "WEB44")
    workers = [people.save_worker(dict(full_name="Тестовый Работник " + str(i), contractor_id=contractor,
        specialty_id=specialty, effective_date="2026-01-01")) for i in range(2)]
    timesheets = TimesheetRepository(db)
    sid = timesheets.create(contractor, 2026, 10)
    timesheets.set_entries(sid, [dict(employee_id=e, work_date="2026-10-01", code="Я", hours="8") for e in workers])
    timesheets.transition(sid, "submit")
    timesheets.transition(sid, "approve")
    return dict(db=db, people=people, contractor=contractor, workers=workers,
        rules=PayrollRulesRepository(db), repo=PayrollRepository(db))


def form(**values):
    return dict(workspace_id="1", operation_key=uuid4().hex, **values)


def terms_data(case, employee=None, **values):
    result = dict(employee_id=employee or case["workers"][0], contractor_id=case["contractor"], valid_from="2026-01-01",
        system="hourly", hourly_rate="350", salary="56000", base_kind="hourly", currency="RUB", bonus_kind="none",
        bonus_basis="base", bonus_value="0", performance_threshold="1", fixed_supplement="0", status="active", expected_revision="0")
    result.update(values)
    return form(**result)


def create_period(client, case):
    for employee in case["workers"]:
        assert post(client, "/payroll/terms", terms_data(case, employee)).status_code == 303
    response = post(client, "/payroll/periods/new", form(contractor_id=case["contractor"], year="2026", month="10", norm_hours="160", comment="Тестовый месяц"))
    assert response.status_code == 303
    period = case["repo"].list_periods()[0]
    return period["id"], case["repo"].get_version(period["id"])["id"]


def action(client, vid, name, **values):
    return post(client, f"/payroll/versions/{vid}/action", form(action=name, **values))


@pytest.mark.parametrize("path", ["/payroll", "/payroll/terms", "/payroll/rates", "/payroll/operations", "/payroll/periods", "/payroll/periods/new", "/payroll/claims"])
def test_all_payroll_sections_open_with_single_navigation_group(client, path):
    response = get(client, path)
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Управленческие начисления" in page and 'data-nav-group="payroll"' in page
    assert page.count('data-nav-group="payroll"') == 1
    assert 'name="workspace_id" value="1"' in page


@pytest.mark.parametrize("system,extra", [("hourly", {}), ("salary", {}), ("piece", {}),
    ("combined", dict(base_kind="salary", piece_enabled="1", bonus_kind="performance", bonus_basis="piece", bonus_value="20", performance_threshold="1.2", fixed_supplement="500"))])
def test_four_pay_system_forms_save_full_configuration(client, payroll_case, system, extra):
    case = payroll_case
    response = post(client, "/payroll/terms", terms_data(case, system=system, **extra))
    assert response.status_code == 303
    saved = case["rules"].list_terms()[0]
    assert saved["system"] == system
    if system == "combined":
        assert saved["base_kind"] == "salary" and saved["piece_enabled"] == 1
        assert saved["bonus_kind"] == "performance" and saved["bonus_basis"] == "piece"
        assert saved["bonus_value"] == "20" and saved["performance_threshold"] == "1.2"
        assert saved["fixed_supplement"] == "500"


def test_condition_interval_revision_is_append_only_and_stale_form_rejected(client, payroll_case):
    c = payroll_case
    first = terms_data(c, valid_to="2026-10-15")
    assert post(client, "/payroll/terms", first).status_code == 303
    assert post(client, "/payroll/terms", first).status_code == 303
    row = c["rules"].list_terms()[0]
    edit = terms_data(c, term_id=row["root_id"], expected_revision="1", valid_to="2026-10-15", hourly_rate="400")
    assert post(client, "/payroll/terms", edit).status_code == 303
    assert c["db"].execute("SELECT hourly_rate FROM payroll_terms ORDER BY id").fetchall()[0][0] == "350"
    assert post(client, "/payroll/terms", dict(edit, operation_key=uuid4().hex)).status_code == 400
    assert post(client, "/payroll/terms", terms_data(c, valid_from="2026-10-15", hourly_rate="450")).status_code == 303
    page = get(client, f'/payroll/terms?term_id={row["root_id"]}').get_data(as_text=True)
    assert 'value="400"' in page and "Новая редакция условий" in page


def test_technology_operation_and_rate_revision_forms(client, payroll_case):
    c = payroll_case
    catalog = dict(c["db"].execute("SELECT id,unit FROM work_catalog ORDER BY id LIMIT 1").fetchone())
    assert post(client, "/payroll/operations", form(catalog_id=catalog["id"], code="PREP", name="Подготовка", is_active="1")).status_code == 303
    operation = c["rules"].list_operations()[0]
    values = form(catalog_id=catalog["id"], contractor_id=c["contractor"], operation_id=operation["id"],
        unit=catalog["unit"], amount="12.35", currency="EUR", valid_from="2026-01-01", status="active", expected_revision="0")
    assert post(client, "/payroll/rates", values).status_code == 303
    rate = c["rules"].list_rates()[0]
    assert rate["operation_id"] == operation["id"] and rate["currency"] == "EUR"
    assert post(client, "/payroll/rates", dict(values, rate_id=rate["root_id"], expected_revision="1", amount="13.55", operation_key=uuid4().hex)).status_code == 303
    assert c["db"].execute("SELECT count(*) FROM payroll_rates").fetchone()[0] == 2
    assert "13,55" in get(client, "/payroll/rates").get_data(as_text=True)


@pytest.mark.parametrize("norm", ["", "0", "-1", "NaN", "1e4"])
def test_period_requires_explicit_valid_norm(client, payroll_case, norm):
    assert post(client, "/payroll/periods/new", form(contractor_id=payroll_case["contractor"], year="2026", month="10", norm_hours=norm)).status_code == 400
    assert not payroll_case["repo"].list_periods()


def test_complete_period_workflow_correction_and_previous_version_read_only(client, payroll_case):
    c = payroll_case
    pid, vid = create_period(client, c)
    for name in ("calculate", "submit", "approve", "close"):
        response = action(client, vid, name)
        assert response.status_code == 303, response.get_data(as_text=True)
    assert c["repo"].report(pid)["totals"] == {"RUB": Decimal("5600")}
    assert action(client, vid, "correction", reason="Уточнение премии").status_code == 303
    new_id = c["repo"].get_version(pid)["id"]
    assert new_id != vid
    assert action(client, new_id, "adjustment", employee_id=c["workers"][0], amount="-100", kind="adjustment", currency="RUB", effective_date="2026-10-01", reason="Исправление").status_code == 303
    assert action(client, new_id, "calculate").status_code == 303
    assert c["repo"].report(pid)["delta"] == {"RUB": Decimal("-100")}
    page = get(client, f"/payroll/periods/{pid}?version_id={vid}").get_data(as_text=True)
    assert "предыдущая версия" in page and "Ручная премия или корректировка" not in page
    assert action(client, vid, "calculate").status_code == 400
    assert c["repo"].report(pid, vid)["totals"] == {"RUB": Decimal("5600")}
    assert get(client, f"/payroll/periods/{pid}/export.xlsx?version_id={vid}").status_code == 200


def test_manual_bonus_needs_reason_and_does_not_change_primary_records(client, payroll_case):
    c = payroll_case
    pid, vid = create_period(client, c)
    before = [tuple(row) for row in c["db"].execute("SELECT * FROM timesheet_entries ORDER BY id")]
    values = dict(employee_id=c["workers"][0], amount="500", kind="bonus", currency="RUB", effective_date="2026-10-01")
    assert action(client, vid, "adjustment", **values).status_code == 400
    assert action(client, vid, "adjustment", reason="Ручная премия по решению оператора", **values).status_code == 303
    assert action(client, vid, "calculate").status_code == 303
    assert c["repo"].report(pid)["totals"]["RUB"] == Decimal("6100")
    assert before == [tuple(row) for row in c["db"].execute("SELECT * FROM timesheet_entries ORDER BY id")]


@pytest.mark.parametrize("method", ["equal", "hours", "ktu", "manual"])
def test_piece_form_preview_then_explicit_confirmation(client, payroll_case, item, plan, method):
    c = payroll_case
    brigade = BrigadeRepository(c["db"]).save(c["contractor"], "WEB44")
    for employee in c["workers"]:
        BrigadeRepository(c["db"]).assign(employee, brigade, "2026-01-01")
        assert post(client, "/payroll/terms", terms_data(c, employee, system="piece")).status_code == 303
    events = EventRepository(c["db"])
    events.add_fact(item, "3", "2026-10-01", event_key="web-pay-fact")
    root = events.add_confirmation(item, "3", "2026-10-01", event_key="web-pay-confirm", document_ref="Акт WEB44")
    catalog = dict(c["db"].execute("SELECT c.* FROM work_catalog c JOIN work_items w ON w.catalog_id=c.id WHERE w.id=?", (item,)).fetchone())
    assert post(client, "/payroll/rates", form(catalog_id=catalog["id"], contractor_id=c["contractor"], unit=catalog["unit"], amount="0.01", currency="RUB", valid_from="2026-01-01", expected_revision="0")).status_code == 303
    values = form(confirmation_event_id=root, contractor_id=c["contractor"], brigade_id=brigade, quantity="3", proof_ref="Подписанное распределение исполнителей", expected_revision="0")
    assert post(client, "/payroll/claims", dict(values, proof_ref="")).status_code == 400
    assert post(client, "/payroll/claims", values).status_code == 303
    claim = c["db"].execute("SELECT id FROM payroll_claims").fetchone()[0]
    pid = c["repo"].create_period(c["contractor"], 2026, 10, norm_hours="160")
    vid = c["repo"].get_version(pid)["id"]
    if method == "hours":
        LaborRepository(c["db"]).allocate(item, "2026-10-01", [dict(employee_id=employee, hours="8", expected_revision=0) for employee in c["workers"]])
    extra = {"weight_"+str(c["workers"][0]):"1", "weight_"+str(c["workers"][1]):"2"} if method == "ktu" else {}
    if method == "manual":
        extra = {"amount_"+str(c["workers"][0]):"0.01", "amount_"+str(c["workers"][1]):"0.02"}
    response = post(client, f"/payroll/versions/{vid}/piece-preview", form(claim_id=claim, quantity="3", method=method, employee_id=list(map(str,c["workers"])), **extra))
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    preview_id = re.search(r'name="preview_id" value="([^"]+)"', page).group(1)
    assert "0,02" in page and "0,01" in page and "Проверил исполнителей" in page
    assert c["db"].execute("SELECT count(*) FROM payroll_piece_batches").fetchone()[0] == 0
    assert post(client, f"/payroll/versions/{vid}/piece-confirm", form(preview_id=preview_id)).status_code == 400
    assert post(client, f"/payroll/versions/{vid}/piece-confirm", form(preview_id=preview_id, confirm_changes="1")).status_code == 303
    assert c["db"].execute("SELECT count(*) FROM payroll_piece_batches").fetchone()[0] == 1
    assert c["db"].execute("SELECT count(*) FROM confirmation_events").fetchone()[0] == 1


def test_payroll_local_request_guards_and_foreign_workspace(client, payroll_case):
    c = payroll_case
    data = terms_data(c)
    assert post(client, "/payroll/terms", data, token=False).status_code == 403
    assert post(client, "/payroll/terms", data, origin="https://example.invalid").status_code == 403
    assert post(client, "/payroll/terms", dict(data, workspace_id="999")).status_code == 403
    assert post(client, "/payroll/terms", {key:value for key,value in data.items() if key!="workspace_id"}).status_code == 400
    other = ProjectRepository(c["db"]).create_project("Другой расчётный объект", "PAY44-FOREIGN")
    people = PersonnelRepository(c["db"], other)
    foreign_contractor = people.save_contractor(dict(name="Чужой подрядчик"))
    assert post(client, "/payroll/terms", terms_data(c, contractor_id=foreign_contractor)).status_code == 400
    pid, vid = create_period(client, c)
    assert post(client, "/projects/select", dict(workspace_id="1", project_id=other)).status_code == 303
    assert get(client, f"/payroll/periods/{pid}").status_code == 400
    assert get(client, f"/payroll/periods/{pid}/export.xlsx").status_code == 400
    assert action(client, vid, "calculate").status_code == 403
    assert post(client, f"/payroll/versions/{vid}/action", dict(workspace_id=other, operation_key=uuid4().hex, action="calculate")).status_code == 400


def test_archived_project_is_read_only_and_period_history_is_bounded(client, payroll_case):
    c = payroll_case
    pid, vid = create_period(client, c)
    for index in range(55):
        c["repo"].add_adjustment(vid, c["workers"][0], "1", effective_date="2026-10-01", reason="Основание " + str(index))
    page = get(client, f"/payroll/periods/{pid}").get_data(as_text=True)
    assert "Основание 49" in page and "Основание 50" not in page
    page_two = get(client, f"/payroll/periods/{pid}?adjustment_page=2").get_data(as_text=True)
    assert "Основание 54" in page_two and "Основание 49" not in page_two
    ProjectRepository(c["db"]).set_archived(1, True)
    page = get(client, f"/payroll/periods/{pid}").get_data(as_text=True)
    assert "только для чтения" in page and "Ручная премия или корректировка" not in page
    assert action(client, vid, "calculate").status_code == 400


def test_export_has_eight_sheets_numeric_money_snapshot_metadata_and_no_contacts(client, payroll_case):
    c = payroll_case
    pid, vid = create_period(client, c)
    assert action(client, vid, "calculate").status_code == 303
    response = get(client, f"/payroll/periods/{pid}/export.xlsx")
    assert response.status_code == 200 and "attachment" in response.headers["Content-Disposition"]
    with ZipFile(BytesIO(response.data)) as archive:
        assert archive.testzip() is None
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        assert [node.get("name") for node in workbook.findall("s:sheets/s:sheet", N)] == list(SHEETS)
        summary = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        amount = summary.find("s:sheetData/s:row[@r='2']/s:c[@r='C2']", N)
        assert amount.get("t") == "n" and Decimal(amount.find("s:v",N).text) == Decimal("5600")
        assert amount.get("s") == "7"
        assert "RUB" in "".join(summary.itertext()) and "Расчёт-снимок (ID)" in "".join(summary.itertext())
        all_xml = b"".join(archive.read(path) for path in archive.namelist() if path.endswith(".xml"))
        assert b"SECRET-CONTACT-44" not in all_xml
        for path in archive.namelist():
            if path.endswith(".xml"):
                assert not ET.fromstring(archive.read(path)).findall(".//s:f", N)


def test_export_literal_user_text_and_typed_dates():
    report = dict(period=dict(id=1, project_id=1, project_name="=Unsafe", contractor_id=2, contractor_name="Тест", year=2026, month=10),
        version=dict(id=3, status="approved", current_run_id=4, norm_hours="160", approved_at="2026-11-01T12:00:00Z"),
        rows=[dict(employee_id=9223372036854775807, full_name="+Unsafe", currency="USD", total=Decimal("1.25"))], totals={"USD":Decimal("1.25")},
        lines=[dict(employee_id=9223372036854775807, component="base_hourly", amount_minor=125, currency="USD", source=dict(work_date="2026-10-01",phone="PRIVATE44"))])
    payload = export_payroll_xlsx(report)
    with ZipFile(BytesIO(payload)) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet3.xml"))
        assert root.find("s:sheetData/s:row[@r='2']/s:c[@r='J2']", N).get("s") == "2"
        detail = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
        assert detail.find("s:sheetData/s:row[@r='2']/s:c[@r='A2']", N).get("t") == "inlineStr"
        assert "PRIVATE44" not in "".join(root.itertext())
        assert "+Unsafe" in "".join(detail.itertext())


def test_export_oversized_cells_reconstruct_losslessly_on_existing_source_sheet():
    # Includes a string below 32767 Python characters but above Excel's UTF-16
    # limit, and long cells in details, applied rates, sources and metadata.
    long_comment = "👷" * 20000 + "\r\n\t\x01"
    correction_reason = "Уточнение " + "ж" * 35000
    source = dict(term=dict(id=7, hourly_rate="1.25"), detail="🙂漢字" * 12000)
    dependencies = dict(calculator=source, batches=[source] * 2)
    report = dict(period=dict(id=1, project_id=1, project_name="Тест", contractor_id=2, contractor_name="Подрядчик", year=2026, month=10),
        version=dict(id=3, status="approved", current_run_id=4, norm_hours="160", correction_reason=correction_reason),
        rows=[dict(employee_id=1, full_name="Работник", currency="RUB", total=Decimal("1.25"))], totals={"RUB": Decimal("1.25")},
        sources=dependencies, lines=[dict(id=5, employee_id=1, component="base_hourly", term_revision_id=7,
            amount_minor=125, currency="RUB", source=source, comment=long_comment)])
    payload = export_payroll_xlsx(report)
    with ZipFile(BytesIO(payload)) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        assert [node.get("name") for node in workbook.findall("s:sheets/s:sheet", N)] == list(SHEETS)
        all_cells = {}
        for index, name in enumerate(SHEETS, 1):
            root = ET.fromstring(archive.read(f"xl/worksheets/sheet{index}.xml"))
            cells = {cell.get("r"): cell for cell in root.findall("s:sheetData/s:row/s:c", N)}
            all_cells[name] = cells
            for cell in cells.values():
                node = cell.find("s:is/s:t", N)
                if node is not None:
                    assert len((node.text or "").encode("utf-16-le")) <= 32767 * 2
        source_sheet = ET.fromstring(archive.read("xl/worksheets/sheet8.xml"))
        pieces = {}
        for row in source_sheet.findall("s:sheetData/s:row", N)[1:]:
            values = {re.sub(r"[0-9]", "", cell.get("r")): "".join(cell.itertext()) for cell in row.findall("s:c", N)}
            if not values.get("A", "").startswith("Продолжение текста"):
                continue
            group = pieces.setdefault(values["B"], dict(parts={}, origin=values["F"], checksum=values["G"], count=int(Decimal(values["I"]))))
            group["parts"][int(Decimal(values["H"]))] = json.loads(values["E"]) if values["A"].endswith("(JSON-строка)") else values["E"]
            assert Decimal(values["O"]) == 10  # Original month metadata survives on every continuation.
        assert "Объект (ID)" in "".join(source_sheet.itertext())
        restored = []
        for reference, group in pieces.items():
            assert sorted(group["parts"]) == list(range(1, group["count"] + 1))
            text_value = "".join(group["parts"][part] for part in range(1, group["count"] + 1))
            assert sha256(text_value.encode("utf-8")).hexdigest() == group["checksum"]
            sheet_name, address = group["origin"].rsplit("!", 1)
            marker = "".join(all_cells[sheet_name][address].itertext())
            assert reference in marker and group["checksum"] in marker
            restored.append(text_value)
        assert long_comment in restored and correction_reason in restored
        assert json.dumps(source, ensure_ascii=False, sort_keys=True) in restored
        assert json.dumps([source], ensure_ascii=False, sort_keys=True) in restored
        assert json.dumps(dependencies, ensure_ascii=False, sort_keys=True) in restored
        money = all_cells[SHEETS[0]]["C2"]
        assert money.get("t") == "n" and money.get("s") == "7"
        assert Decimal(money.find("s:v", N).text) == Decimal("1.25")


def test_global_dashboard_has_no_accrual_totals(client, payroll_case):
    pid, vid = create_period(client, payroll_case)
    for name in ("calculate", "submit", "approve"):
        assert action(client, vid, name).status_code == 303
    page = get(client, "/").get_data(as_text=True)
    assert "Фонд управленческих начислений" not in page and "5600" not in page
    assert "5600" in get(client, "/payroll").get_data(as_text=True)
