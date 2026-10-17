from decimal import Decimal
import sqlite3

import pytest

from app.calculation_engine.aggregation import aggregate, group_by
from app.calculation_engine.historical import HistoricalCalculator
from app.calculation_engine.models import Status
from app.calculation_engine.progress import calculate_item
from app.repositories.base import ValidationError
from app.repositories.event_repository import EventRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.work_repository import WorkRepository
from app.services.progress_service import ProgressService


@pytest.fixture
def school(db):
    project = ProjectRepository(db).create_project("Школа", "ШК", buildings=[
        dict(code="К1", name="Учебный"), dict(code="К2", name="Спортивный")])
    work = WorkRepository(db, project)
    building = work.list_buildings()[0]["id"]
    floor = work.add_floor(building, "-1", -1)
    item = work.add_work_item(building, floor, 1)
    version = work.create_plan_version("CONSTRUCTION", created_at="2026-01-01T00:00:00Z")
    work.add_plan_line(version, item, "1000", "0.8")
    work.approve_plan_version(version, approved_at="2026-01-01T01:00:00Z")
    return dict(project=project, work=work, building=building, floor=floor, item=item, version=version)


@pytest.mark.parametrize("count", [0, 1, 2, 5])
def test_create_workspace_and_buildings_atomically(db, count):
    repo = ProjectRepository(db)
    p = repo.create_project("ЖК Северный", "СЕВ", "Описание", buildings=[
        dict(code="К{}".format(i), name="Корпус {}".format(i)) for i in range(count)])
    assert repo.get_project(p)["description"] == "Описание"
    assert len(WorkRepository(db, p).list_buildings()) == count
    assert WorkRepository(db, p).list_floors() == []
    assert len(WorkRepository(db).list_catalog()) == 15


@pytest.mark.parametrize("bad", [dict(code="К1", name="Повтор"), dict(code="", name="Пустой код"), dict(code="К2", name="")])
def test_invalid_building_rolls_back_project_buildings_and_audit(db, bad):
    before = [db.execute("SELECT count(*) FROM " + table).fetchone()[0]
              for table in ("projects", "buildings", "audit_log")]
    with pytest.raises(ValidationError):
        ProjectRepository(db).create_project("Ошибка", "ERR", buildings=[dict(code="К1", name="Первый"), bad])
    assert before == [db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                      for table in ("projects", "buildings", "audit_log")]


def test_project_codes_are_unique_and_editable(db, school):
    repo = ProjectRepository(db)
    with pytest.raises(ValidationError):
        repo.create_project("Дубль", "ШК")
    created = repo.get_project(school["project"])["created_at"]
    repo.update_project(school["project"], "Школа №2", "ШК2", "Новое описание")
    row = repo.get_project(school["project"])
    assert (row["name"], row["code"], row["description"]) == ("Школа №2", "ШК2", "Новое описание")
    assert row["created_at"] == created


def test_identical_building_and_catalog_codes_in_different_projects(db, school):
    p = ProjectRepository(db).create_project("ЖК", "ЖК", buildings=[dict(code="К1", name="Жилой")])
    work = WorkRepository(db, p)
    b = work.list_buildings()[0]["id"]
    f = work.add_floor(b, "-1", -1)
    other_item = work.add_work_item(b, f, 1)
    assert other_item != school["item"]
    assert work.list_work_items()[0]["catalog_id"] == school["work"].list_work_items()[0]["catalog_id"]
    assert len(work.list_catalog()) == 15
    with pytest.raises(ValidationError):
        work.add_building("К1", "Дубль")


def test_plan_numbering_and_approval_order_are_project_and_discipline_local(db, work, item, school):
    v = work.create_plan_version("CONSTRUCTION", created_at="2026-01-01T00:00:00Z")
    work.add_plan_line(v, item, "1000", "1")
    work.approve_plan_version(v, approved_at="2026-01-01T01:00:00Z")
    design = school["work"].create_plan_version("DESIGN")
    assert work.get_plan_version(v)["version_no"] == 1
    assert school["work"].get_plan_version(school["version"])["version_no"] == 1
    assert school["work"].get_plan_version(design)["version_no"] == 1
    assert school["work"].get_plan_version(school["work"].create_plan_version("CONSTRUCTION"))["version_no"] == 2


def test_renaming_and_reordering_preserve_building_identity_and_links(db, school):
    school["work"].update_building(school["building"], "УЧ", "Учебный корпус", -10)
    assert school["work"].list_work_items()[0]["building_id"] == school["building"]
    assert school["work"].list_floors()[0]["building_id"] == school["building"]
    assert school["work"].list_buildings()[0]["sort_order"] == -10
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_archiving_preserves_reports_and_blocks_writes_until_restored(db, school):
    repo = ProjectRepository(db)
    events = EventRepository(db, school["project"])
    events.add_fact(school["item"], "600", "2026-01-10", event_key="s", recorded_at="2026-01-10T12:00:00Z")
    repo.set_archived(school["project"], True)
    report = HistoricalCalculator(db, school["project"]).calculate_as_of("2026-01-31", "2026-01-31T23:00:00Z", school["version"])
    assert report.total.actual_progress == Decimal("0.6")
    with pytest.raises(ValidationError):
        school["work"].add_floor(school["building"], "2", 2)
    with pytest.raises(ValidationError):
        events.add_fact(school["item"], "1", "2026-01-11", event_key="blocked")
    assert len(events.list_events("fact")) == 1
    repo.set_archived(school["project"], False)
    school["work"].add_floor(school["building"], "2", 2)


