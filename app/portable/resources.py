"""Explicit bundle resources, checked before touching the user's database."""
import hashlib
import json
from pathlib import Path, PurePosixPath

from .paths import PortableError

CORE = ('app/web/templates/base.html', 'app/web/templates/error.html',
        'app/web/templates/stopped.html', 'app/web/templates/timesheet_detail.html',
        'app/web/templates/work_order_print.html', 'app/web/static/css/app.css',
        'app/web/static/js/app.js', 'migrations/001_initial.sql', 'migrations/010_timesheets.sql')


def validate_resources(root, *, require_manifest=False):
    root = Path(root).resolve()
    for name in CORE:
        if not (root / name).is_file():
            raise PortableError('Отсутствует ресурс или шаблон {}. Распакуйте полную папку приложения заново, сохранив профиль данных.'.format(name))
    manifest = root / 'bundle-manifest.json'
    if not manifest.exists() and not require_manifest:
        return
    try:
        if manifest.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('manifest too large')
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if data['format'] != 1 or not isinstance(data['resources'], dict) or not set(CORE) <= set(data['resources']):
            raise ValueError('invalid manifest')
        for name, expected in data['resources'].items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:
                raise ValueError('unsafe resource path')
            path = (root / name).resolve()
            if root not in path.parents or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError('resource mismatch')
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise PortableError('Проверка ресурсов сборки не пройдена. Распакуйте полный архив приложения заново; базу данных не заменяйте.') from exc
