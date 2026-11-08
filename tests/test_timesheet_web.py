"""Temporary-database browser boundary tests for monthly time records."""
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest

from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.timesheet_repository import TimesheetRepository
from app.services.xlsx_export import pack_workbook, sheet
from test_personnel import personnel, worker, DATE
from test_web import webapp, client, get, post


@pytest.fixture
def timesheet(db, personnel):
    one = worker(personnel, "Тестов Иван Иванович")
    two = worker(personnel, "Пробин Пётр Петрович")
    repo = TimesheetRepository(db)
    sid = repo.create(personnel["contractor"], 2026, 10)
    return dict(repo=repo, sid=sid, one=one, two=two, personnel=personnel)


def change(values, action, **overrides):
    report = values["repo"].report(values["sid"])
    data = dict(action=action, version_id=report["version"]["id"],
                revision=report["version"]["revision"], operation_key=uuid4().hex)
    data.update(overrides)
    return data


def path(values):
    return "/timesheets/" + str(values["sid"])


def cell(values, **overrides):
    data = dict(employee_id=values["one"], work_date=DATE, code="Я", hours="6,5", comment="Проверка")
    data.update(overrides)
    return change(values, "cell", **data)


def workbook(rows):
    return pack_workbook(["Табель"], [sheet(["ID работника", "ФИО", "Дата", "Код", "Часы"], rows, exact_column=False)])


@pytest.mark.parametrize("page", ["/timesheets", "/timesheets?history=1", "/timesheets/new", "/timesheets/template.xlsx"])
def test_timesheet_pages_open(client, page):
    assert get(client, page).status_code == 200


def test_create_timesheet_web_does_not_award_attendance(client, db, personnel):
    worker(personnel)
    data = dict(contractor_id=personnel["contractor"], year="2026", month="10", comment="Новый месяц", operation_key="web-month")
    result = post(client, "/timesheets/new", data)
    assert result.status_code == 303
    assert post(client, "/timesheets/new", data).status_code == 303
    assert db.execute("SELECT count(*) FROM timesheets").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM timesheet_workers").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM timesheet_entries").fetchone()[0] == 0


def test_month_matrix_cell_fraction_clear_and_no_record_distinction(client, timesheet):
    s = timesheet
    body = get(client, path(s)).get_data(as_text=True)
    assert "timesheet-matrix" in body and 'title="2026-10-31"' in body
    assert 'id="cell-editor"' in body and "Тестов Иван" in body
    assert post(client, path(s), cell(s)).status_code == 303
    report = s["repo"].report(s["sid"])
    assert report["total_hours"] == Decimal("6.5")
    assert post(client, path(s), cell(s, code="Б", hours="0")).status_code == 303
    person = next(w for w in s["repo"].report(s["sid"])["workers"] if w["employee_id"] == s["one"])
    assert person["absence_days"] == 1 and person["total_hours"] == 0
    assert post(client, path(s), cell(s, clear="1", code="", hours="")).status_code == 303
    assert all(not w["entries"] for w in s["repo"].report(s["sid"])["workers"])


@pytest.mark.parametrize("overrides", [dict(hours="-1"), dict(hours="24.1"), dict(code="Б", hours="8"),
                                       dict(work_date="2026-11-01"), dict(employee_id=9999), dict(hours="nan")])
def test_invalid_timesheet_input_is_russian_error_without_500(client, timesheet, overrides):
    result = post(client, path(timesheet), cell(timesheet, **overrides))
    assert result.status_code == 400
    assert "Traceback" not in result.get_data(as_text=True)
    assert timesheet["repo"].report(timesheet["sid"])["total_hours"] == 0


def test_repeated_cell_post_is_idempotent_and_stale_revision_fails(client, timesheet):
    s = timesheet
    data = cell(s)
    assert post(client, path(s), data).status_code == 303
    assert post(client, path(s), data).status_code == 303
    assert s["repo"].report(s["sid"])["total_hours"] == Decimal("6.5")
    assert post(client, path(s), dict(data, operation_key="different", hours="7")).status_code == 400


