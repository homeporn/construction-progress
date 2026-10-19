"""Minimal offline OOXML writer: exact Decimal XML, literal strings, no formulas."""
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
import re
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZipFile, ZIP_DEFLATED

from app.decimal_utils import multiply, exact_sum, ratio
from .analytics_service import deviations

NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PACKAGE='http://schemas.openxmlformats.org/package/2006/relationships'
SHEETS=('01. Сводка','02. Объёмы','03. Динамика','04. Отклонения','05. Аналитические группы','06. Исходные данные')


def column(index):
    result=''
    while index:
        index,rest=divmod(index-1,26);result=chr(65+rest)+result
    return result


def xml(element):
    return tostring(element,encoding='utf-8',xml_declaration=True)


def sheet(headers, rows, percentage_columns=()):
    root=Element('worksheet',xmlns=NS)
    views=SubElement(root,'sheetViews');view=SubElement(views,'sheetView',workbookViewId='0')
    SubElement(view,'pane',ySplit='1',topLeftCell='A2',activePane='bottomLeft',state='frozen')
    cols=SubElement(root,'cols')
    for i,h in enumerate(headers+['Точные значения (>15 цифр; Excel ограничен 15)'],1):
        SubElement(cols,'col',min=str(i),max=str(i),width=str(min(48,max(15,len(h)+2))),customWidth='1')
    data=SubElement(root,'sheetData')
    count=0
    for index,values in enumerate([headers+['Точные значения (>15 цифр; Excel ограничен 15)']]+list(rows),1):
        if index>1048576:raise ValueError('Превышен предел строк листа Excel')
        row=SubElement(data,'row',r=str(index));exact=[]
        for j,value in enumerate(values,1):
            ref=column(j)+str(index);style=1 if index==1 else 0
            if value is None:continue
            if isinstance(value,(Decimal,int)) and not isinstance(value,bool):
                if isinstance(value,Decimal) and not value.is_finite():raise ValueError('Нечисловое значение Excel')
                if index>1:style=4 if j in percentage_columns else 3
                c=SubElement(row,'c',r=ref,t='n',s=str(style));SubElement(c,'v').text=format(value,'f')
                if isinstance(value,Decimal) and len(value.as_tuple().digits)>15:exact.append(ref+'='+format(value,'f'))
            elif isinstance(value,datetime):
                value=value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
                delta=value-datetime(1899,12,30)
                serial=exact_sum([Decimal(delta.days-(1 if value.date()<date(1900,3,1) else 0)),ratio(Decimal(delta.seconds*1000000+delta.microseconds),Decimal(86400000000))])
                c=SubElement(row,'c',r=ref,t='n',s='5');SubElement(c,'v').text=format(serial,'f')
                exact.append(ref+'='+value.isoformat(timespec='microseconds')+'Z')
            elif isinstance(value,date):
                serial=(value-date(1899,12,30)).days-(1 if value<date(1900,3,1) else 0)
                c=SubElement(row,'c',r=ref,t='n',s='2');SubElement(c,'v').text=str(serial)
            else:
                # inlineStr never evaluates =, +, -, @ or a leading tab as a formula.
                c=SubElement(row,'c',r=ref,t='inlineStr',s=str(style));text=SubElement(SubElement(c,'is'),'t')
                text.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
                value=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','�',str(value))
                if len(value)>32767:raise ValueError('Текст превышает лимит ячейки Excel 32767 символов. Сократите выборку отчёта')
                text.text=value
        if index>1 and exact:
            c=SubElement(row,'c',r=column(len(headers)+1)+str(index),t='inlineStr')
            SubElement(SubElement(c,'is'),'t').text='; '.join(exact)
        count=index
    SubElement(root,'autoFilter',ref='A1:'+column(len(headers)+1)+str(max(1,count)))
    return xml(root)


def styles():
    root=Element('styleSheet',xmlns=NS)
    formats=SubElement(root,'numFmts',count='4')
    for i,code in [(164,'yyyy-mm-dd'),(165,'0.#########'),(166,'0.0%'),(167,'yyyy-mm-dd hh:mm:ss')]:SubElement(formats,'numFmt',numFmtId=str(i),formatCode=code)
    fonts=SubElement(root,'fonts',count='2')
    for bold in (False,True):
        f=SubElement(fonts,'font');SubElement(f,'sz',val='11');SubElement(f,'name',val='Calibri')
        if bold:SubElement(f,'b')
    fills=SubElement(root,'fills',count='2')
    for kind in ('none','gray125'):SubElement(SubElement(fills,'fill'),'patternFill',patternType=kind)
    borders=SubElement(root,'borders',count='1');SubElement(borders,'border')
    bases=SubElement(root,'cellStyleXfs',count='1');SubElement(bases,'xf',numFmtId='0',fontId='0',fillId='0',borderId='0')
    cells=SubElement(root,'cellXfs',count='6')
    for i,fmt in enumerate((0,0,164,165,166,167)):
        xf=SubElement(cells,'xf',numFmtId=str(fmt),fontId='1' if i==1 else '0',fillId='0',borderId='0',xfId='0',applyNumberFormat='1')
        if i==1:SubElement(xf,'alignment',wrapText='1',vertical='top')
    cs=SubElement(root,'cellStyles',count='1');SubElement(cs,'cellStyle',name='Normal',xfId='0',builtinId='0')
    return xml(root)


