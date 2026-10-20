from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.repositories.base import ValidationError
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.brigade_repository import BrigadeRepository
from app.time_utils import instant
from app.web import current_project_id, get_db
from app.web.forms import business_date, integer, text

bp = Blueprint('personnel', __name__)
KINDS = dict(contractors='Подрядная организация', employees='Работник', specialties='Специальность')


def repository():
    return PersonnelRepository(get_db(), current_project_id())


@bp.get('/personnel')
def index():
    repo = repository()
    return render_template('personnel.html', contractors=repo.list_contractors(), employees=repo.list_employees(), specialties=repo.list_specialties())


@bp.route('/personnel/<kind>/new', methods=['GET', 'POST'])
@bp.route('/personnel/<kind>/<int:record_id>/edit', methods=['GET', 'POST'])
def edit(kind, record_id=None):
    if kind not in KINDS:
        abort(404)
    repo = repository()
    row = (repo.contractor(record_id) if kind == 'contractors' else repo.employee(record_id)
           if kind == 'employees' else repo.specialty(record_id)) if record_id is not None else {}
    error = None
    if request.method == 'POST':
        row = request.form
        try:
            if kind == 'contractors':
                repo.save_contractor(dict(row), record_id)
            elif kind == 'employees':
                data = dict(row)
                data['contractor_id'] = integer(row, 'contractor_id', 'Подрядчик')
                data['specialty_id'] = integer(row, 'specialty_id', 'Специальность')
                data['effective_date'] = business_date(row, 'effective_date', default=instant()[:10])
                # Empty brigade on an existing card does not implicitly remove
                # membership; explicit transfers use the worker detail form.
                data.pop('brigade_id', None)
                if row.get('brigade_id'):
                    data['brigade_id'] = integer(row, 'brigade_id', 'Бригада')
                repo.save_worker(data, record_id)
            else:
                repo.save_specialty(text(row, 'name', 'Специальность'), text(row, 'code', 'Код', limit=80), record_id)
        except ValidationError as exc:
            error = str(exc)
        else:
            flash('Запись справочника сохранена.', 'success')
            return redirect(url_for('personnel_management.workers' if kind=='employees' else 'personnel.index'), code=303)
    return render_template('personnel_form.html', kind=kind, kind_label=KINDS[kind], record=row, error=error,
                           contractors=repo.list_contractors(), specialties=repo.list_specialties(),
                           brigades=BrigadeRepository(get_db(),current_project_id()).list(active_only=True)), 400 if error else 200


@bp.post('/personnel/<kind>/<int:record_id>/status')
def status(kind, record_id):
    if kind not in ('contractors', 'employees'):
        abort(404)
    action = text(request.form, 'action', 'Операция')
    if action not in ('archive', 'restore'):
        raise ValidationError('Неизвестная операция справочника')
    repository().set_active(kind, record_id, action == 'restore')
    flash('Статус записи сохранён. История нарядов сохранена.', 'success')
    return redirect(url_for('personnel.index'), code=303)
