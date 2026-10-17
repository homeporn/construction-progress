from decimal import Decimal
import shutil

from app.calculation_engine.historical import HistoricalCalculator
from app.calculation_engine.models import Status
from app.services.progress_service import ProgressService
from app.time_utils import instant
from app.web import create_app


def test_draft_preview_is_explicit_and_never_changes_active_baseline(db, work, item, plan, events):
    events.add_fact(item, "600", "2026-01-10", event_key="f", recorded_at="2026-01-10T12:00:00Z")
    draft = work.create_plan_version("CONSTRUCTION", base_version_id=plan)
    work.update_plan_line(draft, item, "1200", "0.8")
    calculator = HistoricalCalculator(db)
    assert calculator.calculate_as_of(instant()[:10], instant(), draft).status == Status.INVALID
    assert calculator.preview_draft_as_of(instant()[:10], instant(), draft).total.actual_progress == Decimal("0.5")
    smr = ProgressService(db).dashboard(instant()[:10], instant())["sections"][0]
    assert smr["version"]["id"] == plan
    assert smr["total"].actual_progress == Decimal("0.6")
    assert smr["total"].remaining_labor == Decimal("320")
    assert smr["report"].items[0].remaining_quantity == Decimal("400")


def test_independent_disciplines_and_labor_weighted_building_groups(db, work, item, plan, events):
    floor = work.add_floor(1, "Тех", 100)
    design_item = work.add_work_item(1, floor, 2, "DESIGN")
    design = work.create_plan_version("DESIGN", created_at="2026-01-01T00:00:00Z")
    work.add_plan_line(design, design_item, "200", "1")
    work.approve_plan_version(design, approved_at="2026-01-01T01:00:00Z")
    events.add_fact(item, "500", "2026-01-10", event_key="s", recorded_at="2026-01-10T12:00:00Z")
    events.add_fact(design_item, "200", "2026-01-10", event_key="d", recorded_at="2026-01-10T12:00:00Z")
    sections = ProgressService(db).dashboard("2026-01-31", "2026-01-31T12:00:00Z")["sections"]
    assert [section["total"].actual_progress for section in sections] == [Decimal("0.5"), Decimal("1")]
    assert sections[0]["buildings"][2].status == Status.NO_DATA


def test_upgrade_preserves_existing_stage1_data_and_adds_comment(tmp_path, monkeypatch):
    import app.database as database
    from app.config import PROJECT_ROOT
    from app.repositories.work_repository import WorkRepository
    from app.repositories.event_repository import EventRepository
    old_root = tmp_path / "stage1"
    (old_root / "migrations").mkdir(parents=True)
    for filename in ("001_initial.sql", "002_replace_guards.sql"):
        shutil.copyfile(PROJECT_ROOT / "migrations" / filename, old_root / "migrations" / filename)
    path = tmp_path / "existing-stage1.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(database, "PROJECT_ROOT", old_root)
        db = database.initialize(path)
        # Populate the historical schema directly: today's repositories require
        # object ownership, which intentionally does not exist in stage 1.
        floor = db.execute("INSERT INTO floors(building_id,floor_code,sort_order) VALUES (1,'1',1)").lastrowid
        item = db.execute("INSERT INTO work_items(building_id,floor_id,catalog_id,discipline) VALUES (1,?,1,'CONSTRUCTION')", (floor,)).lastrowid
        version = db.execute("INSERT INTO plan_versions(discipline,version_no,created_at) VALUES ('CONSTRUCTION',1,'2026-01-01T00:00:00.000000Z')").lastrowid
        db.execute("INSERT INTO plan_lines(plan_version_id,work_item_id,planned_quantity,labor_norm,unit) VALUES (?,?,'1000','0.8','м²')", (version,item))
        db.execute("UPDATE plan_versions SET status='APPROVED',approved_at='2026-01-01T01:00:00.000000Z' WHERE id=?", (version,))
        db.execute("INSERT INTO fact_events(work_item_id,quantity_delta,work_date,recorded_at,event_key,unit) VALUES (?,'600','2026-01-10','2026-01-10T12:00:00.000000Z','legacy-f','м²')", (item,))
        db.execute("INSERT INTO confirmation_events(work_item_id,quantity_delta,confirmed_date,recorded_at,document_ref,event_key,unit) "
                   "VALUES (?,'450','2026-01-10','2026-01-11T12:00:00.000000Z','old-document','legacy-c','м²')", (item,))
        before = [tuple(r) for r in db.execute("SELECT * FROM plan_lines")]
        db.close()
    create_app({"DATABASE": str(path), "TESTING": True})
    db = database.connect(path)
    try:
        assert [tuple(r) for r in db.execute("SELECT * FROM plan_lines")] == before
        assert db.execute("SELECT comment FROM confirmation_events").fetchone()[0] == ""
        assert db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == len(list((PROJECT_ROOT / 'migrations').glob('*.sql')))
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert HistoricalCalculator(db).calculate_as_of("2026-01-31", "2026-01-31T12:00:00Z", version).total.confirmed_progress == Decimal("0.45")
        EventRepository(db).add_confirmation(item, "1", "2026-01-12", event_key="new-c", document_ref="new-doc",
                                            comment="Комментарий нового интерфейса", recorded_at="2026-01-12T12:00:00Z")
        assert "Комментарий нового интерфейса" in db.execute(
            "SELECT details FROM audit_log WHERE entity_type='confirmation_events' ORDER BY id DESC").fetchone()[0]
    finally:
        db.close()


def test_server_binds_only_loopback_restores_signals_and_closes(tmp_path, monkeypatch):
    import signal
    from app.web import server as module
    calls = []
    class FakeHTTPServer:
        server_port = 54321
        def handle_request(self):
            local.stop_event.set()
        def server_close(self):
            calls.append("closed")
    def make_server(host, port, app, threaded):
        calls.append((host, port, app.debug, threaded))
        return FakeHTTPServer()
    monkeypatch.setattr(module, "make_server", make_server)
    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigterm = signal.getsignal(signal.SIGTERM)
    old_sighup = signal.getsignal(signal.SIGHUP) if hasattr(signal, "SIGHUP") else None
    local = module.LocalServer(tmp_path / "lifetime.sqlite3")
    local.serve(open_browser=False)
    assert calls == [("127.0.0.1", 0, False, False), "closed"]
    assert local.origin == "http://127.0.0.1:54321"
    assert local.app.config["SESSION_COOKIE_NAME"] == "construction_progress_54321"
    assert signal.getsignal(signal.SIGINT) is old_sigint
    assert signal.getsignal(signal.SIGTERM) is old_sigterm
    if hasattr(signal, "SIGHUP"):
        assert signal.getsignal(signal.SIGHUP) is old_sighup


def test_server_opens_browser_at_bound_local_address(tmp_path, monkeypatch):
    from app.web import server as module
    opened = []
    class FakeHTTPServer:
        server_port = 54322
        def handle_request(self):
            local.stop_event.set()
        def server_close(self):
            pass
    class ImmediateTimer:
        def __init__(self, interval, callback):
            self.callback = callback
        def start(self):
            self.callback()
        def cancel(self):
            pass
    monkeypatch.setattr(module, "make_server", lambda *a, **kw: FakeHTTPServer())
    monkeypatch.setattr(module.threading, "Timer", ImmediateTimer)
    monkeypatch.setattr(module.webbrowser, "open", lambda url: opened.append(url) or True)
    local = module.LocalServer(tmp_path / "browser.sqlite3")
    local.serve()
    assert opened == ["http://127.0.0.1:54322"]
