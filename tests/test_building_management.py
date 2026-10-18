from decimal import Decimal
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.calculation_engine.historical import HistoricalCalculator
from app.database import connect
from app.repositories.base import ValidationError
from app.repositories.project_repository import ProjectRepository
from app.repositories.work_repository import WorkRepository
from app.web import create_app


def contents(db, *tables):
    return {t: [tuple(r) for r in db.execute('SELECT * FROM "{}" ORDER BY id'.format(t))] for t in tables}


def test_delete_empty_building_preserves_other_buildings_and_audit(db, work):
    audit = contents(db, "audit_log")["audit_log"]
    assert work.building_deletion_info(2)["can_delete"]
    work.delete_building(2)
    assert [b["id"] for b in work.list_buildings()] == [1, 3]
    after = contents(db, "audit_log")["audit_log"]
    assert after[:len(audit)] == audit
    assert db.execute("SELECT 1 FROM audit_log WHERE entity_type='buildings' AND entity_id=2 AND operation='DELETE'").fetchone()
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_delete_empty_floors_requires_explicit_consent_and_preserves_other_floors(db, work):
    target = [work.add_floor(1, code, n) for n, code in enumerate(("-2", "Тех", "Кровля"))]
    kept = work.add_floor(2, "1", 1)
    before = contents(db, "buildings", "floors", "audit_log")
    with pytest.raises(ValidationError, match="Подтвердите"):
        work.delete_building(1)
    assert contents(db, *before) == before
    work.delete_building(1, delete_empty_floors=True)
    assert [f["id"] for f in work.list_floors()] == [kept]
    assert {r[0] for r in db.execute("SELECT entity_id FROM audit_log WHERE entity_type='floors' AND operation='DELETE'")} == set(target)


def test_unused_accounting_position_alone_blocks_building_deletion(db, work, item):
    info = work.building_deletion_info(1)
    assert not info["can_delete"]
    assert info["dependencies"] == [dict(table="work_items", label="Учётные позиции", count=1)]
    before = contents(db, "buildings", "floors", "work_items", "audit_log")
    with pytest.raises(ValidationError, match="архивирование"):
        work.delete_building(1, delete_empty_floors=True)
    assert contents(db, *before) == before


@pytest.mark.parametrize("parent, column, foreign_key", [
    ("buildings", "building_id", "REFERENCES buildings(id) ON DELETE CASCADE"),
    ("floors", "floor_id", "REFERENCES floors ON DELETE CASCADE"),
    ("buildings", "building_id", ""),
    ("floors", "floor_id", ""),
])
def test_future_dependencies_block_deletion_even_with_cascade_or_without_fk(db, work, parent, column, foreign_key):
    floor = work.add_floor(1, "Тех", 1)
    # Only a test fixture for future integration, not a new business table.
    db.execute('CREATE TABLE building_attachments(id INTEGER PRIMARY KEY, "{}" INTEGER {})'.format(column, foreign_key))
    db.execute('INSERT INTO building_attachments("{}") VALUES (?)'.format(column), (1 if parent == "buildings" else floor,))
    before = contents(db, "buildings", "floors", "building_attachments", "audit_log")
    with pytest.raises(ValidationError):
        work.delete_building(1, delete_empty_floors=True)
    assert contents(db, *before) == before
    assert work.building_deletion_info(1)["dependencies"][0]["label"] == "Связанные записи: building_attachments"
    # Dependencies of the first building must not block another one.
    work.delete_building(2)


def test_composite_floor_reference_and_quoted_identifiers_are_checked(db, work):
    floor = work.add_floor(1, "1", 1)
    db.execute('CREATE TABLE "Строки \'наряда\'" (id INTEGER PRIMARY KEY, level INTEGER, wing INTEGER, '
               'FOREIGN KEY(level,wing) REFERENCES floors(id,building_id) ON DELETE CASCADE)')
    db.execute('INSERT INTO "Строки \'наряда\'" VALUES (1,?,1)', (floor,))
    with pytest.raises(ValidationError):
        work.delete_building(1, delete_empty_floors=True)
    assert db.execute('SELECT COUNT(*) FROM "Строки \'наряда\'"').fetchone()[0] == 1


def test_floor_and_building_deletes_roll_back_together_on_failure(db, work):
    work.add_floor(1, "1", 1)
    work.add_floor(1, "2", 2)
    db.execute("CREATE TRIGGER simulate_delete_failure BEFORE DELETE ON buildings "
               "WHEN OLD.id=1 BEGIN SELECT RAISE(ABORT,'test failure'); END")
    before = contents(db, "buildings", "floors", "audit_log")
    with pytest.raises(ValidationError, match="test failure"):
        work.delete_building(1, delete_empty_floors=True)
    assert contents(db, *before) == before
    assert not db.in_transaction


