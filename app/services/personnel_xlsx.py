"""Bounded offline OOXML reader. Never evaluates formulas or follows links."""
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import PurePosixPath
import re
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from app.repositories.base import ValidationError
from app.repositories.personnel_repository import normalized

MAX_UPLOAD=5*1024*1024
MAX_UNPACKED=32*1024*1024
MAX_ROWS=5000
HEADERS=('ФИО','Номер бригады','Специальность')
NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main','r':'http://schemas.openxmlformats.org/package/2006/relationships'}
DOC_REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def _xml(archive, path):
    try:data=archive.read(path)
    except KeyError:raise ValidationError('Повреждён XLSX: отсутствует обязательная часть книги') from None
    if b'\x00' in data or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValidationError('XML с DTD или сущностями не поддерживается')
    try:return ET.fromstring(data)
    except ET.ParseError:raise ValidationError('Повреждён XML внутри XLSX') from None


def _read(payload):
    with ZipFile(BytesIO(payload)) as z:
        entries=z.infolist();names=[i.filename for i in entries]
        if len(entries)>256 or len(names)!=len(set(names)) or sum(i.file_size for i in entries)>MAX_UNPACKED:
            raise ValidationError('Книга превышает допустимый распакованный размер или содержит дубли частей')
        if any(i.flag_bits&1 or i.file_size>16*1024*1024 or i.compress_size and i.file_size/i.compress_size>1000 for i in entries):
            raise ValidationError('Зашифрованный или чрезмерно сжатый XLSX не поддерживается')
        if any('..' in PurePosixPath(n).parts or n.startswith('/') or '\\' in n for n in names):raise ValidationError('Небезопасные пути внутри XLSX')
        if any(n.lower().endswith('.bin') or 'externallinks/' in n.lower() or 'embeddings/' in n.lower() for n in names):
            raise ValidationError('Книга с макросами, вложениями или внешними ссылками не поддерживается')
        types=_xml(z,'[Content_Types].xml')
        if not any(x.attrib.get('ContentType')=='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml' for x in types):
            raise ValidationError('Файл не является обычной книгой XLSX')
        for name in names:
            if name.endswith('.rels'):
                if any(r.attrib.get('TargetMode')=='External' for r in _xml(z,name)):
                    raise ValidationError('Книга содержит внешние ссылки; сохраните копию без них')
        wb=_xml(z,'xl/workbook.xml');sheets=wb.findall('s:sheets/s:sheet',NS)
        target=next((s for s in sheets if s.attrib.get('name')=='Работники'),None)
        if target is None:raise ValidationError('Лист «Работники» не найден. Доступные листы: '+', '.join(s.attrib.get('name','') for s in sheets))
        rels=_xml(z,'xl/_rels/workbook.xml.rels')
        link=next((r for r in rels if r.attrib.get('Id')==target.attrib.get('{'+DOC_REL+'}id') and r.attrib.get('Type')==DOC_REL+'/worksheet'),None)
        if link is None:raise ValidationError('Повреждена ссылка на лист «Работники»')
        path=link.attrib.get('Target','')
        if '..' in PurePosixPath(path).parts:raise ValidationError('Некорректный путь листа')
        path=path.lstrip('/') if path.startswith('/') else 'xl/'+path
        shared=[]
        if 'xl/sharedStrings.xml' in names:
            for s in _xml(z,'xl/sharedStrings.xml').findall('s:si',NS):
                shared.append(''.join(t.text or '' for t in s.findall('.//s:t',NS)))
                if len(shared)>50000:raise ValidationError('Слишком много строк в книге')
        formats={};styles=[];format_ids=[]
        if 'xl/styles.xml' in names:
            root=_xml(z,'xl/styles.xml')
            formats={r.attrib['numFmtId']:r.attrib.get('formatCode','') for r in root.findall('s:numFmts/s:numFmt',NS)}
            styles=[formats.get(r.attrib.get('numFmtId'),'') for r in root.findall('s:cellXfs/s:xf',NS)]
            format_ids=[int(r.attrib.get('numFmtId','0')) for r in root.findall('s:cellXfs/s:xf',NS)]
        root=_xml(z,path)
        rows=root.findall('s:sheetData/s:row',NS)
        if len(rows)>MAX_ROWS+20:raise ValidationError('Допускается не более '+str(MAX_ROWS)+' строк работников')
        parsed=[];seen_rows=set()
        for row in rows:
            index=int(row.attrib.get('r','0'))
            if not 1<=index<=1048576 or index in seen_rows:raise ValidationError('Некорректная нумерация строк XLSX')
            seen_rows.add(index);cells={}
            for c in row.findall('s:c',NS):
                match=re.fullmatch(r'([A-Z]{1,3})([0-9]{1,7})',c.attrib.get('r',''))
                if not match or int(match[2])!=index:raise ValidationError('Некорректный адрес ячейки')
                col=0
                for char in match[1]:col=col*26+ord(char)-64
                if col>100:raise ValidationError('Слишком много колонок; оставьте три колонки шаблона')
                if col in cells:raise ValidationError('Повторяющаяся ячейка XLSX')
                kind=c.attrib.get('t','n');v=c.find('s:v',NS);value=v.text if v is not None and v.text else ''
                if c.find('s:f',NS) is not None:kind,value='formula',''
                elif kind=='inlineStr':value=''.join(t.text or '' for t in c.findall('s:is//s:t',NS))
                elif kind=='s':
                    try:value=shared[int(value)]
                    except (ValueError,IndexError):raise ValidationError('Повреждён справочник строк XLSX') from None
                elif kind=='n' and value:
                    # Integer brigade numbers are common in Excel; respect an
                    # explicit zero mask so 01 is not silently converted to 1.
                    try:
                        numeric=Decimal(value)
                        if not numeric.is_finite() or numeric!=numeric.to_integral_value() or abs(numeric)>10**79:kind='invalid-number'
                        else:
                            value=str(int(numeric));style=int(c.attrib.get('s','0'))
                            mask=styles[style] if 0<=style<len(styles) else ''
                            fmt=format_ids[style] if 0<=style<len(format_ids) else 0
                            bare=re.sub(r'"[^"]*"|\\.', '', mask).lower()
                            if fmt in set(range(14,23))|{45,46,47,9,10} or re.search(r'[ymdhs%]',bare):kind='date-or-percent'
                            elif re.fullmatch(r'0{2,80}',mask) and numeric>=0:value=value.zfill(len(mask))
                    except (InvalidOperation,ValueError):kind='invalid-number'
                cells[col]=(value,kind)
            if any(v or k=='formula' for v,k in cells.values()):parsed.append((index,cells))
        if not parsed:raise ValidationError('Книга пуста: добавьте заголовки на лист «Работники»')
        header_index,header=parsed[0]
        mapping={}
        for col,(value,kind) in header.items():
            key=normalized(value)
            if key in {normalized(h) for h in HEADERS}:
                if key in mapping:raise ValidationError('Заголовок колонки повторяется: '+value)
                if kind not in ('s','inlineStr','str'):raise ValidationError('Заголовки колонок должны быть текстом')
                mapping[key]=col
        missing=[h for h in HEADERS if normalized(h) not in mapping]
        if missing:raise ValidationError('Отсутствуют обязательные колонки: '+', '.join(missing))
        result=[]
        for index,cells in parsed[1:]:
            values=[cells.get(mapping[normalized(h)],('','inlineStr')) for h in HEADERS]
            if not any(v or k=='formula' for v,k in values):continue
            errors=[];cleaned=[]
            for h,(value,kind) in zip(HEADERS,values):
                value=value.strip() if h=='Номер бригады' else ' '.join(value.split())
                if kind=='formula':errors.append(h+': формулы запрещены; вставьте текстовое значение')
                elif kind not in ('s','inlineStr','str') and not (h=='Номер бригады' and kind=='n') and value:
                    errors.append(h+': требуется текстовое значение')
                limit=80 if h=='Номер бригады' else 200
                if len(value)>limit or any(ord(c)<32 for c in value):errors.append(h+': недопустимая длина или управляющие символы')
                cleaned.append(value[:limit+1])
            if not cleaned[0]:errors.append('ФИО: заполните обязательное поле')
            elif len(cleaned[0].split()) not in (2,3):errors.append('ФИО: укажите два или три компонента имени')
            if not cleaned[2]:errors.append('Специальность: заполните обязательное поле')
            result.append(dict(row_number=index,full_name=cleaned[0],brigade_number=cleaned[1],specialty_name=cleaned[2],errors=errors))
            if len(result)>MAX_ROWS:raise ValidationError('Допускается не более '+str(MAX_ROWS)+' строк работников')
        return result


def parse_workers_xlsx(payload, filename='workers.xlsx'):
    if not filename.lower().endswith('.xlsx'):raise ValidationError('Выберите файл в формате .xlsx')
    if not isinstance(payload,bytes) or len(payload)>MAX_UPLOAD:raise ValidationError('Максимальный размер XLSX — 5 МиБ')
    if not payload.startswith(b'PK\x03\x04'):raise ValidationError('Файл не является XLSX (ожидается книга Office Open XML)')
    try:return _read(payload)
    except (BadZipFile,RuntimeError,ValueError,KeyError,OverflowError) as exc:
        if isinstance(exc,ValidationError):raise
        raise ValidationError('Не удалось прочитать XLSX: повреждена структура книги') from None
