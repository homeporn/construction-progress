import re
import json
import secrets
from io import BytesIO
from uuid import uuid4

from flask import Blueprint, flash, redirect, render_template, request, url_for, send_file

from app.repositories.base import ValidationError
from app.repositories.work_order_repository import WorkOrderRepository
from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.work_order_workflow_repository import WorkOrderWorkflowRepository
from app.repositories.work_order_control_repository import WorkOrderControlRepository
from app.repositories.work_order_transfer_repository import WorkOrderTransferRepository
from app.services.work_order_control import WorkOrderControlProjection
from app.calculation_engine.historical import read_snapshot
from app.time_utils import instant, end_of_day
from app.work_order_models import STATUS_LABELS
from app.web import current_project, current_project_id, get_db
from app.web.forms import InputError, business_date, integer, quantity, text, report_dates

bp = Blueprint('work_orders', __name__)


def repository():
    return WorkOrderRepository(get_db(), current_project_id())


def parse_form(data):
    header = dict(shift_date=business_date(data, 'shift_date'), shift_start=text(data, 'shift_start', 'Начало смены', limit=5),
                  shift_end=text(data, 'shift_end', 'Окончание смены', limit=5),
                  contractor_id=integer(data, 'contractor_id', 'Подрядчик'),
                  issued_by_employee_id=integer(data, 'issued_by_employee_id', 'Мастер / прораб', optional=True),
                  foreman_employee_id=integer(data, 'foreman_employee_id', 'Бригадир', optional=True),
                  notes=text(data, 'notes', 'Примечание', required=False, limit=4000))
    ids, quantities, remarks = (data.getlist(k) for k in ('work_item_id', 'planned_quantity', 'line_remarks'))
    if not len(ids) == len(quantities) == len(remarks):
        raise InputError('Повреждена таблица работ. Проверьте строки наряда.')
    version_id = integer(data, 'plan_version_id', 'Версия плана', optional=True)
    lines = []
    for item, amount, remark in zip(ids, quantities, remarks):
        values = dict(work_item_id=item, planned_quantity=amount, remarks=remark)
        lines.append(dict(work_item_id=integer(values, 'work_item_id', 'Работа'), plan_version_id=version_id,
                          planned_quantity=quantity(values, 'planned_quantity', 'Объём на смену'),
                          remarks=text(values, 'remarks', 'Примечание строки', required=False, limit=2000)))
    crew, seen = [], set()
    for index in data.getlist('crew_row'):
        if not re.fullmatch(r'[0-9]{1,6}', index) or index in seen:
            raise InputError('Повреждена таблица состава бригады.')
        seen.add(index)
        specialty = integer(data, 'crew_specialty_' + index, 'Специальность')
        count = integer(data, 'crew_count_' + index, 'Численность', optional=True)
        members = [integer(dict(employee_id=e), 'employee_id', 'Работник') for e in data.getlist('crew_employee_' + index)]
        crew.append(dict(specialty_id=specialty, planned_count=count, employee_ids=members,
                         comment=text(data, 'crew_comment_' + index, 'Комментарий бригады', required=False, limit=2000)))
    return header, lines, crew


def control_repository():
    return WorkOrderControlRepository(get_db(), current_project_id())


def commands():
    return WorkOrderTransferRepository(get_db(), current_project_id())


def control_filters(data):
    filters = dict(search=text(data, 'search', 'Номер', required=False, limit=80),
                   date_from=business_date(data, 'date_from') if data.get('date_from') else None,
                   date_to=business_date(data, 'date_to') if data.get('date_to') else None,
                   contractor_id=integer(data, 'contractor_id', 'Подрядчик', optional=True),
                   building_id=integer(data, 'building_id', 'Корпус', optional=True),
                   brigade_id=integer(data, 'brigade_id', 'Бригада', optional=True),
                   status=text(data, 'status', 'Статус', required=False, limit=30))
    for key in ('has_remaining', 'has_transfer', 'unclosed', 'discrepancies'):
        value=data.get(key, '')
        if value not in ('', '0', '1'):
            raise InputError('Выберите значение фильтра остатков / расхождений')
        if value:
            filters[key]=value=='1'
    return filters


