import re

import pytest
from werkzeug.datastructures import MultiDict

from app.database import connect
from app.time_utils import instant
from app.repositories.event_repository import EventRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.work_repository import WorkRepository
from app.web import create_app

ORIGIN = "http://127.0.0.1:5100"


@pytest.fixture
def client(db):
    app = create_app(dict(DATABASE=db.execute("PRAGMA database_list").fetchone()["file"],
                          TESTING=True, LOCAL_ORIGIN=ORIGIN, SECRET_KEY="projects-test"))
    return app.test_client()


def get(client, path):
    return client.get(path, base_url=ORIGIN)


def post(client, path, data):
    get(client, "/")
    with client.session_transaction(base_url=ORIGIN) as state:
        token, project = state["csrf_token"], state["project_id"]
    values = MultiDict(data)
    values.setdefault("csrf_token", token)
    values.setdefault("workspace_id", str(project))
    return client.post(path, base_url=ORIGIN, headers={"Origin": ORIGIN}, data=values)


def make_school(db):
    p = ProjectRepository(db).create_project("Школа", "ШК", buildings=[dict(code="К1", name="Учебный"), dict(code="К2", name="Спортивный")])
    repo = WorkRepository(db, p)
    b = repo.list_buildings()[0]["id"]
    f = repo.add_floor(b, "1", 1)
    i = repo.add_work_item(b, f, 1)
    v = repo.create_plan_version("CONSTRUCTION", created_at="2026-01-01T00:00:00Z")
    repo.add_plan_line(v, i, "1000", "0.8")
    repo.approve_plan_version(v, approved_at="2026-01-01T01:00:00Z")
    return p, b, f, i, v


@pytest.mark.parametrize("path", ["/projects", "/projects/new", "/projects/1"])
def test_project_pages_open_in_russian(client, path):
    response = get(client, path)
    assert response.status_code == 200
    assert '<html lang="ru">' in response.get_data(as_text=True)


def test_create_school_with_two_buildings_selects_it_and_creates_no_floors(client, db):
    data = MultiDict([("name", "Школа"), ("code", "ШК"), ("description", "Новая школа"), ("building_count", "2"),
                      ("building_code", "УЧ"), ("building_name", "Учебный"), ("building_code", "СП"), ("building_name", "Спортивный")])
    response = post(client, "/projects/new", data)
    assert response.status_code == 303
    with client.session_transaction(base_url=ORIGIN) as state:
        project = state["project_id"]
    assert project != 1
    assert [row["name"] for row in WorkRepository(db, project).list_buildings()] == ["Учебный", "Спортивный"]
    assert WorkRepository(db, project).list_floors() == []
    assert get(client, response.location).status_code == 200


@pytest.mark.parametrize("kind", ["duplicate", "empty", "count", "negative"])
def test_invalid_creation_is_atomic_over_http(client, db, kind):
    data = MultiDict([("name", "Школа"), ("code", "ШК"), ("building_count", "2"),
                      ("building_code", "К1"), ("building_name", "Первый"), ("building_code", "К1" if kind=="duplicate" else "К2"),
                      ("building_name", "" if kind=="empty" else "Второй")])
    if kind == "count": data["building_count"] = "3"
    if kind == "negative": data["building_count"] = "-1"
    assert post(client, "/projects/new", data).status_code == 400
    assert db.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM buildings").fetchone()[0] == 3


def test_switching_changes_dashboard_versions_items_events_and_historical_report(client, db, work, item, plan):
    EventRepository(db).add_fact(item, "600", "2026-01-10", event_key="default")
    p, b, f, i, v = make_school(db)
    EventRepository(db, p).add_fact(i, "200", "2026-01-10", event_key="school")
    EventRepository(db, p).add_confirmation(i, "100", "2026-01-10", event_key="school-c", document_ref="ШК-АКТ")
    assert "60,0%" in get(client, "/").get_data(as_text=True)
    assert post(client, "/projects/select", dict(project_id=p)).status_code == 303
    dashboard = get(client, "/").get_data(as_text=True)
    assert "20,0%" in dashboard and "10,0%" in dashboard and "60,0%" not in dashboard
    assert [row["name"] for row in get(client, "/api/buildings").json] == ["Учебный", "Спортивный"]
    assert "20,0%" in get(client, "/progress").get_data(as_text=True)
    assert "Учебный" in get(client, "/volumes").get_data(as_text=True)
    assert "ШК-АКТ" in get(client, "/confirmations").get_data(as_text=True)
    assert get(client, "/volumes?version_id="+str(plan)).status_code == 400
    assert post(client, "/projects/select", dict(project_id=1)).status_code == 303
    assert "60,0%" in get(client, "/").get_data(as_text=True)
    assert "ШК-АКТ" not in get(client, "/confirmations").get_data(as_text=True)


def test_end_to_end_new_workspace_floor_plan_fact_confirmation_restart(client, db):
    p, b, f, i, v = make_school(db)
    post(client, "/projects/select", dict(project_id=p))
    assert post(client, "/buildings/floors", dict(action="add", building_id=b, floor_code="Тех", sort_order=100)).status_code == 303
    assert post(client, "/volumes/plans", dict(discipline="CONSTRUCTION", base_version_id=v)).status_code == 303
    draft = WorkRepository(db,p).list_plan_versions("CONSTRUCTION")[0]["id"]
    assert post(client, "/volumes/lines", dict(version_id=draft, work_item_id=i, planned_quantity="1200", labor_norm="0,8")).status_code == 303
    assert post(client, f"/volumes/plans/{draft}/approve", {}).status_code == 303
    data = dict(discipline="CONSTRUCTION", building_id=b, floor_id=f, work_item_id=i, quantity_delta="600", business_date="2026-01-10", event_key="f")
    assert post(client, "/facts", data).status_code == 303
    data.update(quantity_delta="450", event_key="c", document_ref="Акт ШК")
    assert post(client, "/confirmations", data).status_code == 303
    assert "50,0%" in get(client, "/").get_data(as_text=True)
    reopened = connect(db.execute("PRAGMA database_list").fetchone()["file"])
    try:
        assert reopened.execute("SELECT quantity_delta FROM fact_events").fetchone()[0] == "600"
        assert WorkRepository(reopened,p).get_plan_version(draft)["status"] == "APPROVED"
    finally:
        reopened.close()


