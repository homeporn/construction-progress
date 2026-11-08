"""Build manifests, validate real Windows artifacts and package explicit files.

The Build Kit is source and build instructions, never a fake portable executable.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
from zipfile import ZipFile, ZIP_DEFLATED

from app.portable.resources import validate_resources

ROOT = Path(__file__).resolve().parents[2]


def resource_entries(root):
    root = Path(root)
    result = []
    for folder in ('app/web/templates', 'app/web/static'):
        for path in sorted((root/folder).rglob('*')):
            if path.is_file():
                if path.is_symlink(): raise ValueError('Symlink in bundle resources')
                result.append((path, path.relative_to(root).as_posix()))
    for path in sorted((root/'migrations').glob('*.sql')):
        result.append((path, path.relative_to(root).as_posix()))
    for version in ('4_1', '4_2'):
        matches = list((root/'docs/templates').glob('Construction_Progress_{}_*.xlsx'.format(version)))
        if len(matches) != 1: raise ValueError('Expected one Excel template for '+version)
        result.append((matches[0], 'excel_templates/'+matches[0].name))
    validate_resources(root)
    return result


def write_resource_manifest(root, output):
    result = {target: hashlib.sha256(source.read_bytes()).hexdigest() for source, target in resource_entries(root)}
    Path(output).write_text(json.dumps(dict(format=1, resources=result), ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def check_windows_exe(path):
    with Path(path).open('rb') as stream:
        header = stream.read(64)
        if len(header) < 64 or header[:2] != b'MZ':
            raise ValueError('Not a real Windows PE executable')
        stream.seek(struct.unpack_from('<I', header, 60)[0])
        pe = stream.read(6)
        if pe[:4] != b'PE\0\0' or len(pe) != 6 or struct.unpack_from('<H', pe, 4)[0] != 0x8664:
            raise ValueError('Expected Windows PE x64, not Linux or another architecture')


def validate_portable(folder):
    folder = Path(folder)
    check_windows_exe(folder/'ConstructionProgress.exe')
    validate_resources(folder/'_internal', require_manifest=True)
    if not (folder/'_internal/python313.dll').is_file():
        raise ValueError('Missing bundled Python runtime')
    for name in ('README.txt', 'VERSION.txt', 'BUILD_INFO.json'):
        if not (folder/name).is_file(): raise ValueError('Missing '+name)
    for path in folder.rglob('*'):
        relative = path.relative_to(folder)
        if path.is_symlink() or any(part in ('.git','.venv','.pytest_cache','__pycache__','tests') for part in relative.parts):
            raise ValueError('Unexpected development files in portable folder')
        if path.is_file() and (path.suffix.lower() in ('.sqlite3','.db','.sqlite') or
                               relative.parts[0] in ('data','backups','logs','exports')):
            raise ValueError('User data must never be included in a distribution')


def package_portable(folder, output):
    folder, output = Path(folder), Path(output)
    validate_portable(folder)
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, 'x', ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob('*')):
            if path.is_file(): archive.write(path, 'ConstructionProgress/'+path.relative_to(folder).as_posix())
        for directory in ('data','backups','logs','exports'):
            archive.writestr('ConstructionProgress/'+directory+'/', b'')
    return output


def create_buildkit(root, output):
    root, output = Path(root), Path(output)
    selected = set()
    for folder in ('app','migrations','requirements','scripts','tests','tools','packaging/windows','docs/windows'):
        for path in (root/folder).rglob('*'):
            if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts and path.suffix in ('.py','.sql','.txt','.ps1','.spec','.cmd','.md','.json'):
                selected.add(path)
    for name in ('windows_launcher.py','main.py','start.command','pyproject.toml','README.md',
                 'docs/windows_portable.md','docs/windows_build_results.md',
                 'docs/stage421_results.md','docs/stage421_performance.md'):
        path=root/name
        if not path.is_file(): raise ValueError('Missing required build source '+name)
        selected.add(path)
    for source, _ in resource_entries(root): selected.add(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix='ConstructionProgress-Windows-BuildKit/'
    hashes={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(selected)}
    with ZipFile(output, 'x', ZIP_DEFLATED) as archive:
        for path in sorted(selected): archive.write(path,prefix+path.relative_to(root).as_posix())
        archive.writestr(prefix+'SOURCE_SHA256.json',json.dumps(hashes,ensure_ascii=False,indent=2)+'\n')
        archive.writestr(prefix+'README_WINDOWS.txt',(root/'packaging/windows/BUILD_KIT_README.txt').read_bytes())
        archive.writestr(prefix+'BUILD_WINDOWS.cmd',(root/'packaging/windows/BUILD_WINDOWS.cmd').read_bytes())
    return output


def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('manifest','buildkit','zip'):
        command=sub.add_parser(name)
        command.add_argument('--output',required=True,type=Path)
        command.add_argument('--root',default=ROOT,type=Path)
        if name=='zip': command.add_argument('--folder',required=True,type=Path)
    args=parser.parse_args()
    if args.command=='manifest': write_resource_manifest(args.root,args.output)
    elif args.command=='buildkit': print(create_buildkit(args.root,args.output))
    else: print(package_portable(args.folder,args.output))


if __name__=='__main__': main()
