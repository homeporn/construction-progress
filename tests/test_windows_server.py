"""Real loopback Waitress tests; no Internet and no working database."""
from contextlib import contextmanager
from http.cookiejar import CookieJar
import json
import re
import secrets
import socket
import sqlite3
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

import pytest

from app.web import create_app, get_db


@pytest.fixture(autouse=True)
def require_loopback_socket():
    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
    except PermissionError:
        pytest.skip('Sandbox forbids loopback sockets; run this test on the Windows build machine.')


@contextmanager
def running(tmp_path, configure=None):
    from app.portable.server import PortableServer
    app = create_app({'DATABASE': str(tmp_path / 'Проверка Windows.sqlite3')})
    if configure: configure(app)
    token = secrets.token_hex(32)
    server = PortableServer(app, token=token)
    server.start()
    try:
        yield server, token
    finally:
        server.close()


def client():
    return build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))


def read(opener, origin, path='/', data=None, headers=None):
    encoded = urlencode(data).encode() if data is not None else None
    request = Request(origin + path, data=encoded, headers=headers or {})
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read(), response.headers
    except HTTPError as response:
        return response.code, response.read(), response.headers


def csrf(opener, origin):
    status, body, _ = read(opener, origin)
    assert status == 200
    return re.search(rb'name="csrf_token" value="([^"]+)"', body).group(1).decode()


def test_waitress_bind_ready_security_and_all_packaged_assets(tmp_path):
    from app.portable.instance import authenticated_probe
    with running(tmp_path) as (server, token):
        assert server.httpd.effective_host == '127.0.0.1'
        assert server.origin.startswith('http://127.0.0.1:')
        assert not server.app.debug
        assert authenticated_probe(server.origin, token)
        assert not authenticated_probe(server.origin, 'wrong-secret-value')
        opener = client()
        for path in ('/', '/projects', '/volumes', '/facts', '/confirmations', '/work-orders',
                     '/personnel/workers', '/timesheets', '/analytics',
                     '/static/css/app.css', '/static/css/timesheets.css',
                     '/static/js/app.js', '/static/js/timesheets.js',
                     '/personnel/import/template.xlsx', '/timesheets/template.xlsx'):
            status, body, headers = read(opener, server.origin, path)
            assert status == 200, (path, status)
            assert body and headers['X-Content-Type-Options'] == 'nosniff'
        assert read(opener, server.origin, headers={'Host': 'example.com'})[0] == 403
        assert read(opener, server.origin, '/_portable/health')[0] == 403


def test_shutdown_requires_origin_cookie_and_csrf_even_with_instance_token(tmp_path):
    with running(tmp_path) as (server, token):
        opener = client()
        assert read(opener, server.origin, '/shutdown')[0] == 405
        assert read(opener, server.origin, '/shutdown', {}, {'Origin': server.origin, 'X-CP-Instance': token})[0] == 403
        key = csrf(opener, server.origin)
        assert read(opener, server.origin, '/shutdown', {'csrf_token': key}, {'Origin': 'http://evil.example'})[0] == 403
        assert not server.stop_event.is_set()
        status, _, _ = read(opener, server.origin, '/shutdown', {'csrf_token': key}, {'Origin': server.origin})
        assert status == 200 and server.stop_event.is_set()
        server.close()
        assert not server.thread.is_alive()
        db = sqlite3.connect(server.app.config['DATABASE'], timeout=.2)
        db.execute('BEGIN EXCLUSIVE'); db.rollback(); db.close()


def test_no_browser_tab_lifetime_dependency_and_close_releases_port(tmp_path):
    with running(tmp_path) as (server, _):
        opener = client()
        assert read(opener, server.origin)[0] == 200
        assert not server.stop_event.is_set()
        port = int(server.httpd.effective_port)
        server.close()
        server.close()  # Idempotent controller cleanup.
        with socket.socket() as sock:
            sock.settimeout(.2)
            assert sock.connect_ex(('127.0.0.1', port)) != 0


def test_already_running_instance_is_authenticated_before_reopening_browser(tmp_path):
    from app.portable.instance import ProfileInstance
    with running(tmp_path) as (server, token):
        owner = ProfileInstance(tmp_path); assert owner.acquire()
        try:
            owner.publish(server.origin, token)
            follower = ProfileInstance(tmp_path); assert follower.acquire() is False
            assert follower.existing_origin(timeout=2) == server.origin
            follower.close()
        finally: owner.close()


def test_close_waits_for_transaction_and_sqlite_teardown(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def configure(app):
        @app.get('/test-active-write')
        def slow():
            db = get_db()
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT INTO floors(building_id,floor_code,sort_order) VALUES (1,'Операция завершена',7)")
            entered.set()
            assert release.wait(10)
            db.commit()
            return 'saved'
    with running(tmp_path, configure) as (server, _):
        result = []
        writer = threading.Thread(target=lambda: result.append(read(client(), server.origin, '/test-active-write')))
        writer.start()
        assert entered.wait(5)
        closer = threading.Thread(target=server.close)
        closer.start()
        closer.join(.1)
        assert closer.is_alive()
        release.set()
        writer.join(10); closer.join(10)
        assert not closer.is_alive() and result[0][0] == 200
        db = sqlite3.connect(server.app.config['DATABASE'])
        assert db.execute('SELECT floor_code FROM floors').fetchall() == [('Операция завершена',)]
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        db.close()


def test_busy_port_and_invalid_port_are_actionable_errors(tmp_path):
    from app.portable.server import PortableServer
    from app.portable.paths import PortableError
    app = create_app({'DATABASE': str(tmp_path / 'test.sqlite3')})
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1',0)); occupied.listen()
        with pytest.raises(PortableError, match='[Пп]орт|сервер'):
            PortableServer(app, token='a'*64, port=occupied.getsockname()[1])
    with pytest.raises(PortableError): PortableServer(app, token='a'*64, port=-1)


def test_unexpected_web_error_does_not_show_traceback(tmp_path):
    def configure(app):
        @app.get('/test-failure')
        def failure(): raise RuntimeError('private marker not to display')
    with running(tmp_path, configure) as (server, _):
        status, body, _ = read(client(), server.origin, '/test-failure')
        assert status == 500
        assert b'Traceback' not in body and b'private marker' not in body
