from contextlib import nullcontext
from datetime import date
from decimal import Decimal
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

import pytest

from app.repositories.base import ValidationError
from app.services.timesheet_xlsx import (
    HEADERS, MAX_UPLOAD, TEMPLATE_NAME, export_timesheet_xlsx,
    make_timesheet_template, parse_timesheet_xlsx,
)
from app.services.xlsx_export import pack_workbook, sheet

N = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def book(rows=None, headers=HEADERS, names=None):
    names = names or ['Табель']
    rows = [[101, 'Иванов Иван', date(2026, 10, 1), 'Я', Decimal('8')]] if rows is None else rows
    return pack_workbook(names, [sheet(list(headers), rows, exact_column=False) for _ in names])


def edit(payload, path, change):
    result = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(result, 'w', ZIP_DEFLATED) as target:
        for item in source.infolist():
            body = source.read(item.filename)
            target.writestr(item.filename, change(body) if item.filename == path else body)
    return result.getvalue()


def xml(payload, path):
    with ZipFile(BytesIO(payload)) as archive:
        return ET.fromstring(archive.read(path))


def test_template_is_empty_only_agreed_sheet_is_read():
    assert TEMPLATE_NAME.endswith('.xlsx')
    assert parse_timesheet_xlsx(make_timesheet_template()) == []
    with ZipFile(BytesIO(make_timesheet_template())) as archive:
        assert archive.testzip() is None
    payload = book(names=['Пример', 'Табель', 'Инструкция'])
    assert len(parse_timesheet_xlsx(payload)) == 1


def test_native_date_decimal_and_stable_id():
    result = parse_timesheet_xlsx(book([[101, '  Иванов   Иван  ', date(2026, 10, 1), 'я', Decimal('6.125')]]))[0]
    assert result == dict(row_number=2, employee_id=101, full_name='Иванов Иван',
                          work_date='2026-10-01', code='Я', hours='6.125', errors=[])


def test_column_order_iso_date_and_russian_decimal():
    payload = book([['8,5', 'Я', '2026-10-01', 'Иванов Иван', '101']],
                   headers=['Часы', 'Код', 'Дата', 'ФИО', 'ID работника'])
    result = parse_timesheet_xlsx(payload)[0]
    assert result['hours'] == '8.5' and result['employee_id'] == 101 and not result['errors']


def test_excel_1904_dates_used_on_mac():
    payload = book([[101, 'Иванов Иван', date(2026, 10, 1), 'Я', 8]])
    root = xml(payload, 'xl/worksheets/sheet1.xml')
    cell = root.find("s:sheetData/s:row[@r='2']/s:c[@r='C2']/s:v", N)
    serial = int(cell.text)
    payload = edit(payload, 'xl/worksheets/sheet1.xml',
                   lambda body: body.replace(f'<v>{serial}</v>'.encode(), f'<v>{serial - 1462}</v>'.encode()))
    payload = edit(payload, 'xl/workbook.xml', lambda body: body.replace(b'<sheets>', b'<workbookPr date1904="1"/><sheets>'))
    assert parse_timesheet_xlsx(payload)[0]['work_date'] == '2026-10-01'


@pytest.mark.parametrize('serial', [60, Decimal('46300.5'), Decimal('-1')])
def test_invalid_or_time_component_excel_dates(serial):
    payload = book([[101, 'Иванов Иван', date(2026, 10, 1), 'Я', 8]])
    root = xml(payload, 'xl/worksheets/sheet1.xml')
    root.find("s:sheetData/s:row[@r='2']/s:c[@r='C2']/s:v", N).text = str(serial)
    payload = edit(payload, 'xl/worksheets/sheet1.xml', lambda unused: ET.tostring(root))
    assert any('Дата:' in error for error in parse_timesheet_xlsx(payload)[0]['errors'])


def test_absent_hours_are_not_zero_and_absent_record_not_created():
    rows = parse_timesheet_xlsx(book([[101, 'Иванов Иван', '2026-10-01', 'В', None],
                                      [101, 'Иванов Иван', '2026-10-02', 'В', 0],
                                      [None, None, None, None, None]]))
    assert len(rows) == 2 and rows[0]['hours'] is None and rows[1]['hours'] == '0'


@pytest.mark.parametrize('index,value,fragment', [
    (0, '', 'ID работника'), (0, 0, 'ID работника'), (0, -1, 'ID работника'),
    (0, Decimal('1.2'), 'ID работника'), (0, '9223372036854775808', 'ID работника'),
    (1, '', 'ФИО'), (1, 123, 'ФИО'), (2, '2026-02-29', 'Дата'),
    (2, '2026-10-01T08:00:00', 'Дата'), (2, '20261001', 'Дата'), (2, 46296, 'Дата'),
    (3, '', 'Код'), (3, 1, 'Код'), (4, -1, 'Часы'), (4, 25, 'Часы'),
    (4, 'NaN', 'Часы'), (4, 'не число', 'Часы'), (4, '1E-999999999', 'Часы'), (4, '1E999999999', 'Часы'),
    (4, '0.0000000001', 'Часы'),
])
def test_invalid_values_have_specific_row_errors(index, value, fragment):
    row = [101, 'Иванов Иван', '2026-10-01', 'Я', 8]
    row[index] = value
    result = parse_timesheet_xlsx(book([row]))[0]
    assert any(fragment in error for error in result['errors'])


