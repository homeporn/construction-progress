"""Offline, untrusted-XLSX boundary for monthly timesheets; no database writes.

``parse_timesheet_xlsx(bytes, filename)`` reads only ``Табель`` and returns rows
with ``row_number, employee_id, full_name, work_date`` (ISO), ``code, hours``
(canonical Decimal text or None), and ``errors``. Structural failures raise
ValidationError; employee/project/code validation belongs to the repository.
Limits: 5 MiB uploaded, 32 MiB unpacked, 20,000 data rows, 256 ZIP members.
Neither cached formula results nor external workbook resources are evaluated.

``export_timesheet_xlsx(report)`` accepts timesheet/project_name/contractor_name/
year/month/id, version/id/version_no/status/comment/approved_at, days (ISO),
workers (employee_id/full_name/specialty_name/brigade_number/entries keyed by
date/total_hours/attendance_days/absence_days/note), and generated_at (UTC ISO).
Each entry has code, hours, comment. Numbers are emitted as numeric OOXML;
strings are literal inlineStr. Excel's usual 15-significant-digit limit applies.
"""
from calendar import monthrange
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import PurePosixPath
import re
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED
from zlib import error as CompressionError

from app.decimal_utils import canonical, decimal_value
from app.repositories.base import ValidationError
from .personnel_xlsx import _xml
from .xlsx_export import column, pack_workbook, sheet

MAX_UPLOAD = 5 * 1024 * 1024
MAX_UNPACKED = 32 * 1024 * 1024
MAX_ROWS = 20000
HEADERS = ('ID работника', 'ФИО', 'Дата', 'Код', 'Часы')
TEMPLATE_NAME = 'Construction_Progress_4_2_Шаблон_табеля.xlsx'
NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
N = {'s': NS}
Q = '{' + NS + '}'
XML_SPACE = '{http://www.w3.org/XML/1998/namespace}space'
BAD_TEXT = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def _normalized(value):
    return ' '.join(value.split()).casefold()


def _validated_archive(archive):
    items = archive.infolist()
    if len(items) > 256 or len({i.filename for i in items}) != len(items):
        raise ValidationError('Некорректная структура XLSX: слишком много или повторяются части файла')
    if sum(i.file_size for i in items) > MAX_UNPACKED:
        raise ValidationError('Распакованный XLSX превышает 32 МиБ')
    for item in items:
        path = PurePosixPath(item.filename)
        lower = item.filename.lower()
        if path.is_absolute() or '..' in path.parts or '\\' in item.filename:
            raise ValidationError('Недопустимый путь внутри XLSX')
        if item.flag_bits & 1 or item.file_size > 16 * 1024 * 1024:
            raise ValidationError('Зашифрованные или слишком большие части XLSX не поддерживаются')
        if item.file_size > max(1, item.compress_size) * 1000:
            raise ValidationError('Слишком высокая степень сжатия XLSX')
        if lower.endswith('.bin') or '/externallinks/' in lower or '/embeddings/' in lower:
            raise ValidationError('Макросы, вложения и внешние ссылки XLSX запрещены')
    types = _xml(archive, '[Content_Types].xml')
    mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'
    if not any(node.get('ContentType') == mime for node in types):
        raise ValidationError('Ожидается обычная книга Excel .xlsx без макросов')
    for item in items:
        if item.filename.endswith('.rels'):
            if any(node.get('TargetMode', '').casefold() == 'external'
                   for node in _xml(archive, item.filename)):
                raise ValidationError('Внешние ссылки XLSX запрещены')


