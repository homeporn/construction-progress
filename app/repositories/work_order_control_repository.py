"""Server-paged registry and shared control/Excel analytical contexts."""
from collections import defaultdict
from decimal import Decimal
import math

from app.calculation_engine.historical import read_snapshot
from app.decimal_utils import ZERO, exact_sum, subtract
from app.repositories.base import ValidationError
from app.services.work_order_control import WorkOrderControlProjection, POLICY, _json
from app.time_utils import day, instant, end_of_day
from app.work_order_models import STATUS_LABELS


QUANTITIES = ('assignment','actual','confirmed','execution_remaining','confirmed_remaining','transferred','reserved','sent','written_off','available','open_quantity','unresolved','overrun','confirmed_overrun','allocation_excess')
DERIVED = ('has_remaining','has_transfer','discrepancies')
BOOLEAN_FILTERS = DERIVED + ('unclosed',)


class WorkOrderControlRepository:
    def __init__(self, db, project_id=1):
        self.db, self.project_id = db, project_id
        self.projection = WorkOrderControlProjection(db, project_id)

    def _filters(self, filters):
        result = dict(filters or {})
        if result.get('status') and result['status'] not in STATUS_LABELS:
            raise ValidationError('Неизвестный статус наряда')
        for key in ('date_from','date_to'):
            if result.get(key):
                day(result[key])
        if result.get('date_from') and result.get('date_to') and result['date_from']>result['date_to']:
            raise ValidationError('Начало периода должно быть не позже окончания')
        for key in ('contractor_id','building_id','brigade_id'):
            if result.get(key) not in (None,''):
                try:
                    result[key]=int(result[key])
                except (ValueError,TypeError):
                    raise ValidationError('Некорректный идентификатор фильтра')
            else:
                result.pop(key,None)
        for key in BOOLEAN_FILTERS:
            if result.get(key) in (None,''):
                result.pop(key,None)
            elif not isinstance(result[key],bool):
                result[key]=str(result[key]).casefold() in ('1','true','yes','on')
        return result

    def _query(self, filters, report_date, cutoff):
        effective=min(cutoff,end_of_day(report_date))
        # The latest historical status is selected before SQL filtering/paging.
        status="COALESCE((SELECT h.new_status FROM work_order_status_history h WHERE h.work_order_id=o.id AND h.changed_at<=? ORDER BY h.changed_at DESC,h.id DESC LIMIT 1),'draft')"
        cte='WITH headers AS (SELECT o.*,c.name AS contractor_name,'+status+' AS asof_status,(SELECT COUNT(*) FROM work_order_lines l WHERE l.work_order_id=o.id) AS line_count FROM work_orders o JOIN contractors c ON c.id=o.contractor_id WHERE o.project_id=? AND o.created_at<=?) '
        args=[effective,self.project_id,effective]
        clauses=[]
        if filters.get('status'):
            clauses.append('h.asof_status=?'); args.append(filters['status'])
        if 'unclosed' in filters:
            predicate="h.asof_status NOT IN ('closed','cancelled')" if filters['unclosed'] else "h.asof_status IN ('closed','cancelled')"
            clauses.append(predicate)
        for key,op in [('contractor_id','='),('date_from','>='),('date_to','<=')]:
            if filters.get(key) is not None:
                column='contractor_id' if key=='contractor_id' else 'shift_date'
                clauses.append('h.'+column+op+'?');args.append(filters[key])
        if filters.get('search'):
            # SQLite lower() does not case-fold Cyrillic; order prefix is fixed.
            search=str(filters['search']).strip().upper()
            clauses.append('UPPER(COALESCE(h.order_number,\'\')) LIKE ? ESCAPE \'\\\'')
            args.append('%'+search.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
        if filters.get('building_id'):
            clauses.append('EXISTS(SELECT 1 FROM work_order_lines l JOIN work_items w ON w.id=l.work_item_id WHERE l.work_order_id=h.id AND w.building_id=?)');args.append(filters['building_id'])
        if filters.get('brigade_id'):
            clauses.append('EXISTS(SELECT 1 FROM work_order_crew c JOIN work_order_crew_members m ON m.crew_id=c.id WHERE c.work_order_id=h.id AND m.brigade_id=?)');args.append(filters['brigade_id'])
        return cte, (' WHERE '+' AND '.join(clauses) if clauses else ''),args

    def _accept(self, lines, filters):
        lines=[r for r in lines if r['status']!='cancelled']
        checks=dict(has_remaining=any(r['unresolved']>ZERO for r in lines),has_transfer=any(r['transferred']>ZERO or r['incoming_quantity']>ZERO for r in lines),discrepancies=any(self._discrepancy(r) for r in lines))
        return all(checks[k]==filters[k] for k in DERIVED if k in filters)

    @staticmethod
    def _discrepancy(row):
        return bool(row['conflicts'] or row['overrun']>ZERO or row['confirmed_overrun']>ZERO or row['matching_required'] or row['state_key']=='review_required')

    def _groups(self, lines, keys):
        groups={}
        for r in lines:
            k=tuple(r[key] for key in keys)
            if k not in groups:
                groups[k]={key:r[key] for key in keys}
                groups[k].update({key:[] for key in QUANTITIES})
                groups[k].update(line_count=0,pending_acceptance=[],matching_required=False)
            group=groups[k]
            group['line_count']+=1
            group['matching_required'] |= r['matching_required']
            for key in QUANTITIES:
                group[key].append(r[key])
            group['pending_acceptance'].append(r['pending_acceptance'])
        for group in groups.values():
            for key in QUANTITIES:
                group[key]=exact_sum(group[key])
            pending=group['pending_acceptance']
            group['pending_acceptance']=None if any(x is None for x in pending) else exact_sum(pending)
        return list(groups.values())

    def _headers(self, headers, lines):
        indexed=defaultdict(list)
        for line in lines:
            indexed[line['order_id']].append(line)
        result=[]
        for header in headers:
            row=dict(header)
            own=indexed[row['id']]
            row['status']=row.pop('asof_status',row['status'])
            if row['status']=='draft' and row['issued_at']:
                row['order_number']=None
                row['header_snapshot']=''
            row['contractor_name']=_json(row['header_snapshot']).get('contractor_name',row['contractor_name'])
            row['buildings']=', '.join(dict.fromkeys(r['location'].get('building_name','') for r in own))
            row['brigades']=list(dict.fromkeys(label for r in own for label in r['brigades']))
            row['volumes']=self._groups(own,('code','unit'))
            row['open_line_count']=sum(r['unresolved']>ZERO for r in own)
            row['transfer_line_count']=sum(r['transferred']>ZERO or r['incoming_quantity']>ZERO for r in own)
            row['has_children']=any(r['transferred']>ZERO for r in own)
            row['execution_state']='cancelled' if row['status']=='cancelled' else 'conflict' if any(r['conflicts'] or r['state_key']=='review_required' for r in own) else ('full' if own and all(r['unresolved']==ZERO for r in own) else ('partial' if any(r['state_key']!='open' for r in own) else 'open'))
            result.append(row)
        return result

    def registry(self, filters=None, page=1, per_page=50, report_date=None, knowledge_cutoff=None):
        with read_snapshot(self.db):
            filters=self._filters(filters)
            try:
                page=max(int(page),1);per_page=min(max(int(per_page),1),200)
            except (TypeError,ValueError):
                raise ValidationError('Некорректная страница реестра')
            cutoff=instant(knowledge_cutoff);report_date=day(report_date or cutoff[:10])
            cte,where,args=self._query(filters,report_date,cutoff)
            total=self.db.execute(cte+'SELECT COUNT(*) FROM headers h'+where,args).fetchone()[0]
            offset=(page-1)*per_page
            order=' ORDER BY h.shift_date DESC,h.id DESC'
            if any(k in filters for k in DERIVED):
                # Bounded exact batches are reduced on the server; only one page
                # of headers/line projections ever reaches the template.
                accepted=[]
                for start in range(0,total,200):
                    ids=[r[0] for r in self.db.execute(cte+'SELECT h.id FROM headers h'+where+order+' LIMIT 200 OFFSET ?',[*args,start])]
                    batch=self.projection.lines(ids,report_date,cutoff)
                    by_order=defaultdict(list)
                    for r in batch:
                        by_order[r['order_id']].append(r)
                    accepted.extend(i for i in ids if self._accept(by_order[i],filters))
                total=len(accepted)
                page_ids=accepted[offset:offset+per_page]
                if page_ids:
                    headers=self.db.execute(cte+'SELECT h.* FROM headers h'+where+' AND h.id IN ('+','.join('?' for _ in page_ids)+')'+order,[*args,*page_ids]).fetchall() if where else self.db.execute(cte+'SELECT h.* FROM headers h WHERE h.id IN ('+','.join('?' for _ in page_ids)+')'+order,[*args,*page_ids]).fetchall()
                else:
                    headers=[]
            else:
                headers=self.db.execute(cte+'SELECT h.* FROM headers h'+where+order+' LIMIT ? OFFSET ?',[*args,per_page,offset]).fetchall()
            lines=self.projection.lines([r['id'] for r in headers],report_date,cutoff)
            return dict(rows=self._headers(headers,lines),total=total,page=page,per_page=per_page,pages=math.ceil(total/per_page),filters=filters)

    def analytics(self, filters=None, report_date=None, knowledge_cutoff=None):
        with read_snapshot(self.db):
            cutoff=instant(knowledge_cutoff);report_date=day(report_date or cutoff[:10])
            filters=self._filters(filters)
            first=self.registry(filters,per_page=200,report_date=report_date,knowledge_cutoff=cutoff)
            headers=list(first['rows'])
            for page in range(2,first['pages']+1):
                headers.extend(self.registry(filters,page=page,per_page=200,report_date=report_date,knowledge_cutoff=cutoff)['rows'])
            ids=[r['id'] for r in headers]
            lines=[]
            for start in range(0,len(ids),200):
                lines.extend(self.projection.lines(ids[start:start+200],report_date,cutoff,include_general=True))
            all_lines,all_transfers,_,_=self.projection._project(None,report_date,cutoff)
            included=set(ids)
            transfers=[t for t in all_transfers if t['source_order_id'] in included or t['target_order_id'] in included]
            incoming={t['target_line_id'] for t in all_transfers if t['active']}
            original_ids={r['line_id'] for r in lines if r['status']!='cancelled' and r['line_id'] not in incoming}
            # Grouped volumes use original assignment once, while linked journals
            # and unconsumed residues are independent at each chain node.
            def chain_groups(rows, keys):
                result=self._groups([r for r in rows if r['status']!='cancelled'],keys)
                original_groups={tuple(g[k] for k in keys):g['assignment'] for g in self._groups([r for r in rows if r['line_id'] in original_ids],keys)}
                for group in result:
                    group['original_quantity']=original_groups.get(tuple(group[k] for k in keys),ZERO)
                    group['assignment']=group['original_quantity']
                return result
            by_work=chain_groups(lines,('catalog_id','code','unit'))
            by_contractor=chain_groups(lines,('contractor_id','contractor_name','code','unit'))
            brigade_lines=[]
            for r in lines:
                label=', '.join(r['brigades']) or 'Без бригады'
                brigade_lines.append(dict(r,brigade=label))
            by_brigade=chain_groups(brigade_lines,('brigade','code','unit'))
            discrepancies=[r for r in lines if self._discrepancy(r)]
            order_lines=defaultdict(list)
            for row in lines:
                if row['status'] not in ('draft','cancelled'):order_lines[row['order_id']].append(row)
            counts=dict(issued=sum(r['status'] not in ('draft','cancelled') for r in headers),
                full=sum(r['execution_state']=='full' for r in headers),
                executed=sum(all(r['actual']>=r['assignment'] for r in own) for own in order_lines.values()),
                confirmed=sum(all(r['confirmed']>=r['assignment'] for r in own) for own in order_lines.values()),
                partial=sum(any(r['actual']>ZERO or r['confirmed']>ZERO for r in own) and not all(max(r['actual'],r['confirmed'])>=r['assignment'] for r in own) for own in order_lines.values()),
                open=sum(r['status'] not in ('draft','closed','cancelled') for r in headers),draft=sum(r['status']=='draft' for r in headers),closed=sum(r['status']=='closed' for r in headers),
                remaining_lines=sum(r['unresolved']>ZERO for r in lines if r['status']!='cancelled'),transferred_lines=sum(r['transferred']>ZERO for r in lines if r['status']!='cancelled'),overdue=None)
            chain_rows=[]
            # Build connected components from the already-read graph. An analytics
            # export must not re-project the entire project once per order.
            adjacency=defaultdict(set)
            for t in all_transfers:
                adjacency[t['source_order_id']].add(t['target_order_id'])
                adjacency[t['target_order_id']].add(t['source_order_id'])
            indexed=defaultdict(list)
            for r in all_lines:
                indexed[r['order_id']].append(r)
            visited=set()
            for oid in ids:
                if oid in visited:
                    continue
                component=set()
                pending=[oid]
                while pending:
                    node=pending.pop()
                    if node in component:
                        continue
                    component.add(node)
                    pending.extend(adjacency[node]-component)
                visited.update(component)
                grouped=defaultdict(list)
                for node in component:
                    for r in indexed[node]:
                        if r['status']!='cancelled':
                            grouped[(r['catalog_id'],r['code'],r['unit'])].append(r)
                for (catalog,code,unit),records in grouped.items():
                    original=exact_sum(r['assignment'] for r in records if r['line_id'] not in incoming)
                    confirmed=exact_sum(r['confirmed'] for r in records)
                    opened=exact_sum(r['open_quantity'] for r in records)
                    written=exact_sum(r['written_off'] for r in records)
                    chain_rows.append(dict(catalog_id=catalog,code=code,unit=unit,original_quantity=original,confirmed=confirmed,open_quantity=opened,written_off=written,excess=max(subtract(exact_sum([confirmed,opened,written]),original),ZERO),root_order_id=min(component),order_ids=sorted(component)))
            source_rows=[]
            for kind,table,datecol in [('fact','fact_events','work_date'),('confirmation','confirmation_events','confirmed_date')]:
                line_ids=[r['line_id'] for r in lines]
                for start in range(0,len(line_ids),200):
                    batch=line_ids[start:start+200]
                    for r in self.db.execute('SELECT * FROM '+table+' WHERE work_order_line_id IN ('+','.join('?' for _ in batch)+') AND '+datecol+'<=? AND recorded_at<=? ORDER BY id',[*batch,report_date,cutoff]):
                        source_rows.append(dict(r,kind=kind,quantity=Decimal(r['quantity_delta'])))
            dynamics={}
            def daily(date,line):
                key=(date,line['catalog_id'],line['code'],line['unit'])
                if key not in dynamics:
                    dynamics[key]=dict(shift_date=date,catalog_id=line['catalog_id'],code=line['code'],unit=line['unit'],assignment=ZERO,original_quantity=ZERO,actual=ZERO,confirmed=ZERO)
                return dynamics[key]
            by_line={r['line_id']:r for r in lines}
            for line in lines:
                if line['line_id'] in original_ids:
                    row=daily(line['shift_date'],line)
                    row['assignment']=exact_sum([row['assignment'],line['assignment']])
                    row['original_quantity']=row['assignment']
            for source in source_rows:
                column='work_date' if source['kind']=='fact' else 'confirmed_date'
                measure='actual' if source['kind']=='fact' else 'confirmed'
                row=daily(source[column],by_line[source['work_order_line_id']])
                row[measure]=exact_sum([row[measure],source['quantity']])
            daily_rows=[dynamics[key] for key in sorted(dynamics)]
            return dict(counts=counts,by_work=by_work,by_contractor=by_contractor,by_brigade=by_brigade,dynamics=daily_rows,discrepancies=discrepancies,rows=lines,registry=headers,filters=filters,report_date=report_date,cutoff=cutoff,transfers=transfers,chain_rows=chain_rows,source_rows=source_rows,policy=POLICY,overdue=None,overdue_policy=POLICY['overdue'])