@pytest.mark.parametrize('hours', ['0.000000001', '8.0000000000', '0E-999999999', '24'])
def test_valid_exact_hours_and_zero_exponents(hours):
    result = parse_timesheet_xlsx(book([[101, 'Иванов Иван', '2026-10-01', 'Я', hours]]))[0]
    assert not result['errors'] and Decimal(result['hours']) == Decimal(hours)


def test_employee_date_duplicates_are_errors_on_both_rows():
    rows = parse_timesheet_xlsx(book([[101, 'Иванов Иван', '2026-10-01', 'Я', 8],
                                      [101, 'Иванов Иван', '2026-10-01', 'В', 0]]))
    assert all(any('Повторная' in error for error in row['errors']) for row in rows)


@pytest.mark.parametrize('column', ['A', 'B', 'C', 'D', 'E'])
def test_formulas_are_never_evaluated_even_with_cached_values(column):
    payload = book()
    root = xml(payload, 'xl/worksheets/sheet1.xml')
    cell = root.find(f"s:sheetData/s:row[@r='2']/s:c[@r='{column}2']", N)
    ET.SubElement(cell, '{' + N['s'] + '}f').text = 'HYPERLINK("https://example.invalid", "click")'
    payload = edit(payload, 'xl/worksheets/sheet1.xml', lambda unused: ET.tostring(root))
    assert any('формулы запрещены' in error for error in parse_timesheet_xlsx(payload)[0]['errors'])


@pytest.mark.parametrize('headers', [HEADERS[:-1], ['ФИО', 'ФИО', 'Дата', 'Код', 'Часы']])
def test_missing_or_duplicate_headers(headers):
    with pytest.raises(ValidationError):
        parse_timesheet_xlsx(book([], headers=headers))


def test_missing_sheet_error_lists_available_sheets():
    with pytest.raises(ValidationError, match='Работники, Пример'):
        parse_timesheet_xlsx(book([], names=['Работники', 'Пример']))


@pytest.mark.parametrize('attack', ['entity', 'external', 'macro', 'traversal', 'duplicate', 'boolean', 'shared_negative'])
def test_untrusted_structures_and_cell_types(attack):
    payload = book()
    if attack == 'entity':
        payload = edit(payload, 'xl/worksheets/sheet1.xml', lambda body: b'<!DOCTYPE x [<!ENTITY a "unsafe">]>' + body)
    elif attack == 'external':
        payload = edit(payload, 'xl/_rels/workbook.xml.rels', lambda body: body.replace(b'Target="styles.xml"', b'Target="https://example.invalid" TargetMode="External"'))
    elif attack in ('macro', 'traversal', 'duplicate'):
        result = BytesIO()
        with ZipFile(BytesIO(payload)) as source, ZipFile(result, 'w', ZIP_DEFLATED) as target:
            for item in source.infolist():
                target.writestr(item.filename, source.read(item.filename))
            with pytest.warns(UserWarning) if attack == 'duplicate' else nullcontext():
                target.writestr({'macro': 'xl/vbaProject.bin', 'traversal': '../outside.xml', 'duplicate': 'xl/styles.xml'}[attack], b'x')
        payload = result.getvalue()
    elif attack == 'boolean':
        payload = edit(payload, 'xl/worksheets/sheet1.xml', lambda body: body.replace(b'<c r="E2" t="n"', b'<c r="E2" t="b"'))
        assert any('Часы:' in error for error in parse_timesheet_xlsx(payload)[0]['errors'])
        return
    else:
        payload = edit(payload, 'xl/worksheets/sheet1.xml', lambda body: body.replace(b'<c r="E2" t="n" s="3"><v>8</v>', b'<c r="E2" t="s"><v>-1</v>'))
    with pytest.raises(ValidationError):
        parse_timesheet_xlsx(payload)


@pytest.mark.parametrize('payload,filename', [(b'not xlsx', 'file.xlsx'), (b'PK\x03\x04broken', 'file.xlsx'), (b'PK\x03\x04', 'file.xls')])
def test_invalid_file(payload, filename):
    with pytest.raises(ValidationError):
        parse_timesheet_xlsx(payload, filename)


def test_upload_limit():
    with pytest.raises(ValidationError, match='5 МиБ'):
        parse_timesheet_xlsx(b'PK\x03\x04' + b'x' * MAX_UPLOAD)


def test_row_limit(monkeypatch):
    import app.services.timesheet_xlsx as module
    monkeypatch.setattr(module, 'MAX_ROWS', 1)
    with pytest.raises(ValidationError, match='20 000'):
        parse_timesheet_xlsx(book([[101, 'Иванов Иван', '2026-10-01', 'Я', 8],
                                    [101, 'Иванов Иван', '2026-10-02', 'Я', 8]]))


