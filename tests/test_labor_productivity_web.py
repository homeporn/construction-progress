"""Real temporary SQLite coverage for the labor UI and its offline workbook."""
from decimal import Decimal
from html import unescape
from io import BytesIO
import re
from urllib.parse import urlencode
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.labor_repository import LaborRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.timesheet_repository import TimesheetRepository
from app.services.labor_productivity import LaborProductivityService
from app.services.labor_xlsx import SHEETS, export_labor_xlsx
from test_web import ORIGIN, client, get, post, webapp

DATE = "2026-10-10"
PERIOD = dict(start_date="2026-10-01", end_date="2026-10-31")
N = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@pytest.fixture
def labor_context(db, item, plan, events):
    people = PersonnelRepository(db)
    contractor = people.save_contractor(dict(name="Строительная организация", inn="1234567890",
        phone="СЕКРЕТНЫЙ-ТЕЛЕФОН", contact_person="СЕКРЕТНЫЙ-КОНТАКТ"))
    specialty = people.save_specialty("Монтажник", "LABOR-WEB")
    workers = [people.save_worker(dict(full_name=name, contractor_id=contractor,
        specialty_id=specialty, effective_date="2026-10-01"))
        for name in ("Работник Первый", "Работник Второй")]
    brigade = BrigadeRepository(db).save(contractor, "Л-1")
    for employee in workers:
        BrigadeRepository(db).assign(employee, brigade, "2026-10-01")
    timesheets = TimesheetRepository(db)
    timesheet = timesheets.create(contractor, 2026, 10)
    timesheets.set_entries(timesheet, [dict(employee_id=employee, work_date=DATE, code="Я", hours="8") for employee in workers])
    timesheets.transition(timesheet, "submit")
    timesheets.transition(timesheet, "approve")
    fact = events.add_fact(item, "16", DATE, event_key="labor-web-fact", recorded_at="2026-10-10T12:00:00Z")
    return dict(item=item, plan=plan, contractor=contractor, workers=workers, brigade=brigade,
                timesheets=timesheets, timesheet=timesheet, fact=fact, repository=LaborRepository(db))


def path(tab="", **query):
    return "/labor" + ("/" + tab if tab else "") + "?" + urlencode(dict(PERIOD, **query))


def allocation_path(c, **query):
    return path("allocations", work_date=DATE, work_item_id=c["item"], **query)


def allocation(c, **values):
    data = dict(workspace_id="1", action="allocate", work_item_id=c["item"], work_date=DATE,
        employee_id=c["workers"][0], hours="6,125", expected_revision="0", operation_key="web-allocation")
    data.update(values)
    return data


def bulk(c, **values):
    source = c["repository"].available_hours(DATE)[0]
    employee = source["employee_id"]
    data = dict(workspace_id="1", action="bulk_preview", work_item_id=c["item"], work_date=DATE,
        employee_id=[str(employee)], operation_key="bulk-web-operation",
        **{"hours_" + str(employee): "4.125", "revision_" + str(employee): "0", "entry_" + str(employee): str(source["entry_id"])})
    data.update(values)
    return data


def preview_token(response):
    return unescape(re.search(r'name="preview_token" value="([^"]+)"', response.get_data(as_text=True)).group(1))


@pytest.mark.parametrize("tab", ["", "allocations", "attributions", "works", "contractors", "brigades", "history", "export.xlsx"])
def test_empty_labor_pages_and_export_open(client, tab):
    response = get(client, path(tab))
    assert response.status_code == 200
    if tab != "export.xlsx":
        assert "Производительность" in response.get_data(as_text=True)


def test_missing_approved_hours_are_unknown_in_overview(client):
    page = get(client, path()).get_data(as_text=True)
    assert "Нет утверждённых часов" in page
    assert re.search(r"data-worked-hours>Нет данных<", page)


def test_individual_allocation_is_exact_idempotent_and_keeps_primary_journals(client, db, labor_context):
    c = labor_context
    before = {table: [tuple(r) for r in db.execute("SELECT * FROM " + table)]
              for table in ("timesheet_entries", "fact_events", "confirmation_events")}
    for _ in range(2):
        assert post(client, allocation_path(c), allocation(c)).status_code == 303
    rows = c["repository"].hour_rows()
    assert len(rows) == 1 and rows[0]["hours"] == Decimal("6.125")
    assert before == {table: [tuple(r) for r in db.execute("SELECT * FROM " + table)] for table in before}
    page = get(client, allocation_path(c)).get_data(as_text=True)
    assert "6,125" in page and "Снять распределение" in page


