"""Single-user loopback WSGI server with explicit lifetime and no reloader."""
import signal
from contextlib import redirect_stderr
from io import StringIO
import threading
import webbrowser

from werkzeug.serving import make_server
from . import create_app


class LocalServer:
    def __init__(self, database, port=0):
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Порт должен быть целым числом от 0 до 65535.")
        self.stop_event = threading.Event()
        self.app = create_app({"DATABASE": str(database), "DEBUG": False})
        # Bind directly to port 0 to avoid a find-free-port / bind race.
        # Werkzeug exits on bind errors instead of raising OSError. Translate that
        # startup failure for Terminal without a traceback or an English fatal log.
        with redirect_stderr(StringIO()):
            try:
                self.httpd = make_server("127.0.0.1", port, self.app, threaded=False)
            except SystemExit:
                raise OSError("Не удалось открыть 127.0.0.1:{}. Порт может быть занят или локальные сокеты недоступны. Используйте --port 0 или другой порт.".format(port)) from None
        self.httpd.timeout = 0.3
        self.origin = "http://127.0.0.1:{}".format(self.httpd.server_port)
        self.app.config["LOCAL_ORIGIN"] = self.origin
        self.app.config["SESSION_COOKIE_NAME"] = "construction_progress_{}".format(self.httpd.server_port)
        self.app.extensions["stop_server"] = self.stop_event.set

    def serve(self, *, open_browser=True):
        previous_handlers = {}
        timer = None
        if threading.current_thread() is threading.main_thread():
            signals = [signal.SIGINT, signal.SIGTERM]
            if hasattr(signal, "SIGHUP"):
                signals.append(signal.SIGHUP)
            for sig in signals:
                previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, lambda *_: self.stop_event.set())
        try:
            print("Construction Progress: " + self.origin, flush=True)
            print("Завершение: кнопка «Завершить работу» или Ctrl+C в Terminal.", flush=True)
            if open_browser:
                def launch_browser():
                    if self.stop_event.is_set():
                        return
                    try:
                        if not webbrowser.open(self.origin):
                            print("Откройте локальный адрес в браузере вручную.", flush=True)
                    except (OSError, webbrowser.Error):
                        print("Не удалось открыть браузер. Используйте локальный адрес выше.", flush=True)
                timer = threading.Timer(0.25, launch_browser)
                timer.daemon = True
                timer.start()
            while not self.stop_event.is_set():
                self.httpd.handle_request()
        finally:
            if timer:
                timer.cancel()
            self.httpd.server_close()
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
            print("Сервер остановлен. Данные сохранены.", flush=True)


def run(database, *, port=0, open_browser=True):
    LocalServer(database, port).serve(open_browser=open_browser)
