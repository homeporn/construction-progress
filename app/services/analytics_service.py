"""One exact, read-only projection shared by HTML, SVG and XLSX."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from app.calculation_engine.aggregation import aggregate
from app.calculation_engine.historical import HistoricalCalculator, read_snapshot
from app.calculation_engine.models import GroupProgress, Status
from app.decimal_utils import ZERO, exact_sum, multiply, ratio, subtract
from app.models import Discipline
from app.repositories.analytics_repository import AnalyticsRepository
from app.repositories.base import ValidationError
from app.repositories.event_repository import EventRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.work_repository import WorkRepository
from app.time_utils import day, instant


def natural_totals(rows):
    rows = list(rows)
    if len({(r['code'],r['unit'],r['discipline']) for r in rows})>1:
        raise ValidationError('Натуральный итог допускает только один код, единицу и направление')
    if len({r['id'] for r in rows})!=len(rows):
        raise ValidationError('Повторное включение позиции в натуральный итог')
    if not rows:
        return dict(planned=None,actual=None,confirmed=None,remaining=None,pending=None,actual_ratio=None,confirmed_ratio=None)
    planned = exact_sum(r['planned'] for r in rows) if all(r['planned'] is not None for r in rows) else None
    actual, confirmed = [exact_sum(r[key] for r in rows) for key in ('actual','confirmed')]
    return dict(planned=planned,actual=actual,confirmed=confirmed,
        remaining=subtract(planned,actual) if planned is not None else None,pending=subtract(actual,confirmed),
        actual_ratio=ratio(actual,planned) if planned is not None and planned>0 else None,
        confirmed_ratio=ratio(confirmed,planned) if planned is not None and planned>0 else None)


def periods(start, end, bucket):
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    if start>end or bucket not in ('day','week','month','range'):
        raise ValidationError('Проверьте границы и шаг периода')
    result=[];cursor=start
    while cursor<=end:
        if len(result)>=3660:
            raise ValidationError('Слишком много точек графика. Выберите неделю, месяц или диапазон')
        if bucket=='day': boundary=cursor
        elif bucket=='week': boundary=cursor+timedelta(days=6-cursor.weekday())
        elif bucket=='month': boundary=date.max if cursor.year==9999 and cursor.month==12 else (
            (date(cursor.year+1,1,1) if cursor.month==12 else date(cursor.year,cursor.month+1,1))-timedelta(days=1))
        else: boundary=end
        boundary=min(boundary,end)
        result.append((cursor.isoformat(),boundary.isoformat()))
        if boundary==end: break
        cursor=boundary+timedelta(days=1)
    return result


class AnalyticsService:
    def __init__(self, db, project_id=1):
        self.db,self.project_id=db,project_id
        self.work=WorkRepository(db,project_id)
        self.groups=AnalyticsRepository(db,project_id)
        self.events=EventRepository(db,project_id)
        self.core=HistoricalCalculator(db,project_id)

    def _validate_filters(self, filters):
        unknown=set(filters)-{'building_id','floor_id','element','catalog_id'}
        if unknown: raise ValidationError('Неизвестный фильтр аналитики')
        if filters.get('building_id'): self.work._building(filters['building_id'])
        if filters.get('floor_id'):
            floor=self.work._floor(filters['floor_id'])
            if filters.get('building_id') and floor['building_id']!=filters['building_id']:
                raise ValidationError('Этаж не относится к выбранному корпусу')
        if filters.get('catalog_id') and not self.db.execute('SELECT 1 FROM work_catalog WHERE id=?',(filters['catalog_id'],)).fetchone():
            raise ValidationError('Код работы не найден')

    @staticmethod
    def matches(row, filters):
        return all(value in (None,'') or row[key]==value for key,value in filters.items())

    def composition(self, version, all_rows, metadata):
        compatible={r['id'] for r in all_rows if r['catalog_id']==version['catalog_id'] and r['unit']==version['unit']
                    and r['discipline']==version['discipline'] and r['in_plan']}
        automatic={i for i in compatible if not version['base_building_id'] or metadata[i]['building_id']==version['base_building_id']}
        includes={m['work_item_id'] for m in version['members'] if m['operation']=='include'}
        excludes={m['work_item_id'] for m in version['members'] if m['operation']=='exclude'}
        missing=sorted((includes|excludes)-compatible)
        warnings=['Позиция №{} отсутствует в выбранной версии плана или несовместима; замена не выполнялась'.format(i) for i in missing]
        return (automatic|includes)-excludes, warnings, bool(includes-compatible)

    def build(self, report_date=None, knowledge_cutoff=None, *, discipline='CONSTRUCTION', version_id=None,
              filters=None, group_id=None, group_version=None, start_date=None, end_date=None, bucket='day'):
        cutoff=instant(knowledge_cutoff);report_date=day(report_date or cutoff[:10]);direction=Discipline(discipline)
        filters={k:v for k,v in (filters or {}).items() if v not in (None,'')}
        self._validate_filters(filters)
        end=day(end_date or report_date)
        start=day(start_date or max(date.min,date.fromisoformat(end)-timedelta(days=min(29,(date.fromisoformat(end)-date.min).days))).isoformat())
        if end>report_date: raise ValidationError('Конец периода не может быть позже даты среза')
        intervals=periods(start,end,bucket)
        with read_snapshot(self.db):
            project=dict(ProjectRepository(self.db).get_project(self.project_id))
            if version_id is not None:
                version=self.work.get_plan_version(version_id)
                if version['status']!='APPROVED' or version['discipline']!=direction.value:
                    raise ValidationError('Выберите утверждённый план текущего направления')
            else: version=self.work.get_active_plan_version(direction,report_date=report_date,knowledge_cutoff=cutoff)
            report=self.core.calculate_as_of(report_date,cutoff,version['id']) if version else None
            metadata={r['id']:dict(r) for r in self.work.list_work_items(discipline=direction.value)}
            all_rows=[]
            for result in report.items if report else []:
                s=result.source;r=dict(metadata[s.work_item_id])
                r.update(planned=s.planned_quantity,actual=s.actual,confirmed=s.confirmed,labor_norm=s.labor_norm,
                         unit=s.plan_unit,in_plan=s.in_plan,result=result)
                r.update(natural_totals([r]));all_rows.append(r)
            all_by_id={r['id']:r for r in all_rows}
            warnings=[i.message for i in report.issues] if report else []
            group_views=[];selected=None;selected_ids=None;invalid_selection=False
            for group in self.groups.list(cutoff,include_archived=True):
                if group['discipline']!=direction.value:continue
                if group_id==group['group_id'] and group_version is not None:
                    group=self.groups.get(group_id,cutoff,version_no=group_version)
                ids,notes,invalid=self.composition(group,all_rows,metadata)
                members=[all_by_id[i] for i in sorted(ids) if i in all_by_id and self.matches(all_by_id[i],filters)]
                view=dict(group);view.update(rows=members,warnings=notes,missing=[i for i in ids if i not in all_by_id],invalid=invalid)
                view.update(natural_totals(members) if not invalid else natural_totals([]))
                group_views.append(view)
                if group_id==group['group_id']:
                    selected=view;selected_ids=ids;invalid_selection=invalid;warnings+=notes
            if group_id is not None and selected is None:
                self.groups.get(group_id,cutoff,version_no=group_version)
                raise ValidationError('Направление аналитической группы не совпадает с фильтром')
            rows=[r for r in all_rows if self.matches(r,filters) and (selected_ids is None or r['id'] in selected_ids)]
            total=aggregate(r['result'] for r in rows) if rows else (
                report.total if report and not report.items and report.total else GroupProgress(status=report.status if report else Status.NO_DATA))
            if invalid_selection: total=GroupProgress(status=Status.INVALID)
            auto=[];buckets=defaultdict(list)
            for r in rows:buckets[(r['code'],r['unit'])].append(r)
            for (code,unit),values in sorted(buckets.items()):
                auto.append(dict(code=code,unit=unit,name=values[0]['name'],rows=values,**natural_totals(values)))
            buildings=[]
            for b in self.work.list_buildings():
                if filters.get('building_id') and b['id']!=filters['building_id']:continue
                related=[r for r in rows if r['building_id']==b['id']]
                progress=aggregate(r['result'] for r in related)
                buildings.append(dict(b,progress=progress,gap=subtract(progress.actual_progress,progress.confirmed_progress)
                                      if progress.actual_progress is not None else None,count=len(related)))
            ids={r['id'] for r in rows}
            latest=max((r['recorded_at'] for kind in ('fact','confirmation') for r in self.events.list_events(kind,report_date=report_date,knowledge_cutoff=cutoff)
                        if r['work_item_id'] in ids),default=None)
            data=dict(project=project,discipline=direction.value,version=dict(version) if version else None,report=report,
                report_date=report_date,cutoff=cutoff,filters=filters,group=selected,group_views=[selected] if selected else group_views,group_id=group_id,
                group_version=group_version,start_date=start,end_date=end,bucket=bucket,rows=rows,automatic=auto,buildings=buildings,
                total=total,object_total=report.total if report and report.total else GroupProgress(status=report.status if report else Status.NO_DATA),
                warnings=list(dict.fromkeys(warnings)),item_count=len(rows),actual_count=sum(r['actual']>0 for r in rows),
                pending_count=sum(r['pending']>0 for r in rows),building_count=len(buildings),latest_event=latest,
                gap=subtract(total.actual_progress,total.confirmed_progress) if total.actual_progress is not None else None)
            data['series']=self._series(data,intervals)
            return data

    def _series(self,data,intervals):
        rows=data['rows'];ids={r['id'] for r in rows}
        natural=bool(data['group'] or data['filters'].get('catalog_id')) and len({(r['code'],r['unit']) for r in rows})==1
        unit=rows[0]['unit'] if natural else '%'
        cache={}
        def values(date_string):
            if date_string is None:return (ZERO,ZERO) if natural else (None,None)
            if date_string in cache:return cache[date_string]
            if natural:
                result=tuple(exact_sum(v for i,v in self.events.cumulative_quantities(k,date_string,data['cutoff'],discipline=data['discipline']).items() if i in ids) for k in ('fact','confirmation'))
            elif data['version']:
                report=self.core.calculate_as_of(date_string,data['cutoff'],data['version']['id'])
                progress=aggregate(i for i in report.items if i.source.work_item_id in ids)
                result=tuple(multiply(v,Decimal(100)) if v is not None else None for v in (progress.actual_progress,progress.confirmed_progress))
            else: result=(None,None)
            cache[date_string]=result;return result
        points=[]
        for begin,end in intervals:
            previous=(date.fromisoformat(begin)-timedelta(days=1)).isoformat() if begin>'0001-01-01' else None
            a,c=values(end);pa,pc=values(previous)
            if data['group'] and data['group']['invalid']:a=c=None
            points.append(dict(start=begin,date=end,actual=a,confirmed=c,
                actual_period=subtract(a,pa) if a is not None and pa is not None else None,
                confirmed_period=subtract(c,pc) if c is not None and pc is not None else None))
        return dict(unit=unit,natural=natural,code=rows[0]['code'] if natural else None,points=points,
                    cumulative_chart=chart(points,('actual','confirmed')),period_chart=chart(points,('actual_period','confirmed_period')))


def deviations(rows, mode='all'):
    predicates=dict(all=lambda r:True,pending=lambda r:r['pending']>0,overrun=lambda r:r['remaining'] is not None and r['remaining']<0,
        no_actual=lambda r:r['actual']==0,complete=lambda r:r['planned'] is not None and r['planned']>0 and r['actual']>=r['planned'],
        confirmed=lambda r:r['planned'] is not None and r['planned']>0 and r['confirmed']>=r['planned'])
    if mode not in predicates:raise ValidationError('Неизвестный фильтр отклонений')
    return [r for r in rows if predicates[mode](r)]


def chart(points, keys):
    """Decimal -> SVG coordinates only; never a source of analytical results."""
    values=[p[k] for p in points for k in keys if p[k] is not None]
    if not values:return None
    low=min([ZERO]+values);high=max([ZERO]+values)
    if high==low:high=exact_sum([low,Decimal(1)])
    paths=[]
    for key in keys:
        segments=[];current=[]
        for i,p in enumerate(points):
            if p[key] is None:
                if current:segments.append(' '.join(current));current=[]
                continue
            x=exact_sum([Decimal(55),multiply(ratio(Decimal(i),Decimal(max(1,len(points)-1))),Decimal(810))])
            y=subtract(Decimal(220),multiply(ratio(subtract(p[key],low),subtract(high,low)),Decimal(195)))
            current.append(format(x,'.3f')+','+format(y,'.3f'))
        if current:segments.append(' '.join(current))
        paths.append(segments)
    return dict(paths=paths,low=low,high=high,low_label=format(low,'.2f'),high_label=format(high,'.2f'),
                first=points[0]['date'],last=points[-1]['date'] if len(points)>1 else '')
