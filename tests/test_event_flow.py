from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, localcontext
from threading import Barrier
import re
from urllib.parse import urlencode

import pytest

from app.calculation_engine.historical import HistoricalCalculator
from app.database import connect
from app.repositories.base import ValidationError
from app.repositories.event_repository import EventRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.work_repository import WorkRepository
from app.services.progress_service import ProgressService
from app.web import create_app

ORIGIN = "http://127.0.0.1:5100"


def queue(db, version, *, project=1, report_date="2026-01-31", cutoff="2026-02-01T23:59:59Z", confirmation=True, pending=True, **filters):
    service = ProgressService(db, project)
    report = service.plan_view(report_date, cutoff, version)
    return service.event_rows(report, confirmation=confirmation, pending_only=confirmation and pending, filters=filters)


def fact(events, item, amount="600", key="fact", date="2026-01-10", recorded="2026-01-10T12:00:00Z", **kwargs):
    return events.add_fact(item, amount, date, event_key=key, recorded_at=recorded, **kwargs)


def confirm(events, item, amount, key="confirm", date="2026-01-10", recorded="2026-01-10T13:00:00Z", **kwargs):
    return events.add_confirmation(item, amount, date, event_key=key, recorded_at=recorded, document_ref="Акт " + key, **kwargs)


def test_approved_work_available_for_fact_but_not_confirmation_without_fact(db, item, plan):
    rows = queue(db, plan, confirmation=False)
    assert [r["id"] for r in rows] == [item]
    assert (rows[0]["planned_quantity"], rows[0]["actual"], rows[0]["confirmed"], rows[0]["remaining"]) == (Decimal("1000"), 0, 0, Decimal("1000"))
    assert queue(db, plan) == queue(db, plan, pending=False) == []


def test_draft_does_not_supply_fact_work(db, work, item):
    v = work.create_plan_version("CONSTRUCTION")
    work.add_plan_line(v, item, "1000", "0.8")
    assert queue(db, v, confirmation=False) == []
    with pytest.raises(ValidationError):
        EventRepository(db).add_fact(item, "1", "2026-01-10", event_key="draft", require_current_plan=True)


def test_delta_events_partial_full_and_additional_fact_flow(db, events, item, plan):
    fact(events, item, "200", "f1")
    fact(events, item, "400", "f2", date="2026-01-11", recorded="2026-01-11T12:00:00Z")
    assert queue(db, plan)[0]["pending"] == Decimal("600")
    for amount, total, pending in [("450", "450", "150"), ("100", "550", "50"), ("50", "600", "0")]:
        confirm(events, item, amount, "c"+amount, date="2026-01-12", recorded="2026-01-12T12:00:00Z")
        rows = queue(db, plan, pending=False)
        assert rows[0]["confirmed"] == Decimal(total)
        assert rows[0]["pending"] == Decimal(pending)
        assert bool(queue(db, plan)) == (Decimal(pending) > 0)
    fact(events, item, "100", "f3", date="2026-01-13", recorded="2026-01-13T12:00:00Z")
    assert queue(db, plan)[0]["pending"] == Decimal("100")
    assert [r[0] for r in db.execute("SELECT quantity_delta FROM fact_events ORDER BY id")] == ["200", "400", "100"]
    assert db.execute("SELECT count(*) FROM work_items").fetchone()[0] == 1


def test_historical_and_knowledge_frontiers_preserve_pending(db, events, item, plan):
    fact(events, item, recorded="2026-01-11T12:00:00Z")
    confirm(events, item, "450", recorded="2026-01-11T13:00:00Z")
    confirm(events, item, "100", "later", date="2026-01-11", recorded="2026-01-12T12:00:00Z")
    assert queue(db, plan, report_date="2026-01-10")[0]["pending"] == Decimal("150")
    assert queue(db, plan, report_date="2026-01-11")[0]["pending"] == Decimal("50")
    assert queue(db, plan, report_date="2026-01-10", cutoff="2026-01-10T23:59:59Z") == []
    assert queue(db, plan, report_date="2026-01-11", cutoff="2026-01-11T23:59:59Z")[0]["pending"] == Decimal("150")


def test_corrections_affect_pending_without_replacing_events(db, events, item, plan):
    f = fact(events, item)
    c = confirm(events, item, "450")
    confirm(events, item, "-20", "cf", recorded="2026-01-12T12:00:00Z", correction_of_id=c)
    fact(events, item, "-30", "ff", recorded="2026-01-12T13:00:00Z", correction_of_id=f)
    row = queue(db, plan)[0]
    assert (row["actual"], row["confirmed"], row["pending"]) == (Decimal("570"), Decimal("430"), Decimal("140"))
    assert [r[0] for r in db.execute("SELECT quantity_delta FROM fact_events ORDER BY id")] == ["600", "-30"]


def test_all_works_keeps_zero_net_fact_history(db, events, item, plan):
    f = fact(events, item, "1")
    fact(events, item, "-1", "zero", recorded="2026-01-11T12:00:00Z", correction_of_id=f)
    assert queue(db, plan) == []
    assert queue(db, plan, pending=False)[0]["actual"] == 0