@pytest.mark.parametrize("violation", ["csrf", "origin", "workspace"])
def test_labor_mutations_share_security_guards(client, labor_context, violation):
    c = labor_context
    data = allocation(c)
    options = {}
    if violation == "csrf":
        options["token"] = False
    elif violation == "origin":
        options["origin"] = "https://foreign.invalid"
    else:
        data["workspace_id"] = "999"
    assert post(client, allocation_path(c), data, **options).status_code == 403
    assert not c["repository"].hour_rows()


def test_historical_forms_are_read_only_and_historical_post_fails(client, labor_context):
    c = labor_context
    url = allocation_path(c, knowledge_cutoff="2026-10-31T23:59:59Z")
    page = get(client, url).get_data(as_text=True)
    assert "только для чтения" in page and 'name="action" value="allocate"' not in page
    assert post(client, url, allocation(c)).status_code == 400
    assert not c["repository"].hour_rows()


def test_stale_revision_and_foreign_position_never_write(client, db, labor_context):
    c = labor_context
    assert post(client, allocation_path(c), allocation(c)).status_code == 303
    assert post(client, allocation_path(c), allocation(c, hours="7", operation_key="stale")).status_code == 400
    foreign = ProjectRepository(db).create_project("Другой объект", "LABOR-FOREIGN")
    assert get(client, path(project_id=foreign)).status_code == 400
    assert get(client, path(building_id="999999")).status_code == 400
    assert c["repository"].hour_rows()[0]["hours"] == Decimal("6.125")


def test_bulk_preview_has_no_writes_and_confirmation_replays_once(client, labor_context):
    c = labor_context
    url = allocation_path(c, brigade_id=c["brigade"])
    response = post(client, url, bulk(c))
    assert response.status_code == 200 and "Предварительный просмотр" in response.get_data(as_text=True)
    assert not c["repository"].hour_rows()
    data = dict(workspace_id="1", action="bulk_confirm", preview_token=preview_token(response), confirm_changes="1")
    assert post(client, url, data).status_code == 303
    assert post(client, url, data).status_code == 303
    rows = c["repository"].hour_rows()
    assert len(rows) == 1 and rows[0]["hours"] == Decimal("4.125")


def test_bulk_tampering_and_changed_approved_source_are_rejected(client, labor_context):
    c = labor_context
    url = allocation_path(c, brigade_id=c["brigade"])
    response = post(client, url, bulk(c))
    token = preview_token(response)
    data = dict(workspace_id="1", action="bulk_confirm", preview_token=token + "invalid", confirm_changes="1")
    assert post(client, url, data).status_code == 400
    t = c["timesheets"]
    version = t.create_correction(c["timesheet"], "Уточнение времени")
    t.set_entries(c["timesheet"], [dict(employee_id=c["workers"][0], work_date=DATE, code="Я", hours="7")], version_id=version)
    t.transition(c["timesheet"], "submit", version_id=version)
    t.transition(c["timesheet"], "approve", version_id=version)
    assert post(client, url, dict(data, preview_token=token)).status_code == 400
    assert not c["repository"].hour_rows()


def test_corrected_absence_keeps_old_allocation_clearable(client, labor_context):
    c = labor_context
    c["repository"].allocate(c["item"], DATE, [dict(employee_id=c["workers"][0], hours="8")])
    t = c["timesheets"]
    version = t.create_correction(c["timesheet"], "Исправлена ошибочная явка")
    t.set_entries(c["timesheet"], [dict(employee_id=c["workers"][0], work_date=DATE, clear=True)], version_id=version)
    t.transition(c["timesheet"], "submit", version_id=version)
    t.transition(c["timesheet"], "approve", version_id=version)
    page = get(client, allocation_path(c)).get_data(as_text=True)
    assert "Снять распределение" in page and "Конфликт" in page
    assert post(client, allocation_path(c), allocation(c, hours="0", expected_revision="1", reason="Снятие ошибочного распределения")).status_code == 303
    assert c["repository"].hour_rows()[0]["hours"] == 0


def test_changed_historical_brigade_invalidates_bulk_confirmation(client, labor_context):
    c = labor_context
    url = allocation_path(c, brigade_id=c["brigade"])
    response = post(client, url, bulk(c))
    assert response.status_code == 200
    token = preview_token(response)
    brigades = BrigadeRepository(c["repository"].db)
    other = brigades.save(c["contractor"], "Л-2")
    brigades.assign(c["workers"][0], other, "2026-10-05")
    data = dict(workspace_id="1", action="bulk_confirm", preview_token=token, confirm_changes="1")
    assert post(client, url, data).status_code == 400
    assert not c["repository"].hour_rows()