def brigade_choices(active_only=False):
    return get_db().execute('SELECT b.*,c.name AS contractor_name FROM brigades b JOIN contractors c ON c.id=b.contractor_id '
        'JOIN contractor_projects cp ON cp.contractor_id=c.id WHERE cp.project_id=? AND (?=0 OR b.is_active=1) ORDER BY c.name,b.number,b.id',
        (current_project_id(),active_only)).fetchall()


def filter_context(repo, filters, report_date, cutoff):
    return dict(filters=filters, report_date=report_date, cutoff=cutoff, contractors=repo.people.list_contractors(),
        buildings=repo.work.list_buildings(), permanent_brigades=brigade_choices(), order_statuses=STATUS_LABELS)


def paged_url(endpoint, page):
    values=request.args.to_dict()
    values['page']=page
    return url_for(endpoint, **values)


@bp.get('/work-orders')
def index():
    filters=control_filters(request.args)
    report_date, cutoff=report_dates(request.args)
    page=integer(request.args, 'page', 'Страница', optional=True) or 1
    result=control_repository().registry(filters,page=page,per_page=50,report_date=report_date,knowledge_cutoff=cutoff)
    return render_template('work_orders.html', orders=result['rows'], registry=result,
        previous_url=paged_url('work_orders.index',page-1) if page>1 else None,
        next_url=paged_url('work_orders.index',page+1) if page<result['pages'] else None,
        **filter_context(repository(),filters,report_date,cutoff))


@bp.get('/work-orders/control')
def control():
    filters=control_filters(request.args)
    report_date,cutoff=report_dates(request.args)
    data=control_repository().analytics(filters,report_date,cutoff)
    # Full data belongs in the workbook; the page shows bounded analytical tables.
    truncated={k:len(data[k])>50 for k in ('by_work','by_contractor','by_brigade','dynamics','discrepancies')}
    for key in truncated:
        data[key]=data[key][:50]
    return render_template('work_order_control.html', data=data,truncated=truncated,
        export_url=url_for('work_orders.control_export',**request.args.to_dict()),
        **filter_context(repository(),filters,report_date,cutoff))


