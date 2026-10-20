from decimal import Decimal
from io import BytesIO
import json
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from app.repositories.project_repository import ProjectRepository
from app.repositories.work_repository import WorkRepository
from app.services.xlsx_export import export_xlsx, SHEETS
from app.time_utils import instant
from test_analytics import school, report, group, CUTOFF
from test_web import webapp, client, get, post, ORIGIN

NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def read_sheet(payload, index):
    with ZipFile(BytesIO(payload)) as z:root=ET.fromstring(z.read('xl/worksheets/sheet'+str(index)+'.xml'))
    rows=[]
    for r in root.findall('s:sheetData/s:row',NS):
        values={}
        for c in r:
            col=re.match('[A-Z]+',c.attrib['r']).group()
            if c.attrib.get('t')=='inlineStr':values[col]=''.join(c.itertext())
            else:values[col]=Decimal(c.find('s:v',NS).text)
        rows.append(values)
    return root,rows


@pytest.mark.parametrize('tab',['summary','volumes','dynamics','deviations','groups','export'])
def test_analytics_sections_open_with_real_school_data(client,school,tab):
    r=get(client,'/analytics/'+tab)
    assert r.status_code==200 and 'Школа' in r.get_data(as_text=True)


def test_summary_shows_correct_65_58_not_average_building_percent(client,school):
    html=get(client,'/analytics').get_data(as_text=True)
    assert 'data-kpi="actual">65,0%' in html and 'data-kpi="confirmed">58,0%' in html
    assert '7,0 п.п.' in html


def test_volumes_and_xlsx_use_identical_frozen_query(client,school):
    path='/analytics/volumes?building_id=1&catalog_id=2&report_date=2026-10-19&knowledge_cutoff='+CUTOFF
    page=get(client,path).get_data(as_text=True)
    assert 'data-value="planned">4000' in page and 'data-value="actual">3000' in page
    r=get(client,'/analytics/export.xlsx?building_id=1&catalog_id=2&report_date=2026-10-19&knowledge_cutoff='+CUTOFF)
    assert r.status_code==200 and 'attachment;' in r.headers['Content-Disposition']
    _,rows=read_sheet(r.data,2)
    assert rows[1]['A']=='ОТД.СТ.02' and rows[1]['C']=='м²'
    assert (rows[1]['D'],rows[1]['E'],rows[1]['F'])==(4000,3000,2800)
    assert rows[1]['O']=='2026-10-19T23:59:59.000000Z'


def test_group_creation_edit_restore_archive_and_history(client,school):
    s=school
    values=dict(name='Шпаклёвка стен',catalog_id=2,unit='м²',discipline='CONSTRUCTION',event_key='web-create')
    assert post(client,'/analytics/groups/new',values).status_code==303
    g=s['groups'].list()[0]['group_id'];path='/analytics/groups/'+str(g)
    edit=dict(values,event_key='web-edit',expected_version=1,exclude_id=[str(s['items'][2])])
    assert post(client,path,edit).status_code==303
    assert report(s,group_id=g,knowledge_cutoff=instant())['group']['planned']==7500
    html=get(client,path+'?group_version=1').get_data(as_text=True)
    assert 'только просмотр' in html and 'Сохранить новую редакцию</button>' not in html
    for action,revision in [('reset',2),('archive',3),('restore',4)]:
        assert post(client,path+'/actions',dict(action=action,expected_version=revision,event_key='web-'+action)).status_code==303
    assert s['groups'].get(g)['version_no']==5 and report(s,group_id=g,knowledge_cutoff=instant())['group']['planned']==10000
    assert post(client,path,edit).status_code==303  # Repeat token returns same operation.
    assert s['groups'].get(g)['version_no']==5


def test_group_forms_csrf_origin_and_workspace_guard(client,school):
    values=dict(name='Группа',catalog_id=2,unit='м²',discipline='CONSTRUCTION',event_key='bad')
    assert post(client,'/analytics/groups/new',values,token=False).status_code==403
    assert post(client,'/analytics/groups/new',values,origin='https://example.com').status_code==403
    assert post(client,'/analytics/groups/new',dict(values,workspace_id=999)).status_code==403


def test_foreign_group_and_filters_rejected_after_project_switch(client,db,school):
    g=group(school)
    p=ProjectRepository(db).create_project('Другой объект','OTHER')
    assert post(client,'/projects/select',dict(project_id=p)).status_code==303
    assert get(client,'/analytics/groups/'+str(g)).status_code==400
    assert get(client,'/analytics?group_id='+str(g)).status_code==400
    assert get(client,'/analytics?building_id=1').status_code==400
    assert get(client,'/analytics?version_id='+str(school['version'])).status_code==400
    assert post(client,'/analytics/groups/'+str(g),dict(name='Подмена',catalog_id=2,unit='м²',event_key='attack',expected_version=1)).status_code==400
    assert '65,0%' not in get(client,'/analytics').get_data(as_text=True)


