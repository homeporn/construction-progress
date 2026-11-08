"""An OS-held lock plus an authenticated live application check, not PID guessing."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .paths import PortableError


def proof(token, challenge):
    return hmac.new(token.encode('ascii'), challenge.encode('ascii'), hashlib.sha256).hexdigest()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def valid_origin(value):
    try:
        parts = urlsplit(value)
        return (parts.scheme == 'http' and parts.hostname == '127.0.0.1' and
                0 < parts.port <= 65535 and parts.netloc == '127.0.0.1:{}'.format(parts.port) and
                not parts.path and not parts.query and not parts.fragment)
    except (ValueError, TypeError):
        return False


def authenticated_probe(origin, token, *, timeout=1):
    if not valid_origin(origin) or not isinstance(token, str) or not 16 <= len(token) <= 128 or not token.isascii():
        return False
    challenge = secrets.token_hex(24)
    request = Request(origin + '/_portable/health', headers={
        'X-CP-Instance': token, 'X-CP-Challenge': challenge})
    # Local requests must neither use a corporate proxy nor follow redirects.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            result = json.loads(response.read(1024))
        return isinstance(result, dict) and isinstance(result.get('proof'), str) and hmac.compare_digest(result['proof'], proof(token, challenge))
    except (OSError, URLError, ValueError, UnicodeError):
        return False


class ProfileInstance:
    def __init__(self, profile):
        self.profile = Path(profile)
        self.lock_path = self.profile / '.instance.lock'
        self.metadata_path = self.profile / '.instance.json'
        self.handle = None
        self.owned = False

    def acquire(self):
        if self.handle is not None:
            return self.owned
        descriptor = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        self.handle = os.fdopen(descriptor, 'r+b', buffering=0)
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    self.handle.write(b'\0')
                self.handle.seek(0)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            self.handle.close(); self.handle = None
            return False
        self.owned = True
        # Keep the lock file itself forever: unlinking a lock admits inode races.
        self.metadata_path.unlink(missing_ok=True)
        return True

    def publish(self, origin, token):
        if not self.owned or not valid_origin(origin):
            raise PortableError('Невозможно зарегистрировать локальный экземпляр.')
        payload = dict(origin=origin, token=token, pid=os.getpid())
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.profile,
                                             prefix='.instance-', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream)
            temporary.replace(self.metadata_path)
        finally:
            if temporary is not None: temporary.unlink(missing_ok=True)

    def existing_origin(self, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.metadata_path.stat().st_size > 4096:
                    raise ValueError('oversize')
                record = json.loads(self.metadata_path.read_text(encoding='utf-8'))
                origin, token = record.get('origin'), record.get('token')
                if not valid_origin(origin):
                    raise PortableError('Некорректный адрес экземпляра. Второй сервер не запущен.')
                if authenticated_probe(origin, token, timeout=min(1, max(.05, deadline-time.monotonic()))):
                    return origin
            except (OSError, ValueError, AttributeError):
                pass
            if time.monotonic() >= deadline:
                raise PortableError('Приложение уже запущено или ещё запускается, но не отвечает. Второй сервер не запущен. Дождитесь завершения запуска или остановите первый экземпляр.')
            time.sleep(.1)

    def close(self):
        if self.owned:
            self.metadata_path.unlink(missing_ok=True)
        if self.handle is not None:
            self.handle.close()  # The OS releases the lock even after a crash.
            self.handle = None
        self.owned = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