@bp.get('/work-orders/control/export.xlsx')
def control_export():
    from app.services.work_order_control_xlsx import export_xlsx
    report_date,cutoff=report_dates(request.args)
    with read_snapshot(get_db()):
        data=control_repository().analytics(control_filters(request.args),report_date,cutoff)
        data['control_source_rows']=[]
        line_ids=[row['line_id'] for row in data['rows']]
        units={row['line_id']:row['unit'] for row in data['rows']}
        effective=min(cutoff,end_of_day(report_date))
        for start in range(0,len(line_ids),200):
            batch=line_ids[start:start+200]
            for table,kind in (('work_order_resolutions','resolution'),('work_order_volume_matches','match')):
                data['control_source_rows'].extend(dict(row,source_kind=kind,unit=units[row['line_id']]) for row in get_db().execute(
                    'SELECT * FROM '+table+' WHERE project_id=? AND line_id IN ('+','.join('?' for _ in batch)+') AND recorded_at<=? ORDER BY recorded_at,id',
                    [current_project_id(),*batch,effective]))
        data['project']=dict(current_project())
    return send_file(BytesIO(export_xlsx(data)),as_attachment=True,download_name='work-order-control-'+report_date+'.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@bp.route('/work-orders/new', methods=['GET', 'POST'])
@bp.route('/work-orders/<int:order_id>/edit', methods=['GET', 'POST'])
def edit(order_id=None):
    repo = repository()
    order = repo.get_order(order_id) if order_id is not None else dict(shift_date=instant()[:10], shift_start='08:00', shift_end='17:00',
             contractor_id=None, issued_by_employee_id=None, foreman_employee_id=None, notes='', lines=[], crew=[], revision=1, draft_key=uuid4().hex)
    if order_id is not None and order['status'] != 'draft':
        raise InputError('Редактировать можно только черновик наряда.')
    order['inherited']=any(line.get('incoming_quantity',0)>0 for line in order['lines'])
    error = None
    if request.method == 'POST':
        try:
            header, lines, crew = parse_form(request.form)
            saved = repo.save_draft(header, lines, crew, order_id=order_id,
                                   expected_revision=integer(request.form, 'revision', 'Редакция черновика') if order_id else None,
                                   draft_key=text(request.form, 'draft_key', 'Ключ формы', limit=100),
                                   brigade_ids=[integer(dict(id=b),'id','Бригада') for b in request.form.getlist('brigade_id')])
        except (ValidationError, ValueError) as exc:
            error = str(exc)
            # Preserve every submitted value, including repeatable rows, on errors.
            order.update({key: request.form.get(key, '') for key in ('shift_date', 'shift_start', 'shift_end', 'notes', 'draft_key', 'revision')})
            for key in ('contractor_id', 'issued_by_employee_id', 'foreman_employee_id'):
                value = request.form.get(key, '')
                order[key] = int(value) if re.fullmatch(r'[0-9]{1,9}', value) else None
            order['lines'] = [dict(work_item_id=int(i) if re.fullmatch(r'[0-9]{1,9}', i) else None, planned_quantity=q, remarks=r)
                              for i, q, r in zip(request.form.getlist('work_item_id'), request.form.getlist('planned_quantity'), request.form.getlist('line_remarks'))]
            order['crew'] = []
            order['brigade_ids'] = [int(b) for b in request.form.getlist('brigade_id') if re.fullmatch(r'[0-9]{1,9}', b)]
            for index in request.form.getlist('crew_row'):
                if not re.fullmatch(r'[0-9]{1,6}', index):
                    continue
                specialty = request.form.get('crew_specialty_' + index, '')
                order['crew'].append(dict(specialty_id=int(specialty) if re.fullmatch(r'[0-9]{1,9}', specialty) else None,
                    planned_count=request.form.get('crew_count_' + index, ''), comment=request.form.get('crew_comment_' + index, ''),
                    employee_ids=[int(e) for e in request.form.getlist('crew_employee_' + index) if re.fullmatch(r'[0-9]{1,9}', e)]))
        else:
            flash('Черновик наряда сохранён.', 'success')
            return redirect(url_for('work_orders.detail', order_id=saved), code=303)
    version = repo.work.get_active_plan_version('CONSTRUCTION', report_date=instant()[:10], knowledge_cutoff=instant())
    items = repo.available_items()
    return render_template('work_order_form.html', order=order, error=error, available_items=items,
                           item_map={i['id']: i for i in items}, plan_version=version,
                           buildings=repo.work.list_buildings(include_archived=False), floors=repo.work.list_floors(include_archived=False),
                           contractors=repo.people.list_contractors(), employees=repo.people.list_employees(as_of=order['shift_date']), specialties=repo.people.list_specialties(),
                           permanent_brigades=BrigadeRepository(get_db(),current_project_id()).list(active_only=True,as_of=order['shift_date']),
                           elements=sorted({i['element'] for i in items}), work_types=sorted({i['work_type'] for i in items})), 400 if error else 200


@bp.get('/work-orders/<int:order_id>')
def detail(order_id):
    repo = repository()
    report_date, cutoff = report_dates(request.args)
    effective_cutoff=cutoff if request.args.get('knowledge_cutoff') else None
    with read_snapshot(get_db()):
        order=repo.get_order(order_id, report_date=report_date,knowledge_cutoff=effective_cutoff)
        control=WorkOrderControlProjection(get_db(),current_project_id()).report(order_id,report_date,order['cutoff'])
    statuses={r['order_id']:r['status'] for r in control['chain_nodes']}
    for transfer in control['transfers']:
        transfer['source_executor']=json.loads(transfer['source_executor_snapshot'])
        transfer['target_executor']=json.loads(transfer['target_executor_snapshot'])
        transfer['target_status']=statuses.get(transfer['target_order_id'],'draft')
    revoked={r.get('reverses_id') for r in control['timeline'] if r['kind']=='revoke'}
    for row in control['timeline']:
        row['revoked']=row.get('id') in revoked if row['kind']=='writeoff' else False
    return render_template('work_order_detail.html',order=order,control=control,order_statuses=STATUS_LABELS,
        employees=repo.people.list_employees(active_only=True),
        workflow_keys={a:secrets.token_urlsafe(24) for a in ('submitted','accepted','closed')},
        operation_key=secrets.token_urlsafe(24))


@bp.post('/work-orders/<int:order_id>/issue')
def issue(order_id):
    number = repository().issue(order_id)
    flash('Выдан наряд ' + number + '. Факт и подтверждение не изменились.', 'success')
    return redirect(url_for('work_orders.detail', order_id=order_id), code=303)


@bp.post('/work-orders/<int:order_id>/cancel')
def cancel(order_id):
    repository().cancel(order_id, text(request.form, 'reason', 'Основание отмены', limit=2000))
    flash('Наряд отменён. Номер и история сохранены.', 'success')
    return redirect(url_for('work_orders.detail', order_id=order_id), code=303)


@bp.get('/work-orders/<int:order_id>/print')
def printing(order_id):
    report_date, cutoff = report_dates(request.args)
    return render_template('work_order_print.html', order=repository().get_order(order_id, report_date=report_date,
                           knowledge_cutoff=cutoff if request.args.get('knowledge_cutoff') else None), order_statuses=STATUS_LABELS)


@bp.post('/work-orders/<int:order_id>/workflow/<action>')
def workflow(order_id, action):
    checks = {}
    for value in request.form.getlist('review_line_id'):
        line_id = integer(dict(id=value), 'id', 'Строка наряда')
        if line_id in checks:
            raise InputError('Строка проверки повторяется')
        checks[line_id] = dict(checked=request.form.get('checked_' + str(line_id)) == '1',
                              comment=text(request.form, 'review_comment_' + str(line_id), 'Замечание строки', required=False, limit=2000))
    WorkOrderWorkflowRepository(get_db(), current_project_id()).act(order_id, action,
        integer(request.form, 'employee_id', 'Ответственное лицо'),
        comment=text(request.form, 'comment', 'Комментарий действия', required=False, limit=2000),
        checks=checks, event_key=text(request.form, 'event_key', 'Ключ действия', limit=100),
        expected_signature=text(request.form, 'event_signature', 'Срез объёмов', limit=64))
    flash('Действие с нарядом зарегистрировано. Журналы объёмов не изменились.', 'success')
    return redirect(url_for('work_orders.detail', order_id=order_id), code=303)


@bp.route('/work-orders/<int:order_id>/lines/<int:line_id>/events/<kind>', methods=['GET','POST'])
def register_event(order_id, line_id, kind):
    if kind not in ('fact', 'confirmation'):
        raise InputError('Неизвестный журнал событий')
    line = WorkOrderWorkflowRepository(get_db(), current_project_id()).line(line_id)
    if line['work_order_id'] != order_id:
        raise InputError('Строка не относится к выбранному наряду')
    from .events import journal
    return journal(kind, forced_line_id=line_id)


def scoped_line(order_id,line_id):
    line=WorkOrderWorkflowRepository(get_db(),current_project_id()).line(line_id)
    if line['work_order_id']!=order_id:
        raise InputError('Строка не относится к выбранному наряду')
    return line


def operation_key():
    return text(request.form,'operation_key','Ключ операции',limit=100)


@bp.route('/work-orders/<int:order_id>/transfer',methods=['GET','POST'])
def transfer(order_id):
    repo=repository()
    order=repo.get_order(order_id)
    repo.work._writable()
    if order['status'] in ('draft','cancelled'):
        raise InputError('Перенос доступен из выданного неотменённого наряда')
    error=None
    values=request.form if request.method=='POST' else None
    target=dict(shift_date=max(order['shift_date'],instant()[:10]),shift_start=order['shift_start'],shift_end=order['shift_end'],
        contractor_id=order['contractor_id'],issued_by_employee_id=order['issued_by_employee_id'],
        foreman_employee_id=order['foreman_employee_id'],notes='',crew=[],brigade_ids=[])
    key=secrets.token_urlsafe(24)
    if request.method=='POST':
        try:
            # Only inherited source selections define transfer lines. Header/crew
            # parsing must never interpret an ordinary work table as a new order.
            header_data=request.form.copy()
            for name in ('work_item_id','planned_quantity','line_remarks'):
                header_data.setlist(name,[])
            header,_,crew=parse_form(header_data)
            lines=[]
            for source in request.form.getlist('source_line_id'):
                line_id=integer(dict(id=source),'id','Исходная строка')
                lines.append(dict(source_line_id=line_id,quantity=quantity(request.form,'quantity_'+str(line_id),'Объём переноса')))
            result=commands().transfer(order_id,lines,header,crew,
                brigade_ids=[integer(dict(id=b),'id','Бригада') for b in request.form.getlist('brigade_id')],
                reason=text(request.form,'reason','Основание переноса',limit=4000),
                comment=text(request.form,'comment','Комментарий',required=False,limit=4000),operation_key=operation_key())
        except (ValidationError,ValueError) as exc:
            error=str(exc)
            for name in ('shift_date','shift_start','shift_end','notes'):
                target[name]=request.form.get(name,'')
            for name in ('contractor_id','issued_by_employee_id','foreman_employee_id'):
                value=request.form.get(name,'')
                target[name]=int(value) if re.fullmatch(r'[0-9]{1,9}',value) else None
            target['brigade_ids']=[int(b) for b in request.form.getlist('brigade_id') if re.fullmatch(r'[0-9]{1,9}',b)]
            for index in request.form.getlist('crew_row'):
                if not re.fullmatch(r'[0-9]{1,6}',index):continue
                specialty=request.form.get('crew_specialty_'+index,'')
                target['crew'].append(dict(specialty_id=int(specialty) if re.fullmatch(r'[0-9]{1,9}',specialty) else None,
                    planned_count=request.form.get('crew_count_'+index,''),comment=request.form.get('crew_comment_'+index,''),
                    employee_ids=[int(e) for e in request.form.getlist('crew_employee_'+index) if re.fullmatch(r'[0-9]{1,9}',e)]))
            key=request.form.get('operation_key',key)
        else:
            flash('Остаток зарезервирован в новом черновике. Выдайте новый наряд, чтобы завершить перенос.','success')
            return redirect(url_for('work_orders.detail',order_id=result['order_id']),code=303)
    as_of=target['shift_date']
    try: business_date(dict(shift_date=as_of),'shift_date')
    except ValueError:as_of=instant()[:10]
    return render_template('work_order_transfer.html',order=order,target=target,values=values,error=error,
        operation_key=key,employees=repo.people.list_employees(as_of=as_of),contractors=repo.people.list_contractors(),
        specialties=repo.people.list_specialties(),permanent_brigades=brigade_choices(active_only=True)),400 if error else 200


@bp.post('/work-orders/transfers/<int:transfer_id>/cancel')
def cancel_transfer(transfer_id):
    row=get_db().execute('SELECT source_order_id FROM work_order_transfers WHERE id=? AND project_id=?',(transfer_id,current_project_id())).fetchone()
    if row is None:raise InputError('Перенос не принадлежит текущему объекту')
    commands().cancel_transfer(transfer_id,text(request.form,'reason','Основание отмены',limit=4000),operation_key())
    flash('Перенос отменён; резерв возвращён исходному наряду.','success')
    return redirect(url_for('work_orders.detail',order_id=row['source_order_id']),code=303)


@bp.post('/work-orders/<int:order_id>/lines/<int:line_id>/writeoff')
def writeoff(order_id,line_id):
    scoped_line(order_id,line_id)
    commands().writeoff(line_id,quantity(request.form,'quantity','Объём списания'),
        text(request.form,'reason','Основание списания',limit=4000),operation_key())
    flash('Списание невыполненного остатка зарегистрировано.','success')
    return redirect(url_for('work_orders.detail',order_id=order_id),code=303)


@bp.post('/work-orders/resolutions/<int:resolution_id>/revoke')
def revoke_writeoff(resolution_id):
    row=get_db().execute('SELECT l.work_order_id FROM work_order_resolutions r JOIN work_order_lines l ON l.id=r.line_id '
        'WHERE r.id=? AND r.project_id=?',(resolution_id,current_project_id())).fetchone()
    if row is None:raise InputError('Списание не принадлежит текущему объекту')
    commands().revoke_writeoff(resolution_id,text(request.form,'reason','Причина отмены списания',limit=4000),operation_key())
    flash('Списание отменено новой записью; история сохранена.','success')
    return redirect(url_for('work_orders.detail',order_id=row['work_order_id']),code=303)


@bp.route('/work-orders/<int:order_id>/lines/<int:line_id>/match',methods=['GET','POST'])
def match(order_id,line_id):
    scoped_line(order_id,line_id)
    if request.method=='POST':
        revision=request.form.get('expected_revision','0')
        if not re.fullmatch(r'[0-9]{1,9}',revision):raise InputError('Некорректная редакция сопоставления')
        commands().match(line_id,integer(request.form,'fact_event_id','Событие факта'),
            integer(request.form,'confirmation_event_id','Событие подтверждения'),quantity(request.form,'quantity','Объём сопоставления'),
            text(request.form,'reason','Основание сопоставления',limit=4000),expected_revision=int(revision),operation_key=operation_key())
        flash('Сопоставление зарегистрировано. Первичные события не изменились.','success')
        return redirect(url_for('work_orders.detail',order_id=order_id),code=303)
    report_date,cutoff=report_dates(request.args)
    order=repository().get_order(order_id,report_date=report_date,knowledge_cutoff=cutoff if request.args.get('knowledge_cutoff') else None)
    line=next(r for r in order['lines'] if r['id']==line_id)
    query=text(request.args,'search','Поиск события',required=False,limit=80)
    roots={}
    for kind,table,date_field in (('fact','fact_events','work_date'),('confirmation','confirmation_events','confirmed_date')):
        roots[kind]=[dict(r,net=line[kind+'_roots'][r['id']]) for r in get_db().execute(
            'SELECT * FROM '+table+' WHERE work_order_line_id=? AND correction_of_id IS NULL AND '+date_field+'<=? AND recorded_at<=? '
            "AND (?='' OR CAST(id AS TEXT)=? OR comment LIKE ?) ORDER BY id DESC LIMIT 50",
            (line_id,report_date,order['cutoff'],query,query,'%'+query+'%')) if r['id'] in line[kind+'_roots']]
    effective=min(order['cutoff'],end_of_day(report_date))
    latest=get_db().execute('SELECT m.* FROM work_order_volume_matches m WHERE m.line_id=? AND m.recorded_at<=? '
        'AND NOT EXISTS(SELECT 1 FROM work_order_volume_matches n WHERE n.previous_id=m.id AND n.recorded_at<=?) '
        "AND (?='' OR CAST(m.fact_event_id AS TEXT)=? OR CAST(m.confirmation_event_id AS TEXT)=? OR m.reason LIKE ?) ORDER BY m.id DESC LIMIT 50",
        (line_id,effective,effective,query,query,query,'%'+query+'%')).fetchall()
    return render_template('work_order_matches.html',order=order,line=line,roots=roots,matches=latest,search=query,operation_key=secrets.token_urlsafe(24))
