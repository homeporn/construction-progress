import re
import secrets
from uuid import uuid4

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.repositories.base import ValidationError
from app.repositories.work_order_repository import WorkOrderRepository
from app.repositories.work_order_workflow_repository import WorkOrderWorkflowRepository
from app.time_utils import instant
from app.work_order_models import STATUS_LABELS
from app.web import current_project_id, get_db
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


@bp.get('/work-orders')
def index():
    repo, data = repository(), request.args
    filters = dict(search=text(data, 'search', 'Номер', required=False, limit=80),
                   date_from=business_date(data, 'date_from') if data.get('date_from') else None,
                   date_to=business_date(data, 'date_to') if data.get('date_to') else None,
                   contractor_id=integer(data, 'contractor_id', 'Подрядчик', optional=True),
                   building_id=integer(data, 'building_id', 'Корпус', optional=True),
                   status=text(data, 'status', 'Статус', required=False, limit=30))
    return render_template('work_orders.html', orders=repo.list_orders(**filters), filters=filters,
                           contractors=repo.people.list_contractors(), buildings=repo.work.list_buildings(), order_statuses=STATUS_LABELS)


@bp.route('/work-orders/new', methods=['GET', 'POST'])
@bp.route('/work-orders/<int:order_id>/edit', methods=['GET', 'POST'])
def edit(order_id=None):
    repo = repository()
    order = repo.get_order(order_id) if order_id is not None else dict(shift_date=instant()[:10], shift_start='08:00', shift_end='17:00',
             contractor_id=None, issued_by_employee_id=None, foreman_employee_id=None, notes='', lines=[], crew=[], revision=1, draft_key=uuid4().hex)
    if order_id is not None and order['status'] != 'draft':
        raise InputError('Редактировать можно только черновик наряда.')
    error = None
    if request.method == 'POST':
        try:
            header, lines, crew = parse_form(request.form)
            saved = repo.save_draft(header, lines, crew, order_id=order_id,
                                   expected_revision=integer(request.form, 'revision', 'Редакция черновика') if order_id else None,
                                   draft_key=text(request.form, 'draft_key', 'Ключ формы', limit=100))
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
                           contractors=repo.people.list_contractors(), employees=repo.people.list_employees(), specialties=repo.people.list_specialties(),
                           elements=sorted({i['element'] for i in items}), work_types=sorted({i['work_type'] for i in items})), 400 if error else 200


@bp.get('/work-orders/<int:order_id>')
def detail(order_id):
    repo = repository()
    report_date, cutoff = report_dates(request.args)
    return render_template('work_order_detail.html', order=repo.get_order(order_id, report_date=report_date,
                           knowledge_cutoff=cutoff if request.args.get('knowledge_cutoff') else None), order_statuses=STATUS_LABELS,
                           employees=repo.people.list_employees(active_only=True),
                           workflow_keys={a: secrets.token_urlsafe(24) for a in ('submitted','accepted','closed')})


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