def _worksheet(archive):
    workbook = _xml(archive, 'xl/workbook.xml')
    worksheets = workbook.findall('s:sheets/s:sheet', N)
    selected = [node for node in worksheets if node.get('name') == 'Табель']
    if len(selected) != 1:
        names = ', '.join(node.get('name', '') for node in worksheets) or 'нет листов'
        raise ValidationError('Нужен один лист «Табель». Доступные листы: ' + names)
    identifier = selected[0].get('{' + REL + '}id')
    relationships = _xml(archive, 'xl/_rels/workbook.xml.rels')
    links = [node for node in relationships
             if node.get('Id') == identifier and node.get('Type') == REL + '/worksheet']
    if len(links) != 1:
        raise ValidationError('Не найдена корректная связь листа «Табель»')
    target = links[0].get('Target', '')
    if not target or '..' in PurePosixPath(target).parts or '\\' in target:
        raise ValidationError('Некорректный путь листа «Табель»')
    path = target.lstrip('/') if target.startswith('/') else 'xl/' + target
    properties = workbook.find('s:workbookPr', N)
    date1904 = properties is not None and properties.get('date1904', '').lower() in ('1', 'true')
    return _xml(archive, path), date1904


def _strings_and_styles(archive):
    strings, styles, formats = [], [], {}
    if 'xl/sharedStrings.xml' in archive.namelist():
        shared = _xml(archive, 'xl/sharedStrings.xml')
        strings = [''.join(text.text or '' for text in node.findall('.//s:t', N))
                   for node in shared.findall('s:si', N)]
        if len(strings) > 50000 or any(len(text) > 32767 for text in strings):
            raise ValidationError('Превышен предел строковых значений XLSX')
    if 'xl/styles.xml' in archive.namelist():
        style_xml = _xml(archive, 'xl/styles.xml')
        formats = {int(node.get('numFmtId')): node.get('formatCode', '')
                   for node in style_xml.findall('s:numFmts/s:numFmt', N)}
        styles = [int(node.get('numFmtId', '0'))
                  for node in style_xml.findall('s:cellXfs/s:xf', N)]
    return strings, styles, formats


def _date_style(index, styles, formats):
    if index < 0 or index >= len(styles):
        return False
    number_format = styles[index]
    if number_format in set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59)):
        return True
    mask = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', '', formats.get(number_format, '')).lower()
    return bool(re.search(r'[yd]', mask))


def _cell_value(node, strings, styles, formats):
    if node.find('s:f', N) is not None:
        return ('formula', '', False)
    kind = node.get('t', 'n')
    raw = node.findtext('s:v', '', N)
    style = int(node.get('s', '0'))
    if kind == 'inlineStr':
        value = ''.join(text.text or '' for text in node.findall('s:is//s:t', N))
        return ('text', value, False)
    if kind == 's':
        index = int(raw)
        if index < 0 or index >= len(strings):
            raise ValidationError('Некорректная ссылка на строковое значение XLSX')
        return ('text', strings[index], False)
    if kind == 'str':
        return ('text', raw, False)
    if kind in ('n', 'd'):
        return (kind, raw, _date_style(style, styles, formats))
    return ('invalid', raw, False)


def _rows(archive, worksheet):
    strings, styles, formats = _strings_and_styles(archive)
    result, used_rows = [], set()
    source = worksheet.findall('s:sheetData/s:row', N)
    if len(source) > MAX_ROWS + 20:
        raise ValidationError('В табеле допускается не более 20 000 строк')
    for row in source:
        number = int(row.get('r', '0'))
        if not 1 <= number <= 1048576 or number in used_rows:
            raise ValidationError('Некорректный или повторный номер строки XLSX')
        used_rows.add(number)
        values = {}
        for cell in row.findall('s:c', N):
            match = re.fullmatch(r'([A-Z]{1,3})([0-9]{1,7})', cell.get('r', ''))
            if not match or int(match[2]) != number:
                raise ValidationError('Некорректный адрес ячейки XLSX')
            index = 0
            for character in match[1]:
                index = index * 26 + ord(character) - 64
            if index > 100 or index in values:
                raise ValidationError('Слишком много или повторяются колонки XLSX')
            values[index] = _cell_value(cell, strings, styles, formats)
        if any(value[1] != '' or value[0] == 'formula' for value in values.values()):
            result.append((number, values))
    return result


