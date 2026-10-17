import re
from decimal import Decimal

import pytest

from app.database import connect
from app.time_utils import instant
from app.web import create_app

ORIGIN = "http://127.0.0.1:5100"


@pytest.fixture
def webapp(db):
    path = db.execute("PRAGMA database_list").fetchone()["file"]
    return create_app({"DATABASE": path, "TESTING": True, "LOCAL_ORIGIN": ORIGIN, "SECRET_KEY": "test-only-secret"})


@pytest.fixture
def client(webapp):
    return webapp.test_client()


def get(client, path):
    return client.get(path, base_url=ORIGIN)


def post(client, path, data, *, origin=ORIGIN, token=True, headers=None):
    page = get(client, "/").get_data(as_text=True)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    values = dict(data)
    if token:
        values["csrf_token"] = csrf
    request_headers = {"Origin": origin} if origin is not None else {}
    request_headers.update(headers or {})
    return client.post(path, base_url=ORIGIN, data=values, headers=request_headers)


def add_floor(client, **overrides):
    data = dict(action="add", building_id="1", floor_code="1", sort_order="1")
    data.update(overrides)
    return post(client, "/buildings/floors", data)


def event_data(item, *, quantity="600", key="web-event", confirmation=False):
    result = dict(discipline="CONSTRUCTION", building_id="1", floor_id="1", work_item_id=str(item),
                  quantity_delta=quantity, business_date="2026-01-10", event_key=key, comment="Обмер на площадке")
    if confirmation:
        result["document_ref"] = "Акт №1"
    return result


@pytest.mark.parametrize("path", ["/", "/buildings", "/catalog", "/volumes", "/facts", "/confirmations", "/progress"])
def test_all_seven_sections_open(client, path):
    response = get(client, path)
    assert response.status_code == 200
    assert '<html lang="ru">' in response.get_data(as_text=True)
    assert "Content-Security-Policy" in response.headers
    assert response.headers["Referrer-Policy"] == "same-origin"


def test_empty_dashboard_shows_no_data_and_never_artificial_zero(client):
    page = get(client, "/").get_data(as_text=True)
    assert "Нет данных" in page
    assert "0,0%" not in page
    assert "Корпус А" in page and "Корпус B" in page and "Корпус C" in page
    assert "Физическая готовность СМР" in page
    assert "Подтверждённая готовность проектирования" in page


def test_buildings_json_and_floor_creation(client, db):
    response = get(client, "/api/buildings")
    assert response.status_code == 200
    assert [row["code"] for row in response.json] == ["A", "B", "C"]
    assert add_floor(client, floor_code="-2", sort_order="-2").status_code == 303
    assert db.execute("SELECT floor_code,sort_order FROM floors").fetchone()[:] == ("-2", -2)
    assert "-2" in get(client, "/buildings").get_data(as_text=True)


def test_independent_floor_structures_and_unused_floor_deletion(client, db):
    assert add_floor(client).status_code == 303
    assert add_floor(client, building_id="2", floor_code="Тех", sort_order="100").status_code == 303
    first = db.execute("SELECT id FROM floors WHERE building_id=1").fetchone()[0]
    assert post(client, "/buildings/floors", dict(action="delete", floor_id=first)).status_code == 303
    assert db.execute("SELECT building_id,floor_code FROM floors").fetchone()[:] == (2, "Тех")


def test_used_floor_can_be_reordered_but_not_deleted(client, db, item, plan):
    assert post(client, "/buildings/floors", dict(action="reorder", floor_id="1", sort_order="-5")).status_code == 303
    assert db.execute("SELECT sort_order FROM floors WHERE id=1").fetchone()[0] == -5
    response = post(client, "/buildings/floors", dict(action="delete", floor_id="1"))
    assert response.status_code == 400
    assert "удаление запрещено" in response.get_data(as_text=True)


