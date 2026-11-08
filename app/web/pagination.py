"""Bound rendered lists without changing calculation inputs or exports."""
import math
from flask import g,request,url_for
from .forms import InputError

def paginate(rows,key='page',total_count=None):
 raw=request.args.get(key,'1')
 if not raw.isascii() or not raw.isdigit() or len(raw)>6 or int(raw)<1:raise InputError('Страница: укажите положительный номер')
 page=int(raw);count=len(rows) if total_count is None else total_count;pages=max(1,math.ceil(count/50))
 links=[]
 for n in sorted({1,pages,*range(max(1,page-2),min(pages,page+2)+1)}):
  query=request.args.to_dict();query[key]=n
  query.update(request.view_args or {})
  links.append(dict(number=n,url=url_for(request.endpoint,**query)))
 if not hasattr(g,'pagination'):g.pagination=[]
 g.pagination.append(dict(page=page,pages=pages,count=count,links=links,key=key))
 return rows if total_count is not None else rows[(page-1)*50:page*50]

def install(app):
 @app.context_processor
 def navigation():return dict(pagination=getattr(g,'pagination',[]))
