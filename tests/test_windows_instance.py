import json
import subprocess
import sys

import pytest

from app.config import PROJECT_ROOT


def test_lock_is_enforced_by_os_and_released_after_owner_exits(tmp_path):
    from app.portable.instance import ProfileInstance
    code = ('from app.portable.instance import ProfileInstance; import sys; '
            's=ProfileInstance(sys.argv[1]); print(s.acquire(),flush=True); input()')
    process = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)], cwd=PROJECT_ROOT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'True'
        follower = ProfileInstance(tmp_path)
        assert follower.acquire() is False
        follower.close()
    finally:
        process.communicate('\n', timeout=5)
    follower = ProfileInstance(tmp_path)
    assert follower.acquire() is True
    follower.close()


def test_stale_metadata_alone_does_not_block_startup_and_token_is_not_in_url(tmp_path):
    from app.portable.instance import ProfileInstance
    profile = ProfileInstance(tmp_path)
    profile.metadata_path.write_text('{"pid":999999,"origin":"http://127.0.0.1:1234"}')
    assert profile.acquire()
    profile.publish('http://127.0.0.1:12345', 'secret-value')
    record = json.loads(profile.metadata_path.read_text())
    assert record['origin'] == 'http://127.0.0.1:12345'
    assert 'secret' not in record['origin']
    profile.close()
    assert not profile.metadata_path.exists()


@pytest.mark.parametrize('origin', ['http://example.com:80','http://localhost:80','http://127.0.0.1:5/path',
                                  'http://127.0.0.1:5/?token=a','https://127.0.0.1:5',
                                  'http://user@127.0.0.1:5','http://127.0.0.1:0'])
def test_instance_metadata_cannot_request_nonlocal_addresses(tmp_path, origin):
    from app.portable.instance import ProfileInstance
    from app.portable.paths import PortableError
    s = ProfileInstance(tmp_path)
    s.metadata_path.write_text(json.dumps(dict(origin=origin, token='a'*64)))
    with pytest.raises(PortableError): s.existing_origin(timeout=0)


def test_locked_but_unresponsive_instance_is_never_replaced(tmp_path):
    from app.portable.instance import ProfileInstance
    from app.portable.paths import PortableError
    owner = ProfileInstance(tmp_path); assert owner.acquire()
    other = ProfileInstance(tmp_path); assert other.acquire() is False
    with pytest.raises(PortableError, match='запущен|запуск'): other.existing_origin(timeout=0)
    other.close()
    assert ProfileInstance(tmp_path).acquire() is False
    owner.close()


def test_technical_formatter_drops_personal_data_and_tracebacks():
    import logging
    from app.portable.logging import SafeFormatter
    try:
        raise ValueError('Секретное ФИО из Excel')
    except ValueError:
        record = logging.LogRecord('app.web', logging.ERROR, __file__, 1,
                                   'Ошибка: %s', ('Секретное ФИО',), sys.exc_info())
    value = SafeFormatter().format(record)
    assert 'ValueError' in value and 'ERROR' in value
    assert 'Секрет' not in value and 'Traceback' not in value