def _date_value(cell, date1904):
    kind, value, is_date = cell
    if kind in ('text', 'd'):
        value = value.strip()
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
            raise ValueError('Ожидается YYYY-MM-DD')
        return date.fromisoformat(value).isoformat()
    if kind == 'n' and is_date:
        serial = Decimal(value)
        if not serial.is_finite() or serial != serial.to_integral_value() or not 0 <= serial <= 3000000:
            raise ValueError('Некорректная дата')
        days = int(serial)
        if date1904:
            return (date(1904, 1, 1) + timedelta(days=days)).isoformat()
        if days == 60:
            raise ValueError('Excel 1900-02-29 не является календарной датой')
        return (date(1899, 12, 31) + timedelta(days=days - (1 if days > 60 else 0))).isoformat()
    raise ValueError('Ожидается дата ISO или ячейка даты Excel')


def _parse_record(number, cells, columns, date1904):
    record = dict(row_number=number, employee_id=None, full_name='', work_date='', code='', hours=None, errors=[])
    errors = record['errors']
    selected = {key: cells.get(index, ('text', '', False)) for key, index in columns.items()}
    for key, cell in selected.items():
        if cell[0] == 'formula':
            errors.append(key + ': формулы запрещены')
        elif cell[0] == 'invalid':
            errors.append(key + ': недопустимый тип ячейки')
        if BAD_TEXT.search(cell[1]) or len(cell[1]) > 32767:
            errors.append(key + ': недопустимое текстовое значение')
    try:
        employee = selected['ID работника']
        value = Decimal(employee[1].strip())
        if employee[0] not in ('text', 'n') or employee[2] or not value.is_finite() or value != value.to_integral_value() or not 0 < value <= 9223372036854775807:
            raise ValueError('Некорректный ID')
        record['employee_id'] = int(value)
    except (ValueError, InvalidOperation):
        errors.append('ID работника: нужен положительный внутренний ID существующего работника')
    name = selected['ФИО']
    record['full_name'] = ' '.join(name[1].split())
    if name[0] != 'text' or not record['full_name'] or len(record['full_name']) > 200:
        errors.append('ФИО: требуется текст до 200 символов; идентификация выполняется по ID')
    try:
        record['work_date'] = _date_value(selected['Дата'], date1904)
    except (ValueError, InvalidOperation, OverflowError):
        errors.append('Дата: укажите календарную дату YYYY-MM-DD или дату Excel без времени')
    code = selected['Код']
    record['code'] = code[1].strip().upper()
    if code[0] != 'text' or not record['code'] or len(record['code']) > 20:
        errors.append('Код: требуется текстовое табельное обозначение до 20 символов')
    hours = selected['Часы']
    if hours[1] != '' and hours[0] in ('n', 'text') and not hours[2]:
        try:
            value = decimal_value(hours[1].strip().replace(',', '.'))
            if not 0 <= value <= 24:
                errors.append('Часы: допустимо от 0 до 24 часов в сутки')
            else:
                # Bound the scale before fixed-point formatting: an untrusted
                # 1E-999999999 cell must never allocate a gigabyte of zeros.
                digits, exponent = value.as_tuple().digits, value.as_tuple().exponent
                trailing = 0
                for digit in reversed(digits):
                    if digit:
                        break
                    trailing += 1
                if value and exponent + trailing < -9:
                    raise ValueError('Точность часов превышает 9 знаков')
                record['hours'] = '0' if not value else canonical(value)
        except (ValueError, ArithmeticError):
            errors.append('Часы: требуется точное число с не более чем 9 знаками после запятой')
    elif hours[1] != '' or hours[0] not in ('text', 'n'):
        errors.append('Часы: требуется число, а не дата, формула или логическое значение')
    return record


