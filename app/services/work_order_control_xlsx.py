"""Seven-sheet offline work-order control export using the existing OOXML writer."""
from datetime import date, datetime
import json
import hashlib
from decimal import Decimal

from app.services.xlsx_export import pack_workbook, sheet, column
from app.work_order_models import STATUS_LABELS

SHEETS=('01. Реестр','02. Контроль','03. Остатки','04. Переносы','05. Цепочки','06. Расхождения','07. Источники')


def export_xlsx(data):
    meta_headers=['Объект ID','Объект','Дата среза','Отсечение знаний UTC','Отсечение знаний UTC (точно)','С даты смены','По дату смены','Фильтры','Правила расчёта']
    meta=[data.get('project',{}).get('id'),data.get('project',{}).get('name',''),date.fromisoformat(data['report_date']),
          datetime.fromisoformat(data['cutoff'].replace('Z','+00:00')),data['cutoff'],
          date.fromisoformat(data['filters']['date_from']) if data['filters'].get('date_from') else None,
          date.fromisoformat(data['filters']['date_to']) if data['filters'].get('date_to') else None,json.dumps(data['filters'],ensure_ascii=False,sort_keys=True),
          json.dumps(data['policy'],ensure_ascii=False,sort_keys=True)]
    text_chunks=[]
    sheet_index=0
    def body(headers,rows,*,include_text_chunks=False):
        nonlocal sheet_index
        sheet_index+=1
        # Preserve report metadata even for an empty selection. All user text is
        # inlineStr in the shared writer, including strings beginning with '='.
        records=[]
        for row_index,row in enumerate(rows,2):
            record=list(row)+meta
            for column_index,value in enumerate(record,1):
                if not isinstance(value,str) or len(value.encode('utf-16-le'))//2<=32767:
                    continue
                digest=hashlib.sha256(value.encode('utf-8')).hexdigest()
                pieces=[value[offset:offset+15000] for offset in range(0,len(value),15000)]
                reference=SHEETS[sheet_index-1]+'!'+column(column_index)+str(row_index)
                for part,piece in enumerate(pieces,1):
                    text_chunks.append(['Текстовая часть',None,None,None,None,None,None,None,None,None,None,None,
                        None,reference,part,len(pieces),digest,piece])
                record[column_index-1]='Полный текст: лист 07. Источники; '+reference+'; SHA256 '+digest+'; частей '+str(len(pieces))
            records.append(record)
        if include_text_chunks:
            records.extend(row+meta for row in text_chunks)
        return sheet(headers+meta_headers,records or [[None]*len(headers)+meta])
    def order_name(row):
        return row.get('order_number') or 'Черновик · '+str(row.get('order_id',row.get('id')))
    def quantities(row):
        return [row.get(key) for key in ('assignment','actual','confirmed','execution_remaining','confirmed_remaining','transferred','reserved','sent','written_off','available','pending_acceptance','open_quantity','unresolved')]
    qhead=['Задание','Факт по наряду','Подтверждение по наряду','Остаток выполнения','Остаток подтверждения','Перенесено','Резерв в черновиках','Выдано получателю','Списано','Доступно к переносу','Ожидает приёмки','Открыто','Незакрыто']
    line_headers=['Наряд ID','Наряд','Смена','Статус','Подрядчик','Бригады (общий состав)','Строка ID','Код','Работа','Ед.']+qhead+['Сопоставление','Состояние','Конфликты']
    def line_row(r):
        return [r['order_id'],order_name(r),date.fromisoformat(r['shift_date']),STATUS_LABELS[r['status']],r['contractor_name'],', '.join(r['brigades']),r['line_id'],r['code'],r['name'],r['unit']]+quantities(r)+['Требуется сопоставление' if r['matching_required'] else 'Сопоставлено' if r.get('matched_quantity',0)>0 else 'Не требуется / нет сопоставлений',r['state'],'; '.join(r['conflicts'])]
    registry=[]
    for r in data['registry']:
        for v in r['volumes'] or [{}]:
            registry.append([r['id'],order_name(r),date.fromisoformat(r['shift_date']),STATUS_LABELS[r['status']],r['contractor_name'],', '.join(r['brigades']),r['buildings'],r['line_count'],v.get('code'),v.get('unit')]+quantities(v))
    bodies=[body(['Наряд ID','Наряд','Смена','Статус','Подрядчик','Бригады','Корпуса','Строк','Код','Ед.']+qhead,registry)]
    bodies.append(body(line_headers,[line_row(r) for r in data['rows']]))
    bodies.append(body(line_headers,[line_row(r) for r in data['rows'] if r['status']!='cancelled' and r['unresolved']>0]))
    bodies.append(body(['Перенос ID','Исходный наряд ID','Исходный наряд','Новый наряд ID','Новый наряд','Исходная строка ID','Новая строка ID','Количество','Ед.','Дата новой смены','Записан UTC','Активен','Основание','Комментарий','Исходный исполнитель (снимок)','Новый исполнитель (снимок)','Отмена UTC','Основание отмены'],[
        [t['id'],t['source_order_id'],t['source_order_number'],t['target_order_id'],t['target_order_number'],t['source_line_id'],t['target_line_id'],t['quantity'],t['unit'],date.fromisoformat(t['target_shift_date']),datetime.fromisoformat(t['created_at'].replace('Z','+00:00')),'Да' if t['active'] else 'Нет',t['reason'],t['comment'],t['source_executor_snapshot'],t['target_executor_snapshot'],datetime.fromisoformat(t['cancelled_at'].replace('Z','+00:00')) if t['cancelled_at'] else None,t['cancellation_reason']] for t in data['transfers']]))
    bodies.append(body(['Корневой наряд ID','Наряды цепочки','Код','Ед.','Первоначально выдано','Подтверждено','Открыто','Списано','Превышение'],[
        [r['root_order_id'],', '.join(map(str,r['order_ids'])),r['code'],r['unit'],r['original_quantity'],r['confirmed'],r['open_quantity'],r['written_off'],r['excess']] for r in data['chain_rows']]))
    bodies.append(body(line_headers,[line_row(r) for r in data['discrepancies']]))
    source_headers=['Журнал','Событие ID','Строка наряда ID','Работа ID','Корректирует событие ID','Дата документа','Записан UTC','Количество (изменение)','Ед.','Документ','Комментарий','Источник','Контроль ID','Ссылка на источник','Часть','Всего частей','SHA256','Полный текст / часть']
    source_rows=[
        ['Факт' if r['kind']=='fact' else 'Подтверждение',r['id'],r['work_order_line_id'],r['work_item_id'],r['correction_of_id'],date.fromisoformat(r.get('work_date',r.get('confirmed_date'))),datetime.fromisoformat(r['recorded_at'].replace('Z','+00:00')),r['quantity'],r['unit'],r.get('document_ref',''),r.get('comment',''),r.get('source_type',''),None,None,None,None,None,None] for r in data['source_rows']]
    for r in data.get('control_source_rows',[]):
        kind=r['source_kind']
        reference=('work_order_resolutions' if kind=='resolution' else 'work_order_volume_matches')+':'+str(r['id'])
        label={'writeoff':'Списание','revoke':'Отмена списания'}.get(r.get('kind'),'Сопоставление')
        details=json.dumps(r,ensure_ascii=False,sort_keys=True,default=str)
        source_rows.append([label,None,r['line_id'],None,r.get('reverses_id',r.get('previous_id')),None,
            datetime.fromisoformat(r['recorded_at'].replace('Z','+00:00')),Decimal(r['quantity']),r.get('unit'),
            None,r['reason'],'Управленческое решение; первичные события не изменены',r['id'],reference,None,None,None,details])
    # Chunk ordinary sources too before appending the bounded, ordered text
    # parts on this seventh sheet. No eighth sheet or new writer is needed.
    bodies.append(body(source_headers,source_rows,include_text_chunks=True))
    return pack_workbook(SHEETS,bodies)
