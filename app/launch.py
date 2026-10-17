"""Standard-library-only launch helpers; no database creation or web imports."""
import argparse
from pathlib import Path
import socket

from app.config import DEFAULT_DB_PATH, PROJECT_ROOT

TEST_DB_PATH = PROJECT_ROOT / "data" / "acceptance" / "acceptance.sqlite3"


class LaunchArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = "Позиционные аргументы"
        self._optionals.title = "Параметры"
        self.add_argument("-h", "--help", action="help", help="Показать справку и завершить работу")

    def format_usage(self):
        return super().format_usage().replace("usage:", "Использование:", 1)

    def format_help(self):
        return super().format_help().replace("usage:", "Использование:", 1)

    def error(self, message):
        message = message.replace("unrecognized arguments:", "Неизвестные параметры:")
        message = message.replace("expected one argument", "ожидается значение")
        message = message.replace("argument ", "параметр ")
        message = message.replace("invalid int value:", "требуется целое число:")
        self.exit(2, "Ошибка параметров: " + message + "\nСправка: ./start.command --help\n")


def port_number(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("порт должен быть целым числом от 0 до 65535") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("порт должен быть целым числом от 0 до 65535")
    return port


def select_database(database=None, *, test_database=False):
    if test_database:
        if database is not None:
            raise ValueError("--test-db и --db нельзя использовать вместе: тестовый режим имеет отдельную фиксированную базу.")
        target, working = TEST_DB_PATH.resolve(), DEFAULT_DB_PATH.resolve()
        if target == working or (target.exists() and working.exists() and target.samefile(working)):
            raise ValueError("Тестовая база ссылается на рабочую. Уберите эту ссылку и повторите запуск; данные не изменены.")
        return target
    return Path(database).expanduser().resolve() if database is not None else DEFAULT_DB_PATH.resolve()


def probe_local_port(port=0):
    """Readiness probe only. The real server performs its own atomic bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        # Match Werkzeug's reuse behavior after a clean restart (TIME_WAIT).
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        connection.bind(("127.0.0.1", port))
        return connection.getsockname()[1]