def test_bulk_preview_selected_workers_only_then_apply_once(client, timesheet):
    s = timesheet
    b = s["personnel"]["brigades"]
    bid = b.save(s["personnel"]["contractor"], "1")
    for eid in [s["one"], s["two"]]:
        b.assign(eid, bid, DATE)
    data = change(s, "bulk_preview", employee_id=[str(s["one"])], brigade_id=bid,
                  start_date=DATE, end_date="2026-10-21", code="Я", hours="8", comment="Выбран один")
    result = post(client, path(s), data)
    assert result.status_code == 303
    preview_url = result.headers["Location"]
    assert "Предварительный просмотр" in get(client, preview_url).get_data(as_text=True)
    assert s["repo"].report(s["sid"])["total_hours"] == 0
    apply = dict(action="apply", confirm_changes="1", operation_key="web-bulk-confirm")
    assert post(client, preview_url, apply).status_code == 303
    assert post(client, preview_url, apply).status_code == 303
    report = s["repo"].report(s["sid"])
    assert report["total_hours"] == 16
    assert not next(w for w in report["workers"] if w["employee_id"] == s["two"])["entries"]


def test_copy_preview_and_cancel_leave_cells_unchanged(client, timesheet):
    s = timesheet
    assert post(client, path(s), cell(s)).status_code == 303
    result = post(client, path(s), change(s, "copy_preview", source_date=DATE,
                                         target_start="2026-10-21", target_end="2026-10-22"))
    assert result.status_code == 303
    assert post(client, result.headers["Location"], dict(action="cancel")).status_code == 303
    assert s["repo"].report(s["sid"])["total_hours"] == Decimal("6.5")


def test_add_worker_and_row_note_without_attendance(client, timesheet):
    s = timesheet
    third = worker(s["personnel"], "Новый Работник")
    assert post(client, path(s), change(s, "add_workers", employee_id=[str(third)])).status_code == 303
    assert post(client, path(s), change(s, "worker_note", employee_id=third, note="Частичная занятость")).status_code == 303
    report = s["repo"].report(s["sid"])
    added = next(row for row in report["workers"] if row["employee_id"] == third)
    assert added["note"] == "Частичная занятость" and not added["entries"]
    body = get(client, path(s)).get_data(as_text=True)
    assert "Примечание" in body and "Частичная занятость" in body
    assert "Изменено примечание работника" in body
    assert "worker_note" not in body.split('id="timesheet-history"', 1)[1]


def test_comment_edit_audited_then_immutable_after_approval(client, timesheet):
    s = timesheet
    assert post(client, path(s), change(s, "comment", comment="Уточнённый состав смены")).status_code == 303
    report = s["repo"].report(s["sid"])
    assert report["version"]["comment"] == "Уточнённый состав смены" and not report["total_hours"]
    assert "Изменён комментарий табеля" in get(client, path(s)).get_data(as_text=True)
    for action in ["submit", "approve"]:
        assert post(client, path(s), change(s, action)).status_code == 303
    assert post(client, path(s), change(s, "comment", comment="Подмена")).status_code == 400
    assert s["repo"].report(s["sid"])["version"]["comment"] == "Уточнённый состав смены"


def test_submit_approve_readonly_and_correction_keep_original(client, timesheet):
    s = timesheet
    assert post(client, path(s), cell(s)).status_code == 303
    original = s["repo"].report(s["sid"])["version"]["id"]
    for action in ["submit", "approve"]:
        assert post(client, path(s), change(s, action)).status_code == 303
    report = s["repo"].report(s["sid"])
    assert report["version"]["status"] == "approved"
    body = get(client, path(s)).get_data(as_text=True)
    assert 'id="cell-editor"' not in body and "Создать корректирующую версию" in body
    assert post(client, path(s), cell(s, hours="7")).status_code == 400
    result = post(client, path(s), change(s, "correction", reason="Уточнение смены"))
    assert result.status_code == 303
    assert post(client, path(s), cell(s, hours="7")).status_code == 303
    assert s["repo"].report(s["sid"], original)["total_hours"] == Decimal("6.5")
    assert s["repo"].report(s["sid"])["total_hours"] == 7
    assert "V1" in get(client, "/timesheets?history=1").get_data(as_text=True)