def test_archived_building_retains_history_and_can_be_restored(db, school):
    work = school["work"]
    work.archive_building(school["building"])
    assert work.available_work_items("CONSTRUCTION") == []
    with pytest.raises(ValidationError):
        work.add_floor(school["building"], "2", 2)
    work.archive_building(school["building"], False)
    assert len(work.available_work_items("CONSTRUCTION")) == 1


def test_facts_confirmations_and_historical_snapshots_are_isolated(db, events, item, plan, school):
    events.add_fact(item, "600", "2026-01-10", event_key="one", recorded_at="2026-01-10T12:00:00Z")
    events.add_confirmation(item, "450", "2026-01-11", event_key="one-c", document_ref="Акт 1", recorded_at="2026-01-11T12:00:00Z")
    other = EventRepository(db, school["project"])
    other.add_fact(school["item"], "200", "2026-01-10", event_key="two", recorded_at="2026-01-12T12:00:00Z")
    other.add_confirmation(school["item"], "100", "2026-01-12", event_key="two-c", document_ref="Акт 2", recorded_at="2026-01-12T12:00:00Z")
    a = ProgressService(db).dashboard("2026-01-31", "2026-01-31T23:00:00Z")["sections"][0]["total"]
    b = ProgressService(db, school["project"]).dashboard("2026-01-31", "2026-01-31T23:00:00Z")["sections"][0]["total"]
    assert (a.actual_progress, a.confirmed_progress) == (Decimal("0.6"), Decimal("0.45"))
    assert (b.actual_progress, b.confirmed_progress) == (Decimal("0.2"), Decimal("0.1"))
    early = HistoricalCalculator(db, school["project"]).calculate_as_of("2026-01-31", "2026-01-11T23:00:00Z", school["version"])
    assert early.total.actual_progress == 0
    assert HistoricalCalculator(db).calculate_as_of("2026-01-31", "2026-01-31T23:00:00Z", school["version"]).status == Status.INVALID
    assert len(events.list_events("fact")) == len(other.list_events("fact")) == 1


def test_cross_workspace_repository_operations_and_sql_plan_links_are_rejected(db, work, item, school):
    draft = work.create_plan_version("CONSTRUCTION")
    for operation in [lambda: work.add_floor(school["building"], "2", 2),
                      lambda: work.reorder_floor(school["floor"], 10),
                      lambda: work.delete_floor(school["floor"]),
                      lambda: work.get_plan_version(school["version"]),
                      lambda: work.compare_plan_versions(draft, school["version"]),
                      lambda: work.add_plan_line(draft, school["item"], "1", "1"),
                      lambda: EventRepository(db).add_fact(school["item"], "1", "2026-01-10", event_key="bad")]:
        with pytest.raises(ValidationError):
            operation()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO plan_lines(plan_version_id,work_item_id,planned_quantity,labor_norm,unit) VALUES (?,?,'1','1','м²')", (draft, school["item"]))


def test_mixed_project_aggregation_is_invalid(make_input):
    result = aggregate([calculate_item(make_input(project_id=1)), calculate_item(make_input(project_id=2, work_item_id=2))])
    assert result.status == Status.INVALID
    assert result.actual_progress is None
    assert any(i.code == "MIXED_PROJECTS" for i in result.issues)


def test_grouping_refuses_unchecked_mixed_workspace_selection(make_input):
    with pytest.raises(ValueError, match="объектов"):
        group_by([calculate_item(make_input(project_id=1)), calculate_item(make_input(project_id=2))], "building_id")


def test_console_summary_respects_explicit_project_scope(db, school, monkeypatch, capsys):
    import main
    path = db.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setattr("sys.argv", ["main.py", "--db", path, "--cli", "--project-id", str(school["project"])])
    assert main.main() == 0
    output = capsys.readouterr().out
    assert "Школа" in output and "Учебный" in output and "Корпус А" not in output


def test_drafts_are_not_current_work_and_removed_work_keeps_events(db, school):
    work = school["work"]
    second = work.add_work_item(school["building"], school["floor"], 2)
    draft = work.create_plan_version("CONSTRUCTION", base_version_id=school["version"])
    work.add_plan_line(draft, second, "100", "1")
    assert [i["id"] for i in work.available_work_items("CONSTRUCTION")] == [school["item"]]
    EventRepository(db, school["project"]).add_fact(school["item"], "1", "2026-01-10", event_key="kept")
    work.remove_plan_line(draft, school["item"])
    work.approve_plan_version(draft)
    assert [i["id"] for i in work.available_work_items("CONSTRUCTION")] == [second]
    assert len(EventRepository(db, school["project"]).list_events("fact")) == 1
    with pytest.raises(ValidationError):
        work.remove_plan_line(draft, second)


def test_projects_cannot_be_physically_deleted(db, school):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM projects WHERE id=?", (school["project"],))