def test_volume_attribution_changes_only_analytical_assignment(client, db, labor_context):
    c = labor_context
    before = [tuple(row) for row in db.execute("SELECT * FROM fact_events")]
    data = dict(workspace_id="1", fact_event_id=c["fact"], contractor_id=c["contractor"], brigade_id=c["brigade"],
        quantity="10,25", expected_revision="0", operation_key="web-volume")
    assert post(client, path("attributions"), data).status_code == 303
    assert c["repository"].attribution_rows()[0]["quantity"] == Decimal("10.25")
    assert before == [tuple(row) for row in db.execute("SELECT * FROM fact_events")]
    assert post(client, path("attributions"), dict(data, quantity="16.1", operation_key="stale-volume")).status_code == 400


def test_history_fetches_only_requested_server_page(client, monkeypatch):
    calls = []
    def history(self, limit=50, offset=0, **kwargs):
        calls.append((limit, offset))
        return [dict(id="test-" + str(i), recorded_at="2026-10-10T12:00:00Z", kind="hours", reason="Проверка") for i in range(51)]
    monkeypatch.setattr(LaborRepository, "history", history)
    response = get(client, path("history", page="2"))
    assert response.status_code == 200 and calls == [(51, 50)]
    page = response.get_data(as_text=True)
    assert "test-49" in page and "test-50" not in page and "Следующая" in page


def test_single_writes_bulk_previews_and_history_skip_full_analytics_build(client, labor_context, monkeypatch):
    c = labor_context
    def forbidden(*args, **kwargs):
        raise AssertionError("A single operation must not rebuild the full analytics projection")
    monkeypatch.setattr(LaborProductivityService, "build", forbidden)
    assert post(client, allocation_path(c), allocation(c)).status_code == 303
    volume = dict(workspace_id="1", fact_event_id=c["fact"], contractor_id=c["contractor"],
                  quantity="4", expected_revision="0", operation_key="fast-volume")
    assert post(client, path("attributions"), volume).status_code == 303
    assert get(client, path("history")).status_code == 200
    employee = c["workers"][1]
    source = next(row for row in c["repository"].available_hours(DATE) if row["employee_id"] == employee)
    data = dict(workspace_id="1", action="bulk_preview", work_item_id=c["item"], work_date=DATE,
                employee_id=[str(employee)], operation_key="fast-bulk",
                **{"hours_" + str(employee): "4", "revision_" + str(employee): "0", "entry_" + str(employee): str(source["entry_id"])})
    url = allocation_path(c, brigade_id=c["brigade"])
    preview = post(client, url, data)
    assert preview.status_code == 200
    confirm = dict(workspace_id="1", action="bulk_confirm", preview_token=preview_token(preview), confirm_changes="1")
    assert post(client, url, confirm).status_code == 303


def test_historical_history_omits_operations_registered_after_cutoff(client, labor_context):
    c = labor_context
    c["repository"].allocate(c["item"], DATE, [dict(employee_id=c["workers"][0], hours="2")],
        operation_key="future-knowledge-operation", recorded_at="2027-01-01T00:00:00Z")
    page = get(client, path("history", knowledge_cutoff="2026-12-31T23:59:59Z")).get_data(as_text=True)
    assert "future-knowledge-operation" not in page


def test_allocations_render_at_most_fifty_workers_per_page(client, labor_context, monkeypatch):
    c = labor_context
    source = c["repository"].available_hours(DATE)[0]
    def available(self, *args, **kwargs):
        return [dict(source, employee_id=1000 + index, full_name="Работник страницы " + str(index)) for index in range(101)]
    monkeypatch.setattr(LaborRepository, "available_hours", available)
    page = get(client, allocation_path(c)).get_data(as_text=True)
    assert page.count('data-labor-worker="') == 50
    page = get(client, allocation_path(c, page=3)).get_data(as_text=True)
    assert page.count('data-labor-worker="') == 1 and "Работник страницы 100" in page