def test_concurrent_dependency_is_committed_before_deletion_checks(db, work):
    work.add_floor(1, "1", 1)
    db.execute("CREATE TABLE building_attachments(id INTEGER PRIMARY KEY, building_id INTEGER REFERENCES buildings(id) ON DELETE CASCADE)")
    filename = db.execute("PRAGMA database_list").fetchone()["file"]
    inserted, delete_started, release = Event(), Event(), Event()

    def writer():
        connection = connect(filename)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO building_attachments(building_id) VALUES (1)")
            inserted.set()
            assert release.wait(5)
            connection.commit()
        finally:
            connection.close()

    def deleter():
        connection = connect(filename)
        try:
            delete_started.set()
            with pytest.raises(ValidationError, match="архивирование"):
                WorkRepository(connection).delete_building(1, delete_empty_floors=True)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        writing = executor.submit(writer)
        try:
            assert inserted.wait(5)
            deleting = executor.submit(deleter)
            assert delete_started.wait(5)
        finally:
            release.set()
        writing.result(timeout=5)
        deleting.result(timeout=5)
    assert db.execute("SELECT COUNT(*) FROM building_attachments").fetchone()[0] == 1
    assert len(work.list_floors(1)) == 1
    assert len(work.list_buildings()) == 3


def test_archive_and_restore_keep_ids_events_versions_and_exact_historical_progress(db, work, events, item, plan):
    events.add_fact(item, "600.001", "2026-01-10", event_key="physical", recorded_at="2026-01-10T12:00:00Z")
    events.add_confirmation(item, "450.001", "2026-01-10", event_key="accepted", document_ref="Акт 1",
                            recorded_at="2026-01-10T13:00:00Z")
    tables = ("floors", "work_items", "plan_versions", "plan_lines", "fact_events", "confirmation_events")
    before = contents(db, *tables)
    calc = HistoricalCalculator(db)
    report = calc.calculate_as_of("2026-01-10", "2026-01-10T14:00:00Z", plan)
    with pytest.raises(ValidationError):
        work.delete_building(1, delete_empty_floors=True)
    work.archive_building(1)
    assert calc.calculate_as_of("2026-01-10", "2026-01-10T14:00:00Z", plan) == report
    assert report.total.actual_progress == Decimal("0.600001")
    assert report.total.confirmed_progress == Decimal("0.450001")
    assert contents(db, *tables) == before
    assert 1 in [b["id"] for b in work.list_buildings()]
    assert 1 not in [b["id"] for b in work.list_buildings(include_archived=False)]
    assert work.list_floors(include_archived=False) == []
    assert work.available_work_items("CONSTRUCTION") == []
    with pytest.raises(ValidationError, match="архивирован"):
        work.add_work_item(1, work.list_floors()[0]["id"], 2)
    with pytest.raises(ValidationError, match="архивирован"):
        events.add_fact(item, "1", "2026-01-11", event_key="blocked")
    work.archive_building(1, False)
    assert [row["id"] for row in work.available_work_items("CONSTRUCTION")] == [item]
    assert contents(db, *tables) == before


def test_archived_empty_building_can_be_physically_deleted(work):
    work.archive_building(3)
    work.delete_building(3)
    assert [b["id"] for b in work.list_buildings()] == [1, 2]


@pytest.mark.parametrize("operation", ["info", "delete", "archive", "restore"])
def test_repository_refuses_foreign_building(db, work, operation):
    project = ProjectRepository(db).create_project("Другой объект", "OTHER", buildings=[dict(code="К1", name="Чужой")])
    other = WorkRepository(db, project).list_buildings()[0]["id"]
    before = contents(db, "buildings", "audit_log")
    with pytest.raises(ValidationError, match="текущему объекту"):
        if operation == "info":
            work.building_deletion_info(other)
        elif operation == "delete":
            work.delete_building(other, delete_empty_floors=True)
        else:
            work.archive_building(other, operation == "archive")
    assert contents(db, *before) == before


def test_archived_project_cannot_delete_or_change_building_status(db, work):
    ProjectRepository(db).set_archived(1, True)
    for operation in (lambda: work.delete_building(2), lambda: work.archive_building(2),
                      lambda: work.archive_building(2, False)):
        with pytest.raises(ValidationError, match="Объект архивирован"):
            operation()
    assert len(work.list_buildings()) == 3


