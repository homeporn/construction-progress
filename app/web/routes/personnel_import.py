from io import BytesIO
from uuid import uuid4

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for

from app.repositories.base import ValidationError
from app.services.personnel_import import PersonnelImportService
from app.services.personnel_template import make_template, TEMPLATE_NAME
from app.services.personnel_xlsx import MAX_UPLOAD
from app.web import current_project_id, get_db
from app.web.forms import business_date, integer, text

bp=Blueprint('personnel_import',__name__)


def service():return PersonnelImportService(get_db(),current_project_id())


@bp.route('/personnel/import',methods=['GET','POST'])
def start():
    repo=service()
    if request.method=='POST':
        sid=repo.start(integer(request.form,'contractor_id','Подрядчик'),business_date(request.form,'effective_date'),session_id=text(request.form,'session_id','Ключ импорта',limit=100))
        return redirect(url_for('personnel_import.wizard',session_id=sid,step=2),code=303)
    return render_template('personnel_import.html',step=1,contractors=repo.people.list_contractors(active_only=True),new_session_id=uuid4().hex)


@bp.get('/personnel/import/template.xlsx')
def template():
    return send_file(BytesIO(make_template()),mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',as_attachment=True,download_name=TEMPLATE_NAME)


@bp.route('/personnel/import/<session_id>',methods=['GET','POST'])
def wizard(session_id):
    repo=service();s=repo.get(session_id);error=None
    step=integer(request.args,'step','Шаг',optional=True) or (5 if s['status']=='committed' else 2 if s['status']=='context' else 3)
    if step not in (2,3,4,5):raise ValidationError('Неизвестный шаг импорта')
    if request.method=='POST':
        action=text(request.form,'action','Действие')
        try:
            if action=='cancel':
                repo.cancel(session_id)
                return redirect(url_for('personnel_import.history'),code=303)
            revision=integer(request.form,'revision','Редакция импорта')
            if action=='upload':
                f=request.files.get('workbook')
                if f is None or not f.filename:raise ValidationError('Выберите книгу .xlsx')
                sid=repo.upload(session_id,f.stream.read(MAX_UPLOAD+1),f.filename,revision=revision)
                if sid!=session_id:
                    flash('Этот файл уже импортирован. Показан результат прежнего импорта; работники и назначения не дублировались.','success')
                    return redirect(url_for('personnel_import.wizard',session_id=sid,step=5),code=303)
                step=3
            elif action=='decide':
                decisions={}
                for row in repo.rows(session_id):
                    n=str(row['row_number'])
                    decisions[row['row_number']]=dict(decision=request.form.get('decision_'+n,''),employee_id=integer(request.form,'employee_'+n,'ID работника',optional=True),
                        clear_brigade=request.form.get('clear_'+n)=='1',acknowledge=request.form.get('ack_'+n)=='1')
                repo.decide(session_id,decisions,revision=revision);step=4
            elif action=='preview':repo.preview(session_id,revision=revision);step=5
            elif action=='confirm':
                if request.form.get('confirm_import')!='1':raise ValidationError('Подтвердите сохранение импорта')
                repo.confirm(session_id,revision=revision,confirm_new=request.form.get('confirm_new')=='1')
                flash('Импорт сохранён одной транзакцией. Персонал доступен в справочнике и нарядах.','success');step=5
            else:raise ValidationError('Неизвестное действие импорта')
        except ValidationError as exc:error=str(exc)
        else:return redirect(url_for('personnel_import.wizard',session_id=session_id,step=step),code=303)
        s=repo.get(session_id)
    if step>2 and s['status']=='context':step=2
    if s['status'] in ('committed','cancelled'):step=5
    rows=repo.rows(session_id)
    stats=dict(total=len(rows),valid=sum(not r['errors'] for r in rows),invalid=sum(bool(r['errors']) for r in rows))
    return render_template('personnel_import.html',step=step,import_session=s,rows=rows,stats=stats,error=error),400 if error else 200


@bp.get('/personnel/import-history')
def history():
    return render_template('personnel_import_history.html',imports=service().history())