def test_complete_workflow_from_floor_to_60_and_45_percent(client, db):
    assert add_floor(client).status_code == 303
    assert post(client, "/volumes/items", dict(building_id="1", floor_id="1", catalog_id="1", discipline="CONSTRUCTION")).status_code == 303
    item_id = db.execute("SELECT id FROM work_items").fetchone()[0]
    assert post(client, "/volumes/plans", dict(discipline="CONSTRUCTION")).status_code == 303
    version_id = db.execute("SELECT id FROM plan_versions").fetchone()[0]
    response = post(client, "/volumes/lines", dict(version_id=version_id, work_item_id=item_id,
                                                  planned_quantity="1000", labor_norm="0,8"))
    assert response.status_code == 303
    assert db.execute("SELECT labor_norm,typeof(labor_norm) FROM plan_lines").fetchone()[:] == ("0.8", "text")
    assert post(client, f"/volumes/plans/{version_id}/approve", {}).status_code == 303
    assert post(client, "/facts", event_data(item_id)).status_code == 303
    assert post(client, "/confirmations", event_data(item_id, quantity="450", key="c1", confirmation=True)).status_code == 303
    page = get(client, "/").get_data(as_text=True)
    assert "60,0%" in page and "45,0%" in page
    assert page.count("Нет данных") >= 2  # Design remains independent and empty.
    assert "60,0%" in get(client, "/volumes").get_data(as_text=True)
    progress = get(client, "/progress").get_data(as_text=True)
    assert "60,0%" in progress and "45,0%" in progress
    assert "800" in progress and "320" in progress
    assert db.execute("SELECT comment FROM confirmation_events").fetchone()[0] == "Обмер на площадке"


def test_approved_version_cannot_be_edited_over_http(client, db, item, plan):
    response = post(client, "/volumes/lines", dict(version_id=plan, work_item_id=item, planned_quantity="1200", labor_norm="1"))
    assert response.status_code == 400
    assert "Утверждённый план изменять нельзя" in response.get_data(as_text=True)
    assert db.execute("SELECT planned_quantity FROM plan_lines WHERE plan_version_id=?", (plan,)).fetchone()[0] == "1000"
    response = post(client, "/volumes/plans", dict(discipline="CONSTRUCTION", base_version_id=plan))
    assert response.status_code == 303
    clone = db.execute("SELECT max(id) FROM plan_versions").fetchone()[0]
    assert clone != plan
    assert post(client, "/volumes/lines", dict(version_id=clone, work_item_id=item, planned_quantity="1200", labor_norm="1")).status_code == 303
    assert db.execute("SELECT planned_quantity FROM plan_lines WHERE plan_version_id=?", (plan,)).fetchone()[0] == "1000"


def test_draft_preview_uses_engine_and_missing_norm_blocks_approval(client, db, item):
    post(client, "/volumes/plans", dict(discipline="CONSTRUCTION"))
    version = db.execute("SELECT id FROM plan_versions").fetchone()[0]
    assert post(client, "/volumes/lines", dict(version_id=version, work_item_id=item, planned_quantity="1000", labor_norm="")).status_code == 303
    page = get(client, "/volumes").get_data(as_text=True)
    assert "Не задан норматив трудоёмкости" in page
    assert "Предварительный расчёт черновика" in page
    assert post(client, f"/volumes/plans/{version}/approve", {}).status_code == 400
    assert db.execute("SELECT status FROM plan_versions").fetchone()[0] == "DRAFT"
    history = get(client, f"/progress?version_id={version}").get_data(as_text=True)
    assert "Версия не утверждена" in history


def test_confirmation_exceeding_actual_is_rejected_without_writes(client, db, item, plan):
    post(client, "/facts", event_data(item, quantity="600"))
    audits = db.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    response = post(client, "/confirmations", event_data(item, quantity="601", key="excess", confirmation=True))
    assert response.status_code == 400
    assert "Подтверждение превышает факт" in response.get_data(as_text=True)
    assert db.execute("SELECT count(*) FROM confirmation_events").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM audit_log").fetchone()[0] == audits