def test_workbook_has_seven_sheets_exact_values_dates_filters_and_no_excess_pii(client, labor_context, db):
    c = labor_context
    c["repository"].allocate(c["item"], DATE, [dict(employee_id=c["workers"][0], hours="6.125")])
    report = LaborProductivityService(db).build(PERIOD["start_date"], PERIOD["end_date"], filters={"building_id": 1})
    payload = export_labor_xlsx(report)
    with ZipFile(BytesIO(payload)) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        names = [row.get("name") for row in workbook.findall("s:sheets/s:sheet", N)]
        assert names == list(SHEETS) and all(len(name) <= 31 for name in names)
        all_xml = "".join(archive.read("xl/worksheets/sheet" + str(i) + ".xml").decode() for i in range(1, 8))
        assert "СЕКРЕТНЫЙ" not in all_xml and "1234567890" not in all_xml
        assert "&quot;building_id&quot;: 1" in all_xml or '"building_id": 1' in all_xml
        assert "6.125" in all_xml and "remaining" not in all_xml
        for i in range(1, 8):
            root = ET.fromstring(archive.read("xl/worksheets/sheet" + str(i) + ".xml"))
            assert root.find(".//s:f", N) is None
            assert root.find(".//s:c[@s='2']", N) is not None
        numeric = ET.fromstring(archive.read("xl/worksheets/sheet7.xml")).find(".//s:c[@r='I2']", N)
        assert numeric.get("t") == "n" and Decimal(numeric.find("s:v", N).text) == Decimal("6.125")
    response = get(client, path("export.xlsx", building_id=1))
    assert response.status_code == 200 and response.data.startswith(b"PK")


def test_xlsx_unassigned_sheet_combines_exact_hour_and_volume_remainders_and_frozen_norm(db, labor_context, events):
    c = labor_context
    fact = events.add_fact(c["item"], "100", DATE, event_key="xlsx-volume-100", recorded_at="2026-10-10T13:00:00Z")
    c["repository"].allocate(c["item"], DATE, [dict(employee_id=c["workers"][0], hours="6.125")])
    c["repository"].attribute(fact, c["contractor"], "60", brigade_id=c["brigade"])
    report = LaborProductivityService(db).build(PERIOD["start_date"], PERIOD["end_date"])
    event = next(row for row in report["events"] if row["id"] == fact)
    assert event["available"] == 100 and event["assigned"] == 60 and event["remaining"] == 40
    before = {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)]
              for table in ("timesheet_entries", "fact_events", "confirmation_events")}
    payload = export_labor_xlsx(report)

    def values(row):
        return {re.sub(r"[0-9]", "", cell.get("r")): cell.find("s:v", N).text
                if cell.get("t") == "n" else "".join(cell.itertext()) for cell in row}

    with ZipFile(BytesIO(payload)) as archive:
        unassigned = ET.fromstring(archive.read("xl/worksheets/sheet6.xml"))
        rows = unassigned.findall("s:sheetData/s:row", N)[1:]
        volume_rows = [row for row in rows if values(row).get("A") == "Объём" and Decimal(values(row).get("D", "0")) == fact]
        assert volume_rows, [values(row) for row in rows]
        volume = volume_rows[0]
        volume_values = values(volume)
        assert [Decimal(volume_values[key]) for key in ("H", "I", "J")] == [Decimal(100), Decimal(60), Decimal(40)]
        assert all(volume.find("s:c[@r='" + column + volume.get("r") + "']", N).get("t") == "n" for column in ("H", "I", "J"))
        hours = next(row for row in rows if values(row).get("A") == "Часы" and Decimal(values(row).get("C", "0")) == c["workers"][0])
        assert Decimal(values(hours)["J"]) == Decimal("1.875")
        detail = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
        allocation = detail.find("s:sheetData/s:row[@r='2']", N)
        assert Decimal(values(allocation)["J"]) == Decimal("6.125")
        raw = ET.fromstring(archive.read("xl/worksheets/sheet7.xml"))
        for row in raw.findall("s:sheetData/s:row", N)[1:]:
            assert Decimal(values(row)["M"]) == c["plan"]
            assert Decimal(values(row)["N"]) == Decimal("0.8")
            assert row.find("s:c[@r='N" + row.get("r") + "']", N).get("t") == "n"
    assert before == {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)] for table in before}
    events.add_fact(c["item"], "-50", "2026-10-11", correction_of_id=fact,
                    event_key="xlsx-conflicting-remainder", recorded_at="2026-10-11T13:00:00Z")
    conflict = LaborProductivityService(db).build(PERIOD["start_date"], PERIOD["end_date"])
    with ZipFile(BytesIO(export_labor_xlsx(conflict))) as archive:
        rows = ET.fromstring(archive.read("xl/worksheets/sheet6.xml")).findall("s:sheetData/s:row", N)[1:]
        volume = next(row for row in rows if values(row).get("A") == "Объём" and Decimal(values(row).get("D", "0")) == fact)
        assert Decimal(values(volume)["J"]) == Decimal("-10") and values(volume)["N"] == "Нет"