def parse_timesheet_xlsx(payload, filename='timesheet.xlsx'):
    """Read data only; row errors are returned, structural errors are raised."""
    if not isinstance(filename, str) or not filename.lower().endswith('.xlsx'):
        raise ValidationError('Поддерживается только формат .xlsx')
    if not isinstance(payload, bytes) or len(payload) > MAX_UPLOAD:
        raise ValidationError('Размер XLSX не должен превышать 5 МиБ')
    if not payload.startswith(b'PK\x03\x04'):
        raise ValidationError('Файл не является корректной книгой XLSX')
    try:
        with ZipFile(BytesIO(payload)) as archive:
            _validated_archive(archive)
            worksheet, date1904 = _worksheet(archive)
            rows = _rows(archive, worksheet)
        if not rows:
            raise ValidationError('На листе «Табель» отсутствуют заголовки')
        columns = {}
        lookup = {_normalized(header): header for header in HEADERS}
        for index, cell in rows[0][1].items():
            key = lookup.get(_normalized(cell[1]))
            if key:
                if key in columns or cell[0] != 'text':
                    raise ValidationError('Заголовки табеля должны быть текстовыми и не повторяться')
                columns[key] = index
        missing = [header for header in HEADERS if header not in columns]
        if missing:
            raise ValidationError('Отсутствуют обязательные колонки: ' + ', '.join(missing))
        if len(rows) - 1 > MAX_ROWS:
            raise ValidationError('В табеле допускается не более 20 000 строк')
        records = [_parse_record(number, values, columns, date1904) for number, values in rows[1:]]
        duplicates = {}
        for record in records:
            key = record['employee_id'], record['work_date']
            if all(key):
                duplicates.setdefault(key, []).append(record)
        for group in duplicates.values():
            if len(group) > 1:
                for record in group:
                    record['errors'].append('Повторная запись работника на одну дату в файле')
        return records
    except (BadZipFile, CompressionError, RuntimeError, ValueError, ArithmeticError, KeyError, OverflowError, TypeError, AttributeError) as error:
        if isinstance(error, ValidationError):
            raise
        raise ValidationError('Некорректная структура книги XLSX') from error


def make_timesheet_template():
    """Empty import sheet; demonstration sheets are never imported."""
    instructions = [
        ['Заполняйте только лист «Табель». «Пример» и «Инструкция» не импортируются.'],
        ['Объект, подрядчика и месяц выбирают в приложении. Импорт доступен только в черновик.'],
        ['ID работника — постоянный ID из справочника. Импорт не создаёт работников и не сопоставляет их по ФИО.'],
        ['Сохраняйте все пять заголовков; порядок колонок можно изменить. Дата: YYYY-MM-DD либо дата Excel.'],
        ['Я — явка; В — выходной; ОТ — отпуск; Б — больничный; Н — невыясненная неявка.'],
        ['Часы вводят числом от 0 до 24; дробные часы допустимы. Для отсутствий часы равны 0.'],
        ['Незаполненная строка не означает неявку. Никакая явка и никакие часы не начисляются автоматически.'],
        ['Не используйте формулы, макросы или внешние ссылки. Предел: 5 МиБ и 20 000 записей.'],
        ['Перед записью проверьте предварительный просмотр. Утверждённые версии не изменяются.'],
        ['Пример содержит вымышленные имена и условные ID. Замените их реальными ID своего справочника.'],
    ]
    example = [[101, 'Работник Демонстрационный', date(2026, 10, 1), 'Я', Decimal('8')],
               [101, 'Работник Демонстрационный', date(2026, 10, 2), 'В', Decimal('0')]]
    return pack_workbook(['Табель', 'Пример', 'Инструкция'], [
        sheet(list(HEADERS), [], exact_column=False),
        sheet(list(HEADERS), example, exact_column=False),
        sheet(['Инструкция'], instructions, exact_column=False),
    ])


def _entry(worker, day):
    return worker.get('entries', {}).get(day)