def test_historical_report_filters_business_and_knowledge_dates(client, item, plan):
    post(client, "/facts", event_data(item))
    early = get(client, f"/progress?version_id={plan}&report_date=2026-01-31&knowledge_cutoff=2026-01-31T23:59:59Z")
    assert "0,0%" in early.get_data(as_text=True)
    assert "60,0%" not in early.get_data(as_text=True)
    restated = get(client, f"/progress?version_id={plan}&report_date=2026-01-31&knowledge_cutoff={instant()}")
    assert "60,0%" in restated.get_data(as_text=True)
    before_work = get(client, f"/progress?version_id={plan}&report_date=2026-01-09&knowledge_cutoff={instant()}")
    assert "60,0%" not in before_work.get_data(as_text=True)


def test_fact_correction_is_a_new_event_and_duplicate_submission_is_rejected(client, db, item, plan):
    assert post(client, "/facts", event_data(item)).status_code == 303
    assert post(client, "/facts", event_data(item)).status_code == 400
    original = db.execute("SELECT id FROM fact_events").fetchone()[0]
    correction = event_data(item, quantity="-20", key="corrected")
    correction["correction_of_id"] = str(original)
    assert post(client, "/facts", correction).status_code == 303
    assert db.execute("SELECT count(*) FROM fact_events").fetchone()[0] == 2
    assert "58,0%" in get(client, "/").get_data(as_text=True)
    assert db.execute("SELECT quantity_delta FROM fact_events WHERE id=?", (original,)).fetchone()[0] == "600"


def test_catalog_search_creation_edit_and_duplicate_protection(client, db, item):
    page = get(client, "/catalog?q=ШТУКАТУРКА").get_data(as_text=True)
    assert "ОТД.СТ.01" in page
    assert "ОТД.ПЛ.01" not in page
    values = dict(code="ПР.01", name="Раздел АР", element="Документация", work_type="Разработка", unit="комплект")
    assert post(client, "/catalog", values).status_code == 303
    assert post(client, "/catalog", values).status_code == 400
    catalog_id = db.execute("SELECT id FROM work_catalog WHERE code='ПР.01'").fetchone()[0]
    values.update(catalog_id=catalog_id, name="Раздел КР")
    assert post(client, "/catalog", values).status_code == 303
    values.update(catalog_id="1")
    assert post(client, "/catalog", values).status_code == 400
    assert db.execute("SELECT unit FROM work_catalog WHERE id=1").fetchone()[0] == "м²"


def test_volume_filters_and_cross_discipline_version_are_validated(client, work, item, plan):
    floor = work.add_floor(2, "Тех", 10)
    work.add_work_item(2, floor, 2)
    page = get(client, f"/volumes?building_id=1&version_id={plan}").get_data(as_text=True)
    assert 'name="work_item_id"' not in page  # No edit form for an approved version.
    assert get(client, f"/volumes?building_id=1&floor_id={floor}").status_code == 400
    assert get(client, f"/progress?discipline=DESIGN&version_id={plan}").status_code == 400
    assert get(client, "/volumes?discipline=WRONG").status_code == 400


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "1e3", "-10", "1); DROP TABLE buildings; --", "0.0000000001"])
def test_bad_plan_quantities_are_rejected(client, db, work, item, bad):
    version = work.create_plan_version("CONSTRUCTION")
    assert post(client, "/volumes/lines", dict(version_id=version, work_item_id=item, planned_quantity=bad, labor_norm="1")).status_code == 400
    assert db.execute("SELECT count(*) FROM plan_lines").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM buildings").fetchone()[0] == 3


@pytest.mark.parametrize("fields", [dict(building_id="1.5"), dict(floor_code=""), dict(sort_order="0.1"), dict(building_id="999")])
def test_bad_floor_inputs_are_rejected(client, db, fields):
    assert add_floor(client, **fields).status_code == 400
    assert db.execute("SELECT count(*) FROM floors").fetchone()[0] == 0