def test_delete_and_archive_survive_database_reopening(db, work):
    filename = db.execute("PRAGMA database_list").fetchone()["file"]
    work.delete_building(2)
    work.archive_building(1)
    reopened = connect(filename)
    try:
        repo = WorkRepository(reopened)
        assert [(b["id"], b["status"]) for b in repo.list_buildings()] == [(1, "archived"), (3, "active")]
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        reopened.close()


ORIGIN = "http://127.0.0.1:5100"


@pytest.fixture
def client(db):
    app = create_app(dict(DATABASE=db.execute("PRAGMA database_list").fetchone()["file"],
                          TESTING=True, LOCAL_ORIGIN=ORIGIN, SECRET_KEY="building-test"))
    return app.test_client()


def get(client, path):
    return client.get(path, base_url=ORIGIN)


def post(client, path, data):
    get(client, "/")
    with client.session_transaction(base_url=ORIGIN) as state:
        values = dict(csrf_token=state["csrf_token"], workspace_id=state["project_id"], **data)
    return client.post(path, base_url=ORIGIN, headers={"Origin": ORIGIN}, data=values)


def delete_url(building_id, project_id=1):
    return "/projects/{}/buildings/{}/delete".format(project_id, building_id)


class FormOptions(HTMLParser):
    """Read only select options in the requested form, without a test dependency."""
    def __init__(self, html, action, select_name, method="post"):
        super().__init__()
        self.action, self.select_name, self.method = action, select_name, method
        self.in_form = self.in_select = False
        self.values = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.in_form = attrs.get("action", "") == self.action and attrs.get("method", "get").lower() == self.method
        elif tag == "select":
            self.in_select = self.in_form and attrs.get("name") == self.select_name
        elif tag == "option" and self.in_select:
            self.values.append(attrs.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "form": self.in_form = False
        if tag == "select": self.in_select = False


def test_delete_button_opens_safe_review_and_get_never_deletes(client, work):
    page = get(client, "/projects/1").get_data(as_text=True)
    assert delete_url(1) in page and "Удалить корпус" in page
    review = get(client, delete_url(1))
    assert review.status_code == 200
    assert 'name="confirm_delete"' in review.get_data(as_text=True)
    assert len(work.list_buildings()) == 3


def test_http_delete_requires_both_confirmations(client, db, work):
    work.add_floor(1, "Тех", 1)
    for data in ({}, {"confirm_delete": "1"}, {"delete_empty_floors": "1"}, {"confirm_delete": "yes", "delete_empty_floors": "1"}):
        assert post(client, delete_url(1), data).status_code == 400
        assert len(work.list_buildings()) == 3 and len(work.list_floors()) == 1
    assert post(client, delete_url(1), dict(confirm_delete="1", delete_empty_floors="1")).status_code == 303
    assert work.list_floors() == []
    assert db.execute("SELECT 1 FROM buildings WHERE id=1").fetchone() is None


def test_http_deletion_without_floors_and_repeat_submission(client, work):
    assert post(client, delete_url(2), dict(confirm_delete="1")).status_code == 303
    assert post(client, delete_url(2), dict(confirm_delete="1")).status_code == 400
    assert len(work.list_buildings()) == 2


def test_blocked_delete_offers_archive_and_restore_preserves_history(client, db, work, item, plan, events):
    events.add_fact(item, "600", "2026-01-10", event_key="fact")
    events.add_confirmation(item, "450", "2026-01-10", event_key="confirmation", document_ref="Акт сохранён")
    tables = ("floors", "work_items", "plan_versions", "plan_lines", "fact_events", "confirmation_events")
    before = contents(db, *tables)
    page = get(client, delete_url(1)).get_data(as_text=True)
    assert "Удаление невозможно" in page and "Архивировать корпус" in page
    assert 'name="confirm_delete"' not in page
    assert post(client, delete_url(1), dict(confirm_delete="1", delete_empty_floors="1")).status_code == 400
    assert post(client, "/projects/1/buildings", dict(action="archive", building_id="1")).status_code == 303
    assert "60,0%" in get(client, "/progress").get_data(as_text=True)
    assert "45,0%" in get(client, "/progress").get_data(as_text=True)
    assert "Акт сохранён" in get(client, "/confirmations").get_data(as_text=True)
    assert contents(db, *tables) == before
    assert post(client, "/projects/1/buildings", dict(action="restore", building_id="1")).status_code == 303
    assert contents(db, *tables) == before
    assert work.list_buildings()[0]["id"] == 1


def test_dependency_created_after_review_is_rechecked_on_submit(client, db, work):
    floor = work.add_floor(1, "1", 1)
    assert 'name="confirm_delete"' in get(client, delete_url(1)).get_data(as_text=True)
    work.add_work_item(1, floor, 1)
    before = contents(db, "buildings", "floors", "work_items", "audit_log")
    response = post(client, delete_url(1), dict(confirm_delete="1", delete_empty_floors="1"))
    assert response.status_code == 400
    assert "Архивировать корпус" in response.get_data(as_text=True)
    assert contents(db, *before) == before


@pytest.mark.parametrize("operation", ["review", "delete", "archive", "restore"])
def test_http_rejects_cross_project_record_ids_and_urls(client, db, operation):
    project = ProjectRepository(db).create_project("Другой объект", "OTHER", buildings=[dict(code="К1", name="Чужой")])
    other = WorkRepository(db, project).list_buildings()[0]["id"]
    before = contents(db, "buildings", "audit_log")
    if operation == "review":
        assert get(client, delete_url(other)).status_code == 400
        assert get(client, delete_url(other, project)).status_code == 403
    elif operation == "delete":
        assert post(client, delete_url(other), dict(confirm_delete="1")).status_code == 400
        assert post(client, delete_url(other, project), dict(confirm_delete="1")).status_code == 403
    else:
        assert post(client, "/projects/1/buildings", dict(action=operation, building_id=other)).status_code == 400
    assert contents(db, *before) == before


@pytest.mark.parametrize("guard", ["csrf", "origin", "workspace"])
def test_delete_request_security(client, work, guard):
    get(client, "/")
    with client.session_transaction(base_url=ORIGIN) as state:
        data = dict(csrf_token=state["csrf_token"], workspace_id="1", confirm_delete="1")
    if guard == "csrf": data["csrf_token"] = "wrong"
    if guard == "workspace": data["workspace_id"] = "2"
    response = client.post(delete_url(1), base_url=ORIGIN, headers={"Origin": "http://foreign.invalid" if guard == "origin" else ORIGIN}, data=data)
    assert response.status_code == 403
    assert len(work.list_buildings()) == 3


def test_archived_building_excluded_from_new_forms_but_available_in_report_filters(client, work, item, plan, events):
    floor = work.add_floor(2, "Другой этаж", 1)
    active_item = work.add_work_item(2, floor, 1)
    version = work.create_plan_version("CONSTRUCTION", base_version_id=plan, created_at="2026-01-02T00:00:00Z")
    work.add_plan_line(version, active_item, "1000", "0.8")
    work.approve_plan_version(version, approved_at="2026-01-02T01:00:00Z")
    events.add_fact(item, "600", "2026-01-10", event_key="archived")
    events.add_fact(active_item, "600", "2026-01-10", event_key="active")
    work.archive_building(1)
    for path, action in (("/buildings", "/buildings/floors"), ("/volumes", "/volumes/items"),
                         ("/facts", ""), ("/confirmations", "")):
        html = get(client, path).get_data(as_text=True)
        assert "1" not in FormOptions(html, action, "building_id").values
        assert "2" in FormOptions(html, action, "building_id").values
        assert str(work.list_floors(1)[0]["id"]) not in FormOptions(html, action, "floor_id").values
    for path in ("/volumes", "/progress", "/facts", "/confirmations"):
        html = get(client, path).get_data(as_text=True)
        assert "1" in FormOptions(html, "", "building_id", method="get").values
    response = post(client, "/volumes/items", dict(building_id="1", floor_id=work.list_floors(1)[0]["id"],
                                                   catalog_id="2", discipline="CONSTRUCTION"))
    assert response.status_code == 400
    work.archive_building(1, False)
    assert "1" in FormOptions(get(client, "/volumes").get_data(as_text=True), "/volumes/items", "building_id").values


@pytest.mark.parametrize("path", ["/facts", "/confirmations"])
def test_posting_old_event_form_for_archived_building_is_rejected(client, db, work, events, item, plan, path):
    events.add_fact(item, "600", "2026-01-10", event_key="existing")
    work.archive_building(1)
    before = contents(db, "fact_events", "confirmation_events", "audit_log")
    response = post(client, path, dict(discipline="CONSTRUCTION", work_item_id=item, version_id=plan,
                                      business_date="2026-01-11", quantity_delta="10", event_key="stale-form",
                                      document_ref="Не сохранится"))
    assert response.status_code == 400
    assert contents(db, *before) == before


def test_archived_project_delete_page_is_read_only_and_post_is_rejected(client, db, work):
    ProjectRepository(db).set_archived(1, True)
    html = get(client, delete_url(1)).get_data(as_text=True)
    assert "Объект архивирован" in html and 'name="confirm_delete"' not in html
    assert post(client, delete_url(1), dict(confirm_delete="1")).status_code == 400
    assert len(work.list_buildings()) == 3