def test_pending_uses_full_actual_even_above_plan(db, events, item, plan):
    fact(events, item, "1150")
    row = queue(db, plan)[0]
    assert row["pending"] == Decimal("1150")
    assert row["remaining"] == 0
    assert row["result"].actual_progress == 1


def test_queue_keeps_exact_decimal_with_small_ambient_precision(db, events, work, item):
    v = work.create_plan_version("CONSTRUCTION", created_at="2026-01-01T00:00:00Z")
    work.add_plan_line(v, item, "999999999999999999.123456789", "1")
    work.approve_plan_version(v, approved_at="2026-01-01T01:00:00Z")
    with localcontext() as ctx:
        ctx.prec = 4
        fact(events, item, "123456789012345678.123456789")
        confirm(events, item, "0.1")
        row = queue(db, v)[0]
        assert row["pending"] == Decimal("123456789012345678.023456789")
        assert row["actual"] == Decimal("123456789012345678.123456789")


def test_selected_plan_changes_plan_columns_not_fact_or_pending(db, events, work, item, plan):
    fact(events, item)
    v2 = work.create_plan_version("CONSTRUCTION", base_version_id=plan, created_at="2026-01-20T00:00:00Z")
    work.update_plan_line(v2, item, "1200", "0.8")
    work.approve_plan_version(v2, approved_at="2026-01-20T01:00:00Z")
    old, new = queue(db, plan)[0], queue(db, v2)[0]
    assert (old["remaining"], new["remaining"]) == (Decimal("400"), Decimal("600"))
    assert old["pending"] == new["pending"] == Decimal("600")
    assert (old["result"].actual_progress, new["result"].actual_progress) == (Decimal("0.6"), Decimal("0.5"))


def test_queues_are_project_scoped(db, events, item, plan):
    fact(events, item)
    p = ProjectRepository(db).create_project("Школа", "SCH", buildings=[dict(code="A", name="Учебный")])
    w = WorkRepository(db, p)
    b = w.list_buildings()[0]["id"]
    floor = w.add_floor(b, "3", 3)
    other = w.add_work_item(b, floor, 1)
    v = w.create_plan_version("CONSTRUCTION", created_at="2026-01-01T00:00:00Z")
    w.add_plan_line(v, other, "1000", "1")
    w.approve_plan_version(v, approved_at="2026-01-01T01:00:00Z")
    other_events = EventRepository(db, p)
    fact(other_events, other, "200", "school")
    confirm(other_events, other, "100", "school-c")
    assert [(r["id"],r["pending"]) for r in queue(db, plan)] == [(item,Decimal("600"))]
    assert [(r["id"],r["pending"]) for r in queue(db, v, project=p)] == [(other,Decimal("100"))]
    assert queue(db, v) == []


def test_confirmation_max_is_business_date_specific(db, events, item, plan):
    fact(events, item, "200")
    fact(events, item, "400", "future-fact", date="2026-01-20", recorded="2026-01-20T12:00:00Z")
    with pytest.raises(ValidationError, match="confirmation exceeds actual"):
        confirm(events, item, "450", recorded="2026-01-21T12:00:00Z")
    assert db.execute("SELECT count(*) FROM confirmation_events").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["fact", "confirmation"])
@pytest.mark.parametrize("quantity", ["0", "-1"])
def test_ordinary_nonpositive_events_are_rejected(db, events, item, kind, quantity):
    if kind == "confirmation": fact(events, item)
    with pytest.raises(ValidationError):
        fact(events, item, quantity) if kind == "fact" else confirm(events, item, quantity)


def test_two_concurrent_confirmations_cannot_consume_same_remainder(db, events, item, plan):
    fact(events, item)
    path = db.execute("PRAGMA database_list").fetchone()["file"]
    barrier = Barrier(2)
    def accept(index):
        connection = connect(path)
        try:
            repo = EventRepository(connection)
            assert repo.cumulative_quantities("fact", "2026-01-10", "2026-01-11T23:00:00Z")[item] == Decimal("600")
            barrier.wait(timeout=10)
            try:
                confirm(repo,item,"400",key="race-"+str(index),recorded="2026-01-11T12:00:00Z",
                        require_current_plan=True,plan_version_id=plan)
                return "accepted"
            except ValidationError:
                return "rejected"
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(accept, [1,2])) == ["accepted", "rejected"]
    assert db.execute("SELECT count(*) FROM confirmation_events").fetchone()[0] == 1
    assert queue(db, plan)[0]["pending"] == Decimal("200")


def test_events_record_source_without_requiring_an_order(db, events, item, plan):
    fact(events,item)
    confirm(events,item,"450")
    assert db.execute("SELECT source_type FROM fact_events").fetchone()[0] == "manual"
    assert db.execute("SELECT source_type FROM confirmation_events").fetchone()[0] == "manual"
    assert "NEW.source_type='manual'" in db.execute("SELECT details FROM audit_log WHERE entity_type='fact_events' ORDER BY id DESC").fetchone()[0]


