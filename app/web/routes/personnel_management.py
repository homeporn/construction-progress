from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from app.repositories.base import ValidationError
from app.repositories.brigade_repository import BrigadeRepository
from app.repositories.personnel_repository import PersonnelRepository
from app.repositories.project_repository import ProjectRepository
from app.time_utils import instant
from app.web import current_project_id, get_db
from app.web.forms import business_date, integer, text

bp=Blueprint('personnel_management',__name__)


def repositories():
    db,p=get_db(),current_project_id()
    return PersonnelRepository(db,p),BrigadeRepository(db,p)


@bp.get('/personnel/workers')
def workers():
    people,brigades=repositories();data=request.args
    filters=dict(search=text(data,'search','ФИО',required=False),contractor_id=integer(data,'contractor_id','Подрядчик',optional=True),
                 specialty_id=integer(data,'specialty_id','Специальность',optional=True),brigade_id=integer(data,'brigade_id','Бригада',optional=True))
    state=text(data,'state','Статус',required=False,limit=10)
    if state not in ('','active','archived'):raise ValidationError('Выберите статус работника')
    rows=people.list_employees(**filters)
    rows=[r for r in rows if not state or bool(r['is_active'])==(state=='active')]
    return render_template('personnel_workers.html',employees=rows,filters=dict(filters,state=state),
                           contractors=people.list_contractors(),specialties=people.list_specialties(),brigades=brigades.list())


@bp.get('/personnel/workers/<int:employee_id>')
def worker_detail(employee_id):
    people,brigades=repositories();employee=people.employee(employee_id)
    return render_template('personnel_worker.html',employee=employee,membership=brigades.current(employee_id),
        memberships=brigades.history(employee_id=employee_id),projects=people.project_history(employee_id),
        brigades=brigades.list(contractor_id=employee['contractor_id'],active_only=True) if employee['contractor_id'] else [],
        specialty=people.specialty(employee['specialty_id']) if employee['specialty_id'] else None)


@bp.post('/personnel/workers/<int:employee_id>/assignment')
def assign(employee_id):
    people,brigades=repositories()
    brigade=integer(request.form,'brigade_id','Бригада',optional=True)
    brigades.assign(employee_id,brigade,business_date(request.form,'effective_date'))
    flash('Назначение сохранено. Состав ранее выданных нарядов не изменился.','success')
    return redirect(url_for('personnel_management.worker_detail',employee_id=employee_id),code=303)


@bp.post('/personnel/workers/<int:employee_id>/project-end')
def project_end(employee_id):
    people,_=repositories();people.end_project_assignment(employee_id,business_date(request.form,'effective_date'))
    flash('Доступность на объекте завершена. История сохранена.','success')
    return redirect(url_for('personnel_management.worker_detail',employee_id=employee_id),code=303)


@bp.route('/personnel/availability',methods=['GET','POST'])
def availability():
    people,_=repositories();data=request.form if request.method=='POST' else request.args
    contractor_id=integer(data,'contractor_id','Подрядчик',optional=True)
    if request.method=='POST':
        employee_id=integer(data,'employee_id','Работник')
        contractor=people.contractor(contractor_id,active=True)
        if not any(e['id']==employee_id for e in people.candidates(contractor['id'])):raise ValidationError('Работник относится к другому подрядчику')
        people.assign_to_project(employee_id,business_date(data,'effective_date'))
        return redirect(url_for('personnel_management.worker_detail',employee_id=employee_id),code=303)
    return render_template('personnel_availability.html',contractors=people.list_contractors(active_only=True),contractor_id=contractor_id,
                           candidates=people.candidates(contractor_id) if contractor_id else [])


@bp.post('/personnel/contractors/<int:contractor_id>/share')
def share(contractor_id):
    people,_=repositories()
    people.share_contractor(contractor_id,integer(request.form,'target_project_id','Объект'))
    flash('Подрядчик доступен на выбранном объекте. Работники не копировались; назначьте их на объект отдельно.','success')
    return redirect(url_for('personnel.index'),code=303)


@bp.route('/personnel/brigades',methods=['GET','POST'])
def brigades():
    people,repo=repositories()
    if request.method=='POST':
        record=repo.save(integer(request.form,'contractor_id','Подрядчик'),text(request.form,'number','Номер бригады',limit=80))
        return redirect(url_for('personnel_management.brigade_detail',brigade_id=record),code=303)
    contractor_id=integer(request.args,'contractor_id','Подрядчик',optional=True)
    return render_template('personnel_brigades.html',brigades=repo.list(contractor_id=contractor_id),contractors=people.list_contractors(active_only=True),contractor_id=contractor_id)


@bp.route('/personnel/brigades/<int:brigade_id>',methods=['GET','POST'])
def brigade_detail(brigade_id):
    people,repo=repositories();brigade=repo.get(brigade_id)
    if request.method=='POST':
        action=text(request.form,'action','Действие')
        if action=='rename':repo.save(brigade['contractor_id'],text(request.form,'number','Номер бригады',limit=80),brigade_id)
        elif action=='assign':repo.assign(integer(request.form,'employee_id','Работник'),brigade_id,business_date(request.form,'effective_date'))
        elif action=='remove':
            employee_id=integer(request.form,'employee_id','Работник')
            repo.remove(employee_id,brigade_id,business_date(request.form,'effective_date'))
        elif action in ('archive','restore'):
            repo.set_active(brigade_id,action=='restore',effective_date=business_date(request.form,'effective_date',default=instant()[:10]),confirm_members=request.form.get('confirm_members')=='1')
        else:raise ValidationError('Неизвестное действие с бригадой')
        return redirect(url_for('personnel_management.brigade_detail',brigade_id=brigade_id),code=303)
    as_of=business_date(request.args,'as_of',default=instant()[:10])
    cutoff=request.args.get('knowledge_cutoff') or instant()
    return render_template('personnel_brigade.html',brigade=brigade,members=repo.members(brigade_id,as_of,cutoff),history=repo.history(brigade_id=brigade_id),as_of=as_of,cutoff=cutoff,
        employees=people.list_employees(active_only=True,contractor_id=brigade['contractor_id']))


@bp.get('/personnel/brigades/<int:brigade_id>/members.json')
def brigade_members(brigade_id):
    _,repo=repositories();brigade=repo.get(brigade_id,active=True)
    when=business_date(request.args,'as_of',default=instant()[:10])
    rows=[dict(id=m['employee_id'],full_name=m['full_name'],specialty_id=m['specialty_id'],specialty_name=m['specialty_name'])
          for m in repo.members(brigade_id,when) if m['is_active']]
    return jsonify(brigade_id=brigade_id,contractor_id=brigade['contractor_id'],number=brigade['number'],members=rows,as_of=when)


@bp.get('/personnel/available-workers.json')
def available_workers():
    people,_=repositories()
    when=business_date(request.args,'as_of',default=instant()[:10])
    rows=people.list_employees(active_only=True,as_of=when)
    return jsonify(project_id=current_project_id(),as_of=when,employees=[
        {k:e[k] for k in ('id','full_name','contractor_id','specialty_id','specialty_name')} for e in rows])
