from pathlib import Path
import pytest
from tools.performance.generate_dataset import generate,destination
from tools.performance.benchmark import stats
from app.database import connect
from app.services.analytics_service import AnalyticsService
from app.repositories.event_repository import EventRepository
from app.web import create_app

@pytest.fixture
def load(tmp_path):
 p=tmp_path/'load.sqlite3';m=generate(p,counts=(1,3,120,600,3,90));return p,m

def test_destination_is_exclusive_and_protected(tmp_path):
 p=tmp_path/'existing.sqlite3';p.write_bytes(b'original')
 with pytest.raises(ValueError):destination(p)
 assert p.read_bytes()==b'original'
 with pytest.raises(ValueError):destination(Path.cwd()/'data/new.sqlite3')
 link=tmp_path/'link.sqlite3';link.symlink_to(p)
 with pytest.raises(ValueError):destination(link)

def test_statistics_has_defined_nearest_rank_percentile():
 assert stats(range(1,11))==dict(mean=5.5,median=5.5,p95=10,maximum=10)

def test_seeded_counts_integrity_and_frontiers(load):
 p,m=load;db=connect(p)
 assert m['counts']['work_items']==120 and m['counts']['timesheet_entries']==90
 assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
 assert not db.execute('pragma foreign_key_check').fetchall()
 for iid in range(1,11):EventRepository(db)._validate_timeline(iid)
 db.close()

def test_skipping_hidden_series_preserves_all_progress(load):
 p,m=load;db=connect(p);s=AnalyticsService(db)
 full=s.build(m['report_date'],m['knowledge_cutoff'],bucket='range')
 partial=s.build(m['report_date'],m['knowledge_cutoff'],bucket='range',include_series=False)
 assert partial['series'] is None
 assert {k:v for k,v in partial.items() if k!='series'}=={k:v for k,v in full.items() if k!='series'}
 db.close()

def test_pagination_retains_global_math_and_preserves_filters(load):
 p,m=load;app=create_app({'DATABASE':str(p),'TESTING':True,'LOCAL_ORIGIN':'http://127.0.0.1:5000'})
 client=app.test_client();q='?report_date='+m['report_date']+'&knowledge_cutoff='+m['knowledge_cutoff']
 a=client.get('/volumes'+q,base_url='http://127.0.0.1:5000')
 b=client.get('/volumes'+q+'&page=2',base_url='http://127.0.0.1:5000')
 assert a.status_code==b.status_code==200
 assert 'по 50 строк' in a.get_data(as_text=True)
 assert a.data!=b.data and 'knowledge_cutoff' in b.get_data(as_text=True)
 assert client.get('/volumes?page=-1',base_url='http://127.0.0.1:5000').status_code==400

def test_sql_journal_paging_and_count_preserve_order_and_scope(load):
 p,m=load;db=connect(p);repo=EventRepository(db);full=repo.list_events('fact')
 assert len(full)>100
 assert repo.list_events('fact',limit=50,offset=50)==full[50:100]
 assert repo.list_events('fact',count=True)==len(full)
 assert repo.list_events('fact',filters={'building_id':9999},count=True)==0
 with pytest.raises(ValueError):repo.list_events('fact',limit=101)
 db.close()