def _freeze_and_style(payload, changes):
    """Extend the common writer's XML without changing its existing API/styles."""
    output = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(output, 'w', ZIP_DEFLATED) as target:
        styles = ET.fromstring(source.read('xl/styles.xml'))
        fills, formats = styles.find(Q + 'fills'), styles.find(Q + 'cellXfs')
        fill_id = len(fills)
        fill = ET.SubElement(fills, Q + 'fill')
        pattern = ET.SubElement(fill, Q + 'patternFill', patternType='solid')
        ET.SubElement(pattern, Q + 'fgColor', rgb='FFEAF0F6')
        ET.SubElement(pattern, Q + 'bgColor', indexed='64')
        fills.set('count', str(len(fills)))
        weekend_styles = {}
        for index in (0, 1, 2, 3):
            weekend_styles[index] = len(formats)
            style = deepcopy(formats[index]);style.set('fillId', str(fill_id));style.set('applyFill', '1')
            formats.append(style)
        formats.set('count', str(len(formats)))
        for member in source.infolist():
            body = source.read(member.filename)
            if member.filename == 'xl/styles.xml':
                body = ET.tostring(styles, encoding='utf-8', xml_declaration=True)
            elif member.filename in changes:
                root = ET.fromstring(body)
                changes[member.filename](root, weekend_styles)
                body = ET.tostring(root, encoding='utf-8', xml_declaration=True)
            target.writestr(member.filename, body)
    return output.getvalue()