def test_bad_event_date_negative_volume_and_scope_rejected(client, db, item):
    for changes in (dict(business_date="2026-02-30"), dict(quantity_delta="-1"), dict(floor_id="999"),
                    dict(quantity_delta="0"), dict(business_date="9999-01-01")):
        data = event_data(item)
        data.update(changes)
        assert post(client, "/facts", data).status_code == 400
    assert db.execute("SELECT count(*) FROM fact_events").fetchone()[0] == 0


@pytest.mark.parametrize("origin", [None, "null", "http://evil.test", "http://localhost:5100", ORIGIN+"/", "https://127.0.0.1:5100"])
def test_mutations_require_exact_origin(client, db, origin):
    response = post(client, "/buildings/floors", dict(action="add", building_id=1, floor_code="1", sort_order=1), origin=origin)
    assert response.status_code == 403
    assert db.execute("SELECT count(*) FROM floors").fetchone()[0] == 0


def test_csrf_token_cross_site_and_host_protection(client, db):
    fields = dict(action="add", building_id=1, floor_code="1", sort_order=1)
    assert post(client, "/buildings/floors", fields, token=False).status_code == 403
    for token in ("invalid", "кириллица"):
        assert post(client, "/buildings/floors", dict(fields, csrf_token=token), token=False).status_code == 403
    assert post(client, "/buildings/floors", fields, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/", base_url="http://evil.test:5100").status_code == 403
    assert db.execute("SELECT count(*) FROM floors").fetchone()[0] == 0


def test_html_is_escaped_and_no_remote_resources(client):
    assert add_floor(client, floor_code="<script>alert(1)</script>").status_code == 303
    page = get(client, "/buildings").get_data(as_text=True)
    assert "&lt;script&gt;" in page
    assert "<script>alert(1)</script>" not in page
    for url in re.findall(r'(?:src|href)="([^"]+)"', page):
        assert not url.startswith(("https://", "http://", "//"))


def test_unexpected_error_does_not_disclose_traceback(client, monkeypatch):
    from app.repositories.work_repository import WorkRepository
    def fail(self):
        raise RuntimeError("private internal traceback detail")
    monkeypatch.setattr(WorkRepository, "list_buildings", fail)
    response = get(client, "/buildings")
    assert response.status_code == 500
    assert b"private internal" not in response.data and b"Traceback" not in response.data


@pytest.mark.parametrize("cutoff", ["2026-01-31T18:00:00", "9999-12-31T23:59:59-23:59"])
def test_invalid_knowledge_timestamp_is_a_validation_error_not_server_error(client, cutoff):
    from urllib.parse import urlencode
    response = get(client, "/progress?" + urlencode({"knowledge_cutoff": cutoff}))
    assert response.status_code == 400
    assert "Дата отсечения знаний" in response.get_data(as_text=True)


def test_database_persists_after_factory_reopens_it(client, webapp, db, item, plan):
    post(client, "/facts", event_data(item))
    original_ids = [tuple(row) for row in db.execute("SELECT id,planned_quantity FROM plan_lines")]
    second = create_app(dict(DATABASE=webapp.config["DATABASE"], TESTING=True, LOCAL_ORIGIN=ORIGIN))
    page = get(second.test_client(), "/").get_data(as_text=True)
    assert "60,0%" in page
    reopened = connect(webapp.config["DATABASE"])
    try:
        assert [tuple(row) for row in reopened.execute("SELECT id,planned_quantity FROM plan_lines")] == original_ids
        assert reopened.execute("SELECT count(*) FROM work_catalog").fetchone()[0] == 15
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()


def test_shutdown_is_a_protected_post_and_signals_server(client, webapp):
    stopped = []
    webapp.extensions["stop_server"] = lambda: stopped.append(True)
    assert get(client, "/shutdown").status_code == 405
    assert post(client, "/shutdown", {}, token=False).status_code == 403
    assert not stopped
    response = post(client, "/shutdown", {})
    assert response.status_code == 200 and stopped == [True]
    assert "Данные сохранены" in response.get_data(as_text=True)
    assert b"/static/" not in response.data  # The stopped server cannot serve new assets.
