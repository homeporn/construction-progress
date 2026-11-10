# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH).resolve().parents[1]
sys.path.insert(0, str(root))
from app.branding import PRODUCT_SLUG
from tools.windows.bundle import resource_entries

manifest = Path(os.environ['CP_WINDOWS_RESOURCE_MANIFEST'])
if not manifest.is_file():
    raise RuntimeError('Generate the checked resource manifest before building')
datas = [(str(source), str(Path(relative).parent)) for source, relative in resource_entries(root)]
datas += [(str(manifest), '.')]
for distribution in ('Flask', 'Werkzeug', 'Jinja2', 'MarkupSafe', 'itsdangerous', 'click', 'blinker', 'waitress'):
    datas += copy_metadata(distribution)

a = Analysis([str(root/'windows_launcher.py')], pathex=[str(root)], binaries=[], datas=datas,
             hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=['pytest'],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=PRODUCT_SLUG,
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, disable_windowed_traceback=False, contents_directory='_internal',
          version=str(root/'packaging/windows/version_info.txt'), uac_admin=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=PRODUCT_SLUG)