@pytest.fixture
def client(db):
    return create_app(dict(DATABASE=db.execute("PRAGMA database_list").fetchone()["file"],
                           LOCAL_ORIGIN=ORIGIN, TESTING=True, SECRET_KEY="flow-tests")).test_client()


def get(client, path):
    return client.get(path, base_url=ORIGIN)


def post(client, path, data):
    page = get(client,"/").get_data(as_text=True)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"',page).group(1)
    return client.post(path,base_url=ORIGIN,headers={"Origin":ORIGIN},data=dict(data,csrf_token=csrf))


def data(item, amount, key):
    return dict(discipline="CONSTRUCTION",building_id="1",floor_id="1",work_item_id=item,
                quantity_delta=amount,event_key=key,business_date="2026-01-10",document_ref="Акт №1")


def table_ids(response):
    return re.findall(r'data-queue-item="([0-9]+)"',response.get_data(as_text=True))


def test_confirmation_tabs_automatically_follow_fact_and_confirmation(client, db, item, plan):
    assert table_ids(get(client,"/facts")) == [str(item)]
    assert table_ids(get(client,"/confirmations")) == []
    assert post(client,"/facts",data(item,"600","f")).status_code == 303
    assert table_ids(get(client,"/confirmations")) == [str(item)]
    assert post(client,"/confirmations",data(item,"450","c1")).status_code == 303
    assert "150" in get(client,"/confirmations").get_data(as_text=True)
    assert post(client,"/confirmations",data(item,"150","c2")).status_code == 303
    assert table_ids(get(client,"/confirmations")) == []
    assert table_ids(get(client,"/confirmations?tab=all")) == [str(item)]
    assert post(client,"/facts",data(item,"100","f2")).status_code == 303
    assert table_ids(get(client,"/confirmations")) == [str(item)]


def test_confirmation_form_displays_remaining_but_does_not_prefill_quantity(client, events, item, plan):
    fact(events,item)
    confirm(events,item,"450")
    page=get(client,f"/confirmations?work_item_id={item}").get_data(as_text=True)
    assert "150 м²" in page and 'data-pending-hint' in page
    field=re.search(r'<input[^>]+name="quantity_delta"[^>]*>',page).group()
    assert 'value=' not in field
    assert re.search(r'<option value="'+str(item)+r'"[^>]+selected',page)


def test_duplicate_posts_and_stale_remainder_do_not_write_twice(client, db, item, plan):
    assert post(client,"/facts",data(item,"600","f")).status_code==303
    values=data(item,"450","c")
    assert post(client,"/confirmations",values).status_code==303
    assert post(client,"/confirmations",values).status_code==400
    assert post(client,"/confirmations",data(item,"151","too-much")).status_code==400
    assert db.execute("SELECT count(*) FROM confirmation_events").fetchone()[0]==1
    assert db.execute("SELECT quantity_delta FROM confirmation_events").fetchone()[0]=="450"


def test_stale_plan_form_is_rechecked_inside_write(client, work, events, item, plan, db):
    v=work.create_plan_version("CONSTRUCTION",base_version_id=plan)
    work.approve_plan_version(v)
    values=data(item,"1","stale")
    values["version_id"]=plan
    assert post(client,"/facts",values).status_code==400
    assert db.execute("SELECT count(*) FROM fact_events").fetchone()[0]==0


def test_historical_confirmation_screen_hides_future_events_and_is_read_only(client, events, item, plan):
    fact(events,item)
    confirm(events,item,"450")
    confirm(events,item,"100","later-document",date="2026-01-11",recorded="2026-01-11T12:00:00Z")
    page=get(client,"/confirmations?report_date=2026-01-10&knowledge_cutoff=2026-01-31T23:59:59Z").get_data(as_text=True)
    assert "150" in page and "later-document" not in page
    assert 'class="event-form"' not in page
    assert "Просмотр среза" in page


@pytest.mark.parametrize("filters,expected", [({"building_id":"1"},True),({"floor_id":"1"},True),
    ({"element":"Стены"},True),({"work_type":"штукатурка"},True),({"element":"Пол"},False),({"work_type":"окраска"},False)])
def test_queue_filters_are_server_side(client, events, item, plan, filters, expected):
    fact(events,item)
    response=get(client,"/confirmations?"+urlencode(filters))
    assert response.status_code==200
    assert table_ids(response)==([str(item)] if expected else [])


def test_forged_event_source_does_not_override_manual_web_origin(client, db, item, plan):
    values=data(item,"600","f");values["source_type"]="import"
    assert post(client,"/facts",values).status_code==303
    assert db.execute("SELECT source_type FROM fact_events").fetchone()[0]=="manual"


def test_bad_tab_and_unknown_selected_work_are_validation_errors(client):
    assert get(client,"/confirmations?tab=bad").status_code==400
    assert get(client,"/confirmations?work_item_id=999").status_code==400