@pytest.fixture
def report():
    return dict(timesheet=dict(id=1, project_name='Школа', contractor_name='ООО Строй', year=2026, month=10),
                version=dict(id=7, version_no=2, status='approved', comment='Коррекция', approved_at='2026-11-01T12:00:00Z'),
                days=[date(2026, 10, number).isoformat() for number in range(1, 32)],
                generated_at='2026-11-01T13:00:00Z', workers=[
                    dict(employee_id=101, full_name='Иванов Иван', specialty_name='Штукатур', brigade_number='01',
                         entries={'2026-10-01': dict(code='Я', hours=Decimal('8'), comment=''),
                                  '2026-10-02': dict(code='Я', hours=Decimal('6.5'), comment=''),
                                  '2026-10-03': dict(code='В', hours=Decimal('0'), comment='')},
                         total_hours=Decimal('14.5'), attendance_days=2, absence_days=1),
                    dict(employee_id=102, full_name='Петров Пётр', specialty_name='Маляр', brigade_number='', entries={},
                         total_hours=Decimal('0'), attendance_days=0, absence_days=0),
                ])


def test_export_numeric_hours_dates_context_and_reimport(report):
    payload = export_timesheet_xlsx(report)
    with ZipFile(BytesIO(payload)) as archive:
        assert archive.testzip() is None
    workbook = xml(payload, 'xl/workbook.xml')
    assert [node.get('name') for node in workbook.findall('s:sheets/s:sheet', N)] == ['Месяц', 'Табель', 'Сведения']
    rows = parse_timesheet_xlsx(payload)
    assert len(rows) == 3 and all(not row['errors'] for row in rows)
    assert [row['hours'] for row in rows] == ['8', '6.5', '0']
    detail = xml(payload, 'xl/worksheets/sheet2.xml')
    assert detail.find("s:sheetData/s:row[@r='2']/s:c[@r='C2']", N).get('t') == 'n'
    assert detail.find("s:sheetData/s:row[@r='3']/s:c[@r='E3']/s:v", N).text == '6.5'
    matrix = xml(payload, 'xl/worksheets/sheet1.xml')
    assert 'Школа' in ''.join(matrix.itertext()) and 'ООО Строй' in ''.join(matrix.itertext())


def test_export_freezes_rows_columns_and_marks_weekends_without_attendance(report):
    payload = export_timesheet_xlsx(report)
    matrix = xml(payload, 'xl/worksheets/sheet1.xml')
    pane = matrix.find('s:sheetViews/s:sheetView/s:pane', N)
    assert pane.attrib == dict(xSplit='4', ySplit='2', topLeftCell='E3', activePane='bottomRight', state='frozen')
    styles = xml(payload, 'xl/styles.xml').findall('s:cellXfs/s:xf', N)
    weekend = matrix.find("s:sheetData/s:row[@r='4']/s:c[@r='I4']", N)  # 03 October, empty worker.
    assert styles[int(weekend.get('s'))].get('fillId') == '2'
    assert weekend.find('s:v', N) is None and weekend.find('s:is', N) is None
    # A missing working day also remains absent; no automatic 8-hour entry.
    assert not [row for row in parse_timesheet_xlsx(payload) if row['employee_id'] == 102]
    assert matrix.find('s:autoFilter', N).get('ref').startswith('A2:')


def test_export_leading_zero_brigade_and_literal_user_text(report):
    value = '=HYPERLINK("https://example.invalid")'
    report['workers'][0]['full_name'] = value
    report['timesheet']['project_name'] = '+unsafe'
    payload = export_timesheet_xlsx(report)
    matrix = xml(payload, 'xl/worksheets/sheet1.xml')
    assert matrix.find("s:sheetData/s:row[@r='3']/s:c[@r='D3']/s:is/s:t", N).text == '01'
    assert matrix.find("s:sheetData/s:row[@r='3']/s:c[@r='B3']", N).get('t') == 'inlineStr'
    with ZipFile(BytesIO(payload)) as archive:
        for member in archive.namelist():
            if member.endswith('.xml'):
                assert not ET.fromstring(archive.read(member)).findall('.//s:f', N)
    assert parse_timesheet_xlsx(payload)[0]['full_name'] == value


def test_export_preserves_long_stable_sqlite_id_as_literal_text(report):
    report['workers'][0]['employee_id'] = 9223372036854775807
    payload = export_timesheet_xlsx(report)
    flat = xml(payload, 'xl/worksheets/sheet2.xml')
    assert flat.find("s:sheetData/s:row[@r='2']/s:c[@r='A2']", N).get('t') == 'inlineStr'
    assert parse_timesheet_xlsx(payload)[0]['employee_id'] == 9223372036854775807


@pytest.mark.parametrize('year,month,length', [(2025, 2, 28), (2024, 2, 29), (2026, 4, 30), (2026, 10, 31)])
def test_export_falls_back_to_correct_calendar_month(report, year, month, length):
    report['timesheet'].update(year=year, month=month)
    report.pop('days');report['workers'] = []
    payload = export_timesheet_xlsx(report)
    root = xml(payload, 'xl/worksheets/sheet1.xml')
    columns = root.findall('s:cols/s:col', N)
    assert len(columns) == 4 + 2 * length + 4
    assert parse_timesheet_xlsx(payload) == []