def export_xlsx(data, deviation_mode='all'):
    def d(value):return date.fromisoformat(value)
    meta_headers=['Объект','Направление','Версия плана (ID)','Дата среза','Отсечение знаний (UTC, точно)','Начало периода','Конец периода','Фильтры','Отсечение знаний (дата UTC)']
    import json
    meta=[data['project']['name'],'СМР' if data['discipline']=='CONSTRUCTION' else 'Проектирование',
          data['version']['id'] if data['version'] else None,d(data['report_date']),data['cutoff'],d(data['start_date']),d(data['end_date']),
          json.dumps(dict(data['filters'],group_id=data['group_id'],group_version=data['group']['version_no'] if data['group'] else None,bucket=data['bucket'],deviation=deviation_mode),ensure_ascii=False),datetime.fromisoformat(data['cutoff'].replace('Z','+00:00'))]
    def combine(headers,rows,pct=()):return sheet(headers+meta_headers,[list(r)+meta for r in rows],pct)
    def quantities(r):return [r[k] for k in ('planned','actual','confirmed','remaining','pending','actual_ratio','confirmed_ratio')]
    qhead=['План','Факт','Подтверждено','Остаток','Ожидает подтверждения','Выполнение','Подтверждение']
    summaries=[['Выборка',data['total'],data['item_count'],data['gap']]]+[[b['name']+(' · архив' if b['status']=='archived' else ''),b['progress'],b['count'],b['gap']] for b in data['buildings']]
    bodies=[]
    bodies.append(combine(['Область','Физическая готовность','Подтверждённая готовность','Разница, п.п.','Позиций','Позиций с фактом','Ожидающих позиций','Корпусов','Последняя регистрация (UTC)','Статус'],
        [[name,p.actual_progress,p.confirmed_progress,multiply(gap,Decimal(100)) if gap is not None else None,n,data['actual_count'] if name=='Выборка' else None,data['pending_count'] if name=='Выборка' else None,data['building_count'],datetime.fromisoformat(data['latest_event'].replace('Z','+00:00')) if data['latest_event'] else None,p.status.value] for name,p,n,gap in summaries],(2,3)))
    bodies.append(combine(['Код','Вид работ','Ед.']+qhead,[[r['code'],r['name'],r['unit']]+quantities(r) for r in data['automatic']],(9,10)))
    bodies.append(combine(['Начало интервала','Конец интервала','Код','Ед.','Факт за период','Подтверждение за период','Накопительный факт','Накопительное подтверждение'],
        [[d(p['start']),d(p['date']),data['series']['code'],data['series']['unit'],p['actual_period'],p['confirmed_period'],p['actual'],p['confirmed']] for p in data['series']['points']]))
    bodies.append(combine(['Позиция','Корпус','Этаж','Конструктив','Код','Вид работ','Ед.']+qhead,
        [[r['id'],r['building_name'],r['floor_code'],r['element'],r['code'],r['name'],r['unit']]+quantities(r) for r in deviations(data['rows'],deviation_mode)],(13,14)))
    bodies.append(combine(['Группа (ID)','Название','Версия состава','Изменено (UTC)','Архив','Код','Ед.','Позиции (ID)','Предупреждения']+qhead,
        [[g['group_id'],g['name'],g['version_no'],datetime.fromisoformat(g['changed_at'].replace('Z','+00:00')),'Да' if g['archived'] else 'Нет',g['code'],g['unit'],', '.join(str(r['id']) for r in g['rows']),'; '.join(g['warnings'])]+quantities(g) for g in data['group_views']],(15,16)))
    bodies.append(combine(['Позиция','Корпус','Этаж','Конструктив','Код','Наименование','Ед.','Норматив, чел.-ч/ед.','Плановая трудоёмкость, чел.-ч','В плане']+qhead,
        [[r['id'],r['building_name'],r['floor_code'],r['element'],r['code'],r['name'],r['unit'],r['labor_norm'],r['result'].planned_labor,'Да' if r['in_plan'] else 'Нет']+quantities(r) for r in data['rows']],(16,17)))
    output=BytesIO()
    with ZipFile(output,'w',ZIP_DEFLATED) as archive:
        types=Element('Types',xmlns='http://schemas.openxmlformats.org/package/2006/content-types')
        SubElement(types,'Default',Extension='rels',ContentType='application/vnd.openxmlformats-package.relationships+xml')
        SubElement(types,'Default',Extension='xml',ContentType='application/xml')
        for part,kind in [('workbook.xml','sheet.main'),('styles.xml','styles')]+[('worksheets/sheet'+str(i)+'.xml','worksheet') for i in range(1,7)]:
            SubElement(types,'Override',PartName='/xl/'+part,ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.'+kind+'+xml')
        archive.writestr('[Content_Types].xml',xml(types))
        rels=Element('Relationships',xmlns=PACKAGE);SubElement(rels,'Relationship',Id='rId1',Type=REL+'/officeDocument',Target='xl/workbook.xml');archive.writestr('_rels/.rels',xml(rels))
        wb=Element('workbook',xmlns=NS);sheets=SubElement(wb,'sheets');links=Element('Relationships',xmlns=PACKAGE)
        for i,(name,body) in enumerate(zip(SHEETS,bodies),1):
            SubElement(sheets,'sheet',name=name,sheetId=str(i),attrib={'{'+REL+'}id':'rId'+str(i)})
            SubElement(links,'Relationship',Id='rId'+str(i),Type=REL+'/worksheet',Target='worksheets/sheet'+str(i)+'.xml')
            archive.writestr('xl/worksheets/sheet'+str(i)+'.xml',body)
        SubElement(links,'Relationship',Id='rId7',Type=REL+'/styles',Target='styles.xml')
        archive.writestr('xl/workbook.xml',xml(wb));archive.writestr('xl/_rels/workbook.xml.rels',xml(links));archive.writestr('xl/styles.xml',styles())
    return output.getvalue()