@pytest.mark.parametrize("target", ["floor_add", "floor_order", "floor_delete", "item", "clone", "approve", "line", "fact", "confirmation", "project", "building"])
def test_cross_workspace_post_is_rejected_without_writes(client, db, target):
    p,b,f,i,v = make_school(db)
    endpoints = {
        "floor_add": ("/buildings/floors", dict(action="add", building_id=b, floor_code="2", sort_order=2)),
        "floor_order": ("/buildings/floors", dict(action="reorder", floor_id=f, sort_order=99)),
        "floor_delete": ("/buildings/floors", dict(action="delete", floor_id=f)),
        "item": ("/volumes/items", dict(building_id=b, floor_id=f, catalog_id=2)),
        "clone": ("/volumes/plans", dict(base_version_id=v)),
        "approve": (f"/volumes/plans/{v}/approve", {}),
        "line": ("/volumes/lines", dict(version_id=v, work_item_id=i, planned_quantity="1", labor_norm="1")),
        "fact": ("/facts", dict(work_item_id=i, building_id=b, floor_id=f, quantity_delta="1", business_date="2026-01-10", event_key="x")),
        "confirmation": ("/confirmations", dict(work_item_id=i, building_id=b, floor_id=f, quantity_delta="1", business_date="2026-01-10", event_key="x", document_ref="x")),
        "project": (f"/projects/{p}", dict(action="edit", name="Взлом", code="X")),
        "building": ("/projects/1/buildings", dict(action="edit", building_id=b, name="Взлом", code="X", sort_order=1))}
    audit = db.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    response = post(client, *endpoints[target])
    assert response.status_code in (400,403)
    assert db.execute("SELECT count(*) FROM audit_log").fetchone()[0] == audit


def test_foreign_direct_links_and_stale_workspace_forms_are_rejected(client, db):
    p,b,f,i,v = make_school(db)
    for path in [f"/projects/{p}", f"/volumes?version_id={v}", f"/progress?version_id={v}", f"/facts?building_id={b}", f"/confirmations?floor_id={f}"]:
        assert get(client, path).status_code in (400,403)
    post(client, "/projects/select", dict(project_id=p))
    assert post(client, "/volumes/plans", dict(workspace_id="1")).status_code == 403


def test_project_edit_archive_restore_and_building_edit(client, db):
    p,b,f,i,v = make_school(db)
    post(client,"/projects/select",dict(project_id=p))
    assert post(client,f"/projects/{p}",dict(action="edit",name="Школа №5",code="ШК5",description="Тест")).status_code==303
    assert post(client,f"/projects/{p}/buildings",dict(action="edit",building_id=b,code="УЧ",name="Учебный корпус",sort_order=-1)).status_code==303
    assert post(client,f"/projects/{p}",dict(action="archive")).status_code==303
    assert get(client,"/").status_code==200
    assert post(client,"/volumes/plans",{}).status_code==400
    assert post(client,f"/projects/{p}",dict(action="restore")).status_code==303
    assert WorkRepository(db,p).list_work_items()[0]["building_id"]==b


def test_draft_works_hidden_then_approved_work_appears_automatically(client, db, work, item):
    version=work.create_plan_version("CONSTRUCTION")
    work.add_plan_line(version,item,"1000","0.8")
    assert 'data-work-item' not in get(client,"/facts").get_data(as_text=True)
    data=dict(work_item_id=item,building_id=1,floor_id=1,quantity_delta="1",business_date="2026-01-10",event_key="draft")
    assert post(client,"/facts",data).status_code==400
    assert post(client,f"/volumes/plans/{version}/approve",{}).status_code==303
    page=get(client,"/facts").get_data(as_text=True)
    assert re.search(r'<option value="'+str(item)+r'" data-building="1" data-floor="1"',page)
    assert post(client,"/facts",data).status_code==303


def test_removed_work_stays_in_journal_and_can_be_corrected(client, db, work, item, plan):
    EventRepository(db).add_fact(item,"600","2026-01-10",event_key="original")
    second=work.add_work_item(1,1,2)
    draft=work.create_plan_version("CONSTRUCTION",base_version_id=plan)
    work.add_plan_line(draft,second,"100","1")
    assert post(client,"/volumes/lines/remove",dict(version_id=draft,work_item_id=item)).status_code==303
    work.approve_plan_version(draft)
    page=get(client,"/facts").get_data(as_text=True)
    options=re.search(r'<select[^>]+data-work-item[^>]*>(.*?)</select>',page,re.S).group(1)
    assert f'value="{item}"' not in options
    assert "600" in page
    assert get(client,"/facts?correction=1").status_code==200
    event=EventRepository(db).list_events("fact")[0]["id"]
    data=dict(work_item_id=item,building_id=1,floor_id=1,quantity_delta="-20",business_date=instant()[:10],event_key="fix",correction_of_id=event)
    assert post(client,"/facts",data).status_code==303
    assert len(EventRepository(db).list_events("fact"))==2