def test_stale_approved_correction_form_cannot_create_version_from_newer_approval(client, timesheet):
    s = timesheet
    assert post(client, path(s), cell(s)).status_code == 303
    for action in ["submit", "approve"]:
        assert post(client, path(s), change(s, action)).status_code == 303
    stale_form = change(s, "correction", reason="Открытая ранее форма V1")
    assert post(client, path(s), change(s, "correction", reason="Разрешённая корректировка V1")).status_code == 303
    for action in ["submit", "approve"]:
        assert post(client, path(s), change(s, action)).status_code == 303
    result = post(client, path(s), stale_form)
    assert result.status_code == 400
    report = s["repo"].report(s["sid"])
    assert len(report["versions"]) == 2 and report["version"]["version_no"] == 2
    assert report["version"]["status"] == "approved"
    stale_page = get(client, path(s) + "?version_id=" + str(stale_form["version_id"])).get_data(as_text=True)
    assert "Создать корректирующую версию" not in stale_page
    assert "Открыть действующую версию V2" in stale_page


def test_xlsx_upload_preview_error_rows_and_commit(client, timesheet):
    s = timesheet
    url = path(s) + "/import"
    assert get(client, url).status_code == 200
    data = dict(version_id=s["repo"].report(s["sid"])["version"]["id"], revision="0",
                workbook=(BytesIO(workbook([[s["one"], "Тестов Иван Иванович", DATE, "Я", Decimal("4.25")]])), "табель.xlsx"))
    result = post(client, url, data)
    assert result.status_code == 303
    preview_url = result.headers["Location"]
    assert s["repo"].report(s["sid"])["total_hours"] == 0
    assert post(client, preview_url, dict(action="apply", confirm_changes="1", operation_key="import-confirm")).status_code == 303
    assert s["repo"].report(s["sid"])["total_hours"] == Decimal("4.25")
    data = dict(version_id=s["repo"].report(s["sid"])["version"]["id"], revision="1",
                workbook=(BytesIO(workbook([[9999, "Чужой Работник", DATE, "Я", 8]])), "ошибки.xlsx"))
    result = post(client, url, data)
    assert result.status_code == 303
    preview_url = result.headers["Location"]
    assert "disabled" in get(client, preview_url).get_data(as_text=True)
    assert post(client, preview_url, dict(action="apply", confirm_changes="1", operation_key="bad-import")).status_code == 400


def test_export_upload_bad_format_and_approved_import_rejection(client, timesheet):
    s = timesheet
    result = get(client, path(s) + "/export.xlsx")
    assert result.status_code == 200 and result.data.startswith(b"PK")
    data = dict(version_id=s["repo"].report(s["sid"])["version"]["id"], revision="0",
                workbook=(BytesIO(b"notxlsx"), "табель.xlsx"))
    result = post(client, path(s) + "/import", data)
    assert result.status_code == 400 and "Traceback" not in result.get_data(as_text=True)
    for action in ["submit", "approve"]:
        assert post(client, path(s), change(s, action)).status_code == 303
    result = post(client, path(s) + "/import", dict(revision="2", workbook=(BytesIO(workbook([])), "табель.xlsx")))
    assert result.status_code == 400


@pytest.mark.parametrize("bad", ["csrf", "origin", "workspace"])
def test_timesheet_writes_keep_csrf_origin_workspace_guards(client, timesheet, bad):
    data = cell(timesheet)
    kwargs = {}
    if bad == "csrf":
        kwargs["token"] = False
    elif bad == "origin":
        kwargs["origin"] = "https://example.com"
    else:
        data["workspace_id"] = "999"
    assert post(client, path(timesheet), data, **kwargs).status_code == 403


def test_foreign_timesheet_url_version_preview_and_contractor_rejected(client, timesheet, db):
    s = timesheet
    preview_id = s["repo"].preview_bulk(s["sid"], [s["one"]], DATE, DATE, "Я", "8")
    project = ProjectRepository(db).create_project("Другой объект", "OTHER-TIME")
    assert post(client, "/projects/select", dict(project_id=project)).status_code == 303
    for url in [path(s), path(s) + "/export.xlsx", path(s) + "/import", path(s) + "/preview/" + preview_id]:
        assert get(client, url).status_code == 400
    assert post(client, path(s), cell(s)).status_code == 400
    assert post(client, "/timesheets/new", dict(contractor_id=s["personnel"]["contractor"], year=2026,
                                                month=10, operation_key="foreign-company")).status_code == 400
    assert s["repo"].report(s["sid"])["total_hours"] == 0
