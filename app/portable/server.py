"""Waitress loopback controller with atomic socket reservation and request drain.

Only this module uses Waitress lifecycle internals (pinned to 3.0.2). The existing
macOS/Werkzeug runner and application routes are not replaced.
"""
import secrets
import socket
import threading
import time

from flask import abort, jsonify, request
from waitress import create_server
from waitress import wasyncore
from waitress.task import ThreadedTaskDispatcher

from .instance import authenticated_probe, proof
from .paths import PortableError


class DrainingApplication:
    def __init__(self, app):
        self.app = app
        self.condition = threading.Condition()
        self.active = 0
        self.draining = False

    def __call__(self, environ, start_response):
        with self.condition:
            rejected = self.draining
            if not rejected: self.active += 1
        if rejected:
            start_response('503 Service Unavailable', [('Content-Type', 'text/plain; charset=utf-8'),
                                                       ('Retry-After', '5')])
            yield 'Приложение завершает работу. Новые операции не сохраняются.'.encode('utf-8')
            return
        iterable = None
        try:
            iterable = self.app(environ, start_response)
            yield from iterable
        finally:
            try:
                if iterable is not None and hasattr(iterable, 'close'):
                    iterable.close()
            finally:
                with self.condition:
                    self.active -= 1
                    self.condition.notify_all()


class PortableServer:
    def __init__(self, app, *, token, port=0):
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise PortableError('Порт должен быть целым числом от 0 до 65535.')
        self.app = app
        self.token = token
        self.stop_event = threading.Event()
        self.io_stop = threading.Event()
        self.close_lock = threading.Lock()
        self.closed = False
        self.failure = None
        self.thread = None
        self.socket_map = {}
        self.wrapper = DrainingApplication(app)
        self.dispatcher = ThreadedTaskDispatcher()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            # This socket remains open and is passed directly to Waitress.
            sock.bind(('127.0.0.1', port))
            self.httpd = create_server(self.wrapper, sockets=[sock], map=self.socket_map,
                                      _dispatcher=self.dispatcher, threads=4,
                                      asyncore_loop_timeout=.1, channel_timeout=30,
                                      expose_tracebacks=False, clear_untrusted_proxy_headers=True)
            self.dispatcher.set_thread_count(4)
        except (OSError, ValueError) as exc:
            sock.close()
            wasyncore.close_all(map=self.socket_map, ignore_all=True)
            self.dispatcher.shutdown()
            raise PortableError('Не удалось запустить локальный сервер. Порт {} занят или недоступен. Используйте --port 0.'.format(port)) from exc
        self.origin = 'http://127.0.0.1:{}'.format(self.httpd.effective_port)
        app.config.update(DEBUG=False, PROPAGATE_EXCEPTIONS=False, LOCAL_ORIGIN=self.origin,
                          SESSION_COOKIE_NAME='construction_progress_{}'.format(self.httpd.effective_port))
        app.extensions['stop_server'] = self.request_stop

        @app.get('/_portable/health', endpoint='portable_health')
        def health():
            supplied = request.headers.get('X-CP-Instance', '')
            challenge = request.headers.get('X-CP-Challenge', '')
            if (not secrets.compare_digest(supplied.encode('utf-8'), token.encode('ascii')) or
                    len(challenge) != 48 or any(c not in '0123456789abcdef' for c in challenge)):
                abort(403)
            return jsonify(proof=proof(token, challenge))

    def _io_loop(self):
        try:
            while not self.io_stop.is_set():
                wasyncore.loop(timeout=.1, count=1, map=self.socket_map)
        except Exception as exc:
            self.failure = type(exc).__name__
            self.stop_event.set()

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._io_loop, name='portable-http', daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if authenticated_probe(self.origin, self.token):
                return
            if self.failure:
                break
            time.sleep(.05)
        self.close()
        raise PortableError('Локальный сервер не прошёл проверку готовности. Проверьте logs/application.log.')

    def request_stop(self):
        self.stop_event.set()

    def wait(self):
        while not self.stop_event.wait(.2):
            pass
        if self.failure:
            raise PortableError('Локальный сервер остановился с ошибкой. Проверьте logs/application.log.')

    def close(self):
        with self.close_lock:
            if self.closed:
                return
            self.stop_event.set()
            self.httpd.accepting = False
            with self.wrapper.condition:
                self.wrapper.draining = True
                # Never kill a running accounting write or XLSX operation.
                while self.wrapper.active:
                    self.wrapper.condition.wait(.1)
            # Already queued requests enter the draining wrapper and get 503.
            # Keep the I/O loop alive until workers finish and replies flush.
            while self.thread is not None and self.thread.is_alive():
                with self.dispatcher.lock:
                    busy = bool(self.dispatcher.queue or self.dispatcher.active_count)
                if not busy:
                    break
                time.sleep(.02)
            deadline = time.monotonic() + 5
            while self.thread is not None and self.thread.is_alive() and time.monotonic() < deadline:
                if not any(getattr(channel, 'total_outbufs_len', 0) for channel in list(self.httpd.active_channels.values())):
                    break
                time.sleep(.02)
            self.io_stop.set()
            if self.thread is not None:
                self.thread.join()
            self.dispatcher.shutdown(cancel_pending=True, timeout=5)
            self.httpd.close()
            wasyncore.close_all(map=self.socket_map, ignore_all=True)
            self.closed = True