def export_timesheet_xlsx(report):
    """Export a monthly matrix and a flat, re-importable data sheet locally."""
    timesheet, version = report['timesheet'], report['version']
    year, month = int(timesheet['year']), int(timesheet['month'])
    days = report.get('days') or [date(year, month, day).isoformat()
                                 for day in range(1, monthrange(year, month)[1] + 1)]
    dates = [date.fromisoformat(day) for day in days]
    workers = report['workers']

    def identifier(employee_id):
        # IDs are identifiers, not quantities. Excel rounds numeric IDs after
        # 15 significant digits; preserve larger stable SQLite IDs as text.
        return str(employee_id) if len(str(employee_id)) > 15 else employee_id

    headers = ['ID работника', 'ФИО', 'Специальность', 'Бригада']
    for day in dates:
        headers.extend([day.strftime('%d.%m') + ' · код', day.strftime('%d.%m') + ' · часы'])
    headers += ['Итого часов', 'Дней явки', 'Дней отсутствия', 'Примечание']
    matrix, records = [], []
    for worker in workers:
        row = [identifier(worker['employee_id']), worker['full_name'], worker.get('specialty_name', ''),
               worker.get('brigade_number', '')]
        for day in days:
            entry = _entry(worker, day)
            row.extend([entry['code'], decimal_value(entry['hours'])] if entry else [None, None])
            if entry:
                records.append([identifier(worker['employee_id']), worker['full_name'], date.fromisoformat(day),
                                entry['code'], decimal_value(entry['hours'])])
        row.extend([decimal_value(worker.get('total_hours', '0')), worker.get('attendance_days', 0),
                    worker.get('absence_days', 0), worker.get('note', worker.get('comment', ''))])
        matrix.append(row)
    statuses = {'draft': 'Черновик', 'submitted': 'Передан на проверку', 'approved': 'Утверждён', 'cancelled': 'Отменён'}
    generated = report.get('generated_at') or datetime.now(timezone.utc).isoformat()
    generated = datetime.fromisoformat(generated.replace('Z', '+00:00')) if isinstance(generated, str) else generated
    metadata = [['Объект', timesheet['project_name']], ['Подрядчик', timesheet['contractor_name']],
                ['Период', date(year, month, 1)], ['ID табеля', timesheet['id']],
                ['Версия', version['version_no']], ['ID версии', version['id']],
                ['Статус', statuses.get(version['status'], version['status'])],
                ['Утверждён (UTC)', version.get('approved_at') or ''],
                ['Комментарий', version.get('comment') or ''], ['Сформирован (UTC)', generated],
                ['Календарные выходные', 'Подсветка суббот и воскресений не означает неявку и не назначает часы.'],
                ['Пустые ячейки', 'Запись отсутствует. Это отличается от нулевых часов и от неявки.'],
                ['Импорт', 'Лист «Табель» содержит структурированные данные с ID; лист «Месяц» предназначен для просмотра.'],
                ['Учёт', 'Состав бригады и наряды не используются для автоматического начисления часов.']]
    context = f"{timesheet['project_name']} · {timesheet['contractor_name']} · {month:02d}.{year} · V{version['version_no']} · {statuses.get(version['status'], version['status'])}"
    matrix_body = sheet(headers, matrix, exact_column=False, text_columns=(4,))
    flat_body = sheet(list(HEADERS), records, exact_column=False)
    metadata_body = sheet(['Параметр', 'Значение'], metadata, exact_column=False)
    payload = pack_workbook(['Месяц', 'Табель', 'Сведения'], [matrix_body, flat_body, metadata_body])

    def monthly(root, weekend_styles):
        pane = root.find('s:sheetViews/s:sheetView/s:pane', N)
        # Move the generated header/data down one row to display the context.
        data = root.find('s:sheetData', N)
        for row in data:
            index = int(row.get('r')) + 1;row.set('r', str(index))
            for cell in row:
                cell.set('r', re.sub(r'[0-9]+$', str(index), cell.get('r')))
        title = ET.Element(Q + 'row', r='1')
        cell = ET.SubElement(title, Q + 'c', r='A1', t='inlineStr', s='1')
        text = ET.SubElement(ET.SubElement(cell, Q + 'is'), Q + 't');text.set(XML_SPACE, 'preserve')
        text.text = BAD_TEXT.sub('�', context)
        data.insert(0, title)
        pane.set('xSplit', '4');pane.set('ySplit', '2');pane.set('topLeftCell', 'E3');pane.set('activePane', 'bottomRight')
        autofilter = root.find('s:autoFilter', N)
        autofilter.set('ref', 'A2:' + column(len(headers)) + str(max(2, len(matrix) + 2)))
        cols = root.find('s:cols', N)
        for index, col in enumerate(cols, 1):
            col.set('width', '34' if index == 2 else '24' if index == 3 else '12' if index in (1, 4) else '10')
        weekend_columns = {5 + 2 * index + offset for index, day in enumerate(dates)
                           if day.weekday() >= 5 for offset in (0, 1)}
        for row in data:
            index = int(row.get('r'))
            if index < 2:
                continue
            existing = {re.sub(r'[0-9]', '', cell.get('r')): cell for cell in row}
            # Emit styled empty weekend cells, preserving missing entries.
            for colindex in weekend_columns:
                ref = column(colindex)
                cell = existing.get(ref)
                if cell is None:
                    cell = ET.SubElement(row, Q + 'c', r=ref + str(index), s='0')
                cell.set('s', str(weekend_styles.get(int(cell.get('s', '0')), weekend_styles[0])))
            row[:] = sorted(row, key=lambda cell: (len(re.sub(r'[0-9]', '', cell.get('r'))), re.sub(r'[0-9]', '', cell.get('r'))))
        ET.SubElement(root, Q + 'pageMargins', left='0.25', right='0.25', top='0.5', bottom='0.5', header='0.2', footer='0.2')
        ET.SubElement(root, Q + 'pageSetup', paperSize='9', orientation='landscape', fitToWidth='1', fitToHeight='0')

    def flat(root, unused):
        pane = root.find('s:sheetViews/s:sheetView/s:pane', N)
        pane.set('xSplit', '2');pane.set('topLeftCell', 'C2');pane.set('activePane', 'bottomRight')

    return _freeze_and_style(payload, {'xl/worksheets/sheet1.xml': monthly,
                                       'xl/worksheets/sheet2.xml': flat})
