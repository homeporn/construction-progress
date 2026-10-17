"""Local web application with retained stage 1 console and demonstration tools."""
from dataclasses import asdict
from decimal import Decimal
from enum import Enum
import json
import sqlite3

from app.launch import LaunchArgumentParser, port_number, select_database
from app.database import initialize
from app.calculation_engine.historical import HistoricalCalculator
from app.repositories.event_repository import EventRepository
from app.repositories.work_repository import WorkRepository


def json_value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    raise TypeError(type(value).__name__)


def demo():
    """Illustrative user inputs, confined to an in-memory database."""
    db = initialize(":memory:")
    try:
        work = WorkRepository(db)
        floor = work.add_floor(1, "1", 1)
        item = work.add_work_item(1, floor, 1)
        version = work.create_plan_version("CONSTRUCTION", created_at="2026-01-01T08:00:00Z")
        work.add_plan_line(version, item, "1000", "0.8")
        work.approve_plan_version(version, approved_at="2026-01-01T09:00:00Z")
        events = EventRepository(db)
        events.add_fact(item, "600", "2026-01-10", event_key="demo-fact-1",
                        recorded_at="2026-01-10T18:00:00Z")
        events.add_confirmation(item, "450", "2026-01-11", event_key="demo-confirm-1",
                                document_ref="Учебный акт №1", recorded_at="2026-01-12T10:00:00Z")
        return HistoricalCalculator(db).calculate_as_of("2026-01-31", "2026-01-31T23:59:59Z", version)
    finally:
        db.close()


def main():
    parser = LaunchArgumentParser(description="Construction Progress — локальное приложение, подготовка к испытаниям macOS")
    parser.add_argument("--db", help="Путь к рабочей SQLite-базе; по умолчанию data/construction_progress.sqlite3")
    parser.add_argument("--demo", action="store_true", help="Пример в памяти: 60%% факта, 45%% подтверждения")
    parser.add_argument("--plan-version", type=int, help="Номер записи версии для консольного отчёта")
    parser.add_argument("--report-date", help="Дата отчёта в формате ГГГГ-ММ-ДД")
    parser.add_argument("--knowledge-cutoff", help="Момент знаний ISO с часовым поясом, например 2026-10-17T18:00:00Z")
    parser.add_argument("--cli", action="store_true", help="Показать сводку базы в консоли без запуска сервера")
    parser.add_argument("--no-browser", action="store_true", help="Не открывать браузер автоматически")
    parser.add_argument("--port", type=port_number, default=0, help="Локальный порт; 0 — свободный порт автоматически")
    parser.add_argument("--check", action="store_true", help="Офлайн-диагностика без запуска сервера и изменения базы")
    parser.add_argument("--test-db", action="store_true", help="Отдельная сохраняемая база приёмки data/acceptance/acceptance.sqlite3")
    args = parser.parse_args()
    try:
        args.db = select_database(args.db, test_database=args.test_db)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.check:
        if args.demo or args.cli or args.plan_version or args.report_date or args.knowledge_cutoff:
            parser.error("--check не совмещается с консольным отчётом или демонстрацией.")
        from scripts.check_environment import diagnose
        return diagnose(args.db, port=args.port, test_database=args.test_db)
    if args.test_db and args.demo:
        parser.error("--demo уже использует базу в памяти. Для приёмки интерфейса используйте только --test-db.")
    if args.test_db:
        print("РЕЖИМ ПРИЁМКИ: используется отдельная тестовая база: " + str(args.db), flush=True)
        print("Рабочая база не открывается. Данные приёмки сохраняются между запусками.", flush=True)
    if args.demo:
        print(json.dumps(asdict(demo()), default=json_value, ensure_ascii=False, indent=2))
        return 0
    if any((args.plan_version, args.report_date, args.knowledge_cutoff)) and not all(
            (args.plan_version, args.report_date, args.knowledge_cutoff)):
        parser.error("Для среза нужны --plan-version, --report-date и --knowledge-cutoff")
    try:
        if not args.cli and not args.plan_version:
            try:
                from app.web.server import run
            except ModuleNotFoundError:
                print("Не найдены веб-зависимости. Выполните ./start.command --prepare для явной установки.")
                return 2
            run(args.db, port=args.port, open_browser=not args.no_browser)
            return 0
        db = initialize(args.db)
        try:
            if args.plan_version:
                result = HistoricalCalculator(db).calculate_as_of(
                    args.report_date, args.knowledge_cutoff, args.plan_version)
                print(json.dumps(asdict(result), default=json_value, ensure_ascii=False, indent=2))
                return 2 if result.status.value == "INVALID" else 0
            print("Construction Progress — локальная база")
            print("База:", args.db)
            for row in db.execute("SELECT code,name FROM buildings ORDER BY id"):
                print("{}: {}".format(row["code"], row["name"]))
            print("Кодов работ:", db.execute("SELECT count(*) FROM work_catalog").fetchone()[0])
            print("Пример расчёта: python main.py --demo")
        finally:
            db.close()
    except (ValueError, sqlite3.Error, OSError) as exc:
        print("Ошибка:", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
