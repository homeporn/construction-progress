"""Exact business projections before/after. Run each code tree in a fresh process."""
import argparse
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from enum import Enum
from io import BytesIO
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from xml.etree import ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]


def normalize(value):
    if is_dataclass(value):
        return normalize(asdict(value))
    if isinstance(value, Decimal):
        text = format(value, 'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, sqlite3.Row):
        return normalize(dict(value))
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items() if k != 'generated_at'}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(normalize(value), sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def workbook_digest(payload):
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    output = {}
    with ZipFile(BytesIO(payload)) as archive:
        for name in sorted(archive.namelist()):
            if not name.startswith('xl/worksheets/') or not name.endswith('.xml'):
                continue
            xml = ET.fromstring(archive.read(name))
            output[name] = [
                [cell.get('r'), cell.get('t'), cell.get('s'),
                 cell.findtext('s:v', '', ns), ''.join(t.text or '' for t in cell.findall('.//s:t', ns))]
                for cell in xml.findall('.//s:c', ns)]
    return digest(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--code-root', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    source = Path(args.db).resolve()
    meta = json.loads(source.with_suffix('.json').read_text())
    if meta.get('marker') != 'cp421-fresh-v1' or ROOT in source.parents:
        parser.error('Только синтетические базы вне каталога проекта')
    sys.path.insert(0, str(Path(args.code_root).resolve()))
    from app.calculation_engine.historical import HistoricalCalculator
    from app.repositories.timesheet_repository import TimesheetRepository
    from app.services.analytics_service import AnalyticsService
    from app.services.timesheet_xlsx import export_timesheet_xlsx
    from app.services.xlsx_export import export_xlsx
    db = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, isolation_level=None)
    db.row_factory = sqlite3.Row
    checks = {}
    for ref in meta['refs']:
        pid = ref['project_id']
        core = HistoricalCalculator(db, pid)
        for version in db.execute("SELECT id,discipline,version_no FROM plan_versions WHERE project_id=? ORDER BY id", (pid,)):
            for report_date, cutoff in [('2026-08-05', '2026-09-03T23:59:59Z'),
                                        ('2026-08-05', meta['knowledge_cutoff']),
                                        (meta['report_date'], meta['knowledge_cutoff'])]:
                report = core.calculate_as_of(report_date, cutoff, version['id'])
                checks['plan/{}/{}/{}/{}'.format(pid, version['id'], report_date, cutoff)] = digest(report)
        analytical = AnalyticsService(db, pid).build(meta['report_date'], meta['knowledge_cutoff'], bucket='range')
        checks['analytics/' + str(pid)] = digest(analytical)
        group = AnalyticsService(db, pid).build(meta['report_date'], meta['knowledge_cutoff'], bucket='range', group_id=ref['group_id'])
        checks['group/' + str(pid)] = digest(group)
        report = TimesheetRepository(db, pid).report(ref['timesheet_id'])
        report['generated_at'] = meta['knowledge_cutoff']
        checks['timesheet/' + str(pid)] = digest(report)
        if pid == meta['refs'][0]['project_id']:
            checks['excel/analytics'] = workbook_digest(export_xlsx(analytical))
            checks['excel/timesheet'] = workbook_digest(export_timesheet_xlsx(report))
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
    db.close()
    result = dict(profile=meta['profile'], checks=checks, count=len(checks))
    with Path(args.output).open('x', encoding='utf-8') as out:
        json.dump(result, out, indent=2)
    print(meta['profile'], len(checks), 'projections captured')


if __name__ == '__main__':
    main()
