from io import BytesIO
import secrets

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for

from app.repositories.analytics_repository import AnalyticsRepository
from app.repositories.work_repository import WorkRepository
from app.services.analytics_service import AnalyticsService, deviations
from app.services.norms import normative_output
from app.services.xlsx_export import export_xlsx
from app.time_utils import instant
from app.web import current_project_id, get_db
from app.web.forms import InputError, business_date, discipline, integer, quantity, report_dates, text

bp=Blueprint('analytics',__name__)
TABS={'summary':'Сводка объекта','volumes':'Объёмы работ','dynamics':'Динамика выполнения',
      'deviations':'Отклонения','groups':'Аналитические группы','export':'Экспорт Excel'}


def context():
    data=request.args;db=get_db();project_id=current_project_id();work=WorkRepository(db,project_id)
    report_date,cutoff=report_dates(data);direction=discipline(data)
    supplied=integer(data,'project_id','Объект',optional=True)
    if supplied is not None and supplied!=project_id:raise InputError('Выберите объект через переключатель рабочего пространства')
    filters=dict(building_id=integer(data,'building_id','Корпус',optional=True),floor_id=integer(data,'floor_id','Этаж',optional=True),
                 catalog_id=integer(data,'catalog_id','Код работы',optional=True),element=text(data,'element','Конструктив',required=False))
    args=dict(discipline=direction.value,version_id=integer(data,'version_id','Версия плана',optional=True),filters=filters,
        group_id=integer(data,'group_id','Аналитическая группа',optional=True),group_version=integer(data,'group_version','Редакция группы',optional=True),
        start_date=business_date(data,'start_date') if data.get('start_date') else None,end_date=business_date(data,'end_date') if data.get('end_date') else None,
        bucket=data.get('bucket','day'))
    if args['group_version'] and not args['group_id']:raise InputError('Для редакции состава выберите группу')
    result=AnalyticsService(db,project_id).build(report_date,cutoff,**args)
    deviation=data.get('deviation','all');result['deviations']=deviations(result['rows'],deviation)
    query=dict(discipline=direction.value,report_date=report_date,knowledge_cutoff=cutoff,start_date=result['start_date'],end_date=result['end_date'],bucket=result['bucket'],deviation=deviation)
    query.update({k:v for k,v in filters.items() if v not in (None,'')})
    for k in ('version_id','group_id','group_version'):
        if args[k] is not None:query[k]=args[k]
    result.update(tabs=TABS,query=query,deviation_mode=deviation,filter_buildings=work.list_buildings(),floors=work.list_floors(),
        catalog=work.list_catalog(),versions=[v for v in work.list_plan_versions(direction) if v['status']=='APPROVED'],
        filter_groups=AnalyticsRepository(db,project_id).list(cutoff,include_archived=True),elements=sorted({r['element'] for r in work.list_catalog()}))
    return result


@bp.get('/analytics')
@bp.get('/analytics/<tab>')
def index(tab='summary'):
    if tab not in TABS:raise InputError('Неизвестный раздел аналитики')
    data=context()
    norm_code=integer(request.args,'productivity_catalog_id','Код для выработки',optional=True) or data['filters'].get('catalog_id')
    selected=next((c for c in data['catalog'] if c['id']==norm_code),None)
    if norm_code and selected is None:raise InputError('Код для выработки не найден')
    people=integer(request.args,'headcount','Численность',optional=True)
    hours=quantity(request.args,'shift_hours','Часы смены',optional=True)
    norm=quantity(request.args,'productivity_norm','Норматив выработки',optional=True)
    if norm is None and selected and selected['reference_labor_norm'] is not None:norm=selected['reference_labor_norm']
    data.update(productivity=dict(catalog_id=norm_code,headcount=people,hours=hours,norm=norm,
        output=normative_output(people,hours,norm) if selected else None,unit=selected['unit'] if selected else 'ед.'))
    return render_template('analytics.html',tab=tab,**data)


@bp.get('/analytics/export.xlsx')
def download():
    data=context();payload=export_xlsx(data,data['deviation_mode'])
    return send_file(BytesIO(payload),as_attachment=True,download_name='Construction-Progress-'+data['report_date']+'.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@bp.route('/analytics/groups/new',methods=['GET','POST'])
@bp.route('/analytics/groups/<int:group_id>',methods=['GET','POST'])
def group_form(group_id=None):
    db=get_db();repo=AnalyticsRepository(db,current_project_id());work=repo.work
    if request.method=='POST':
        data=request.form
        direction=discipline(data);catalog_id=integer(data,'catalog_id','Код работы')
        includes=[integer(dict(id=i),'id','Позиция группы') for i in data.getlist('include_id')]
        excludes=[integer(dict(id=i),'id','Позиция группы') for i in data.getlist('exclude_id')]
        group_id=repo.save(text(data,'name','Название группы'),catalog_id,unit=text(data,'unit','Единица измерения',limit=30),
            discipline=direction,base_building_id=integer(data,'base_building_id','Базовый корпус',optional=True),
            includes=includes,excludes=excludes,group_id=group_id,expected_version=integer(data,'expected_version','Редакция',optional=True),
            event_key=text(data,'event_key','Ключ операции',limit=100))
        flash('Новая редакция аналитической группы сохранена. Учётные данные не менялись.','success')
        return redirect(url_for('analytics.group_form',group_id=group_id),code=303)
    cutoff=instant(request.args.get('knowledge_cutoff') or None)
    version_no=integer(request.args,'group_version','Редакция',optional=True)
    group=repo.get(group_id,cutoff,version_no=version_no) if group_id else None
    direction=group['discipline'] if group else discipline(request.args).value
    code=group['catalog_id'] if group else integer(request.args,'catalog_id','Код',optional=True)
    entries=work.list_catalog();entry=next((r for r in entries if r['id']==code),None)
    if code and entry is None:raise InputError('Код работы не найден')
    plan=work.get_active_plan_version(direction,report_date=instant()[:10],knowledge_cutoff=cutoff)
    planned={r['work_item_id'] for r in work.list_plan_lines(plan['id'])} if plan else set()
    members={m['work_item_id']:m['operation'] for m in group['members']} if group else {}
    items=[dict(r,in_plan=r['id'] in planned) for r in work.list_work_items(discipline=direction) if r['catalog_id']==code]
    return render_template('analytics_group.html',group=group,catalog=entries,entry=entry,discipline=direction,items=items,members=members,
        buildings=work.list_buildings(),event_key=secrets.token_urlsafe(24),history=repo.history(group_id,cutoff) if group_id else [],
        cutoff=cutoff,read_only=bool(request.args.get('knowledge_cutoff') or version_no))


@bp.post('/analytics/groups/<int:group_id>/actions')
def group_action(group_id):
    action=text(request.form,'action','Действие')
    if action not in ('reset','archive','restore'):raise InputError('Неизвестное действие с группой')
    AnalyticsRepository(get_db(),current_project_id()).revise(group_id,reset=action=='reset',archived=None if action=='reset' else action=='archive',
        expected_version=integer(request.form,'expected_version','Редакция'),event_key=text(request.form,'event_key','Ключ операции',limit=100))
    flash('Сохранена новая редакция группы. Планы и события не изменены.','success')
    return redirect(url_for('analytics.group_form',group_id=group_id),code=303)