@pytest.mark.parametrize('query',['building_id=abc','catalog_id=999','floor_id=1&building_id=2','start_date=2026-10-20&end_date=2026-10-19','bucket=year','deviation=late','group_version=1','project_id=999'])
def test_invalid_filters_are_safe_errors(client,school,query):
    r=get(client,'/analytics?'+query)
    assert r.status_code==400 and 'Traceback' not in r.get_data(as_text=True)


def test_reference_norm_ui_and_new_plan_suggestion_are_not_retroactive(client,school,db):
    s=school
    assert post(client,'/catalog/norm',dict(catalog_id=2,reference_labor_norm='0,7')).status_code==303
    assert {r['labor_norm'] for r in s['work'].list_plan_lines(s['version'])}=={'0.5'}
    v=s['work'].create_plan_version('CONSTRUCTION')
    page=get(client,'/volumes?version_id='+str(v)).get_data(as_text=True)
    assert 'name="labor_norm" value="0.7"' in page
    assert post(client,'/catalog/norm',dict(catalog_id=2,reference_labor_norm='0')).status_code==400
    prod=get(client,'/analytics/volumes?productivity_catalog_id=2&headcount=5&shift_hours=8&productivity_norm=0.5').get_data(as_text=True)
    assert 'data-productivity>80 м²/смену' in prod
    assert 'Недостаточно данных' in get(client,'/analytics/volumes').get_data(as_text=True)


def test_xlsx_has_six_sheets_freezes_filters_numeric_and_date_types(school):
    g=group(school);data=report(school,group_id=g);payload=export_xlsx(data)
    with ZipFile(BytesIO(payload)) as z:
        assert z.testzip() is None
        book=ET.fromstring(z.read('xl/workbook.xml'))
        assert [s.attrib['name'] for s in book.find('s:sheets',NS)]==list(SHEETS)
        for i in range(1,7):
            sheet=ET.fromstring(z.read('xl/worksheets/sheet'+str(i)+'.xml'))
            assert sheet.find('s:sheetViews/s:sheetView/s:pane',NS).attrib['topLeftCell']=='A2'
            assert sheet.find('s:autoFilter',NS) is not None and sheet.find('.//s:f',NS) is None
    _,v=read_sheet(payload,2)
    assert [v[1][k] for k in ['D','E','F','G','H','I','J']]==[10000,6500,5800,3500,700,Decimal('.65'),Decimal('.58')]
    _,groups=read_sheet(payload,5)
    assert groups[1]['C']==1 and groups[1]['H']=='1, 2, 3'
    assert isinstance(v[1]['N'],Decimal)  # Native Excel date serial.


@pytest.mark.parametrize('name',['=HYPERLINK("https://example.com")','+SUM(1,2)','@cmd','-1+1','\t=1+1'])
def test_user_text_never_becomes_excel_formula(db,school,name):
    ProjectRepository(db).update_project(1,name,'ШК')
    payload=export_xlsx(report(school))
    with ZipFile(BytesIO(payload)) as z:
        for path in z.namelist():
            if path.startswith('xl/worksheets/'):
                root=ET.fromstring(z.read(path));assert root.find('.//s:f',NS) is None
    _,rows=read_sheet(payload,2)
    assert rows[1]['K']==name.strip()


def test_xlsx_preserves_decimal_xml_and_exact_auxiliary_column(school):
    data=report(school)
    data['automatic'][0]['planned']=Decimal('123456789012345678.123456789')
    payload=export_xlsx(data)
    root,rows=read_sheet(payload,2)
    assert rows[1]['D']==Decimal('123456789012345678.123456789')
    assert any('D2=123456789012345678.123456789' in str(v) for v in rows[1].values())


def test_deviation_filter_export_matches_visible_rows(client,school):
    s=school
    s['events'].add_fact(s['items'][0],'2000','2026-10-12',event_key='more',recorded_at='2026-10-12T12:00:00Z')
    html=get(client,'/analytics/deviations?deviation=overrun').get_data(as_text=True)
    assert html.count('data-analytics-item=')==1
    r=get(client,'/analytics/export.xlsx?deviation=overrun')
    _,rows=read_sheet(r.data,4);assert len(rows)==2 and rows[1]['A']==s['items'][0]


def test_excel_cell_limit_is_reported_without_silent_data_truncation(school):
    data=report(school);data['automatic'][0]['name']='Д'*32768
    with pytest.raises(ValueError,match='32767'):export_xlsx(data)


def test_excel_timestamps_are_native_and_precise_utc_is_also_preserved(school):
    group(school);payload=export_xlsx(report(school))
    summary,rows=read_sheet(payload,1)
    assert summary.find('.//s:c[@r="I2"]',NS).attrib['s']=='5'
    groups,rows=read_sheet(payload,5)
    assert groups.find('.//s:c[@r="D2"]',NS).attrib['s']=='5'
    assert any('2026-10-08T12:00:00.000000Z' in str(value) for value in rows[1].values())
