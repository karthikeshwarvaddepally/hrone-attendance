"""CRUD runs on mongomock by default; TEST_MONGO_URI uses real MongoDB.
Mocks cannot prove server-side concurrency, aggregation correctness or plans.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main as m


def millis(day, clock):
    return int(datetime.fromisoformat(day + 'T' + clock).replace(tzinfo=m.IST).timestamp() * 1000)


def employee(code='EMP0001', department='Engineering', joined_on='2026-07-01', **shifts):
    return dict(emp_code=code, name=code, email=code+'@example.com', department=department,
                joined_on=joined_on, **shifts)


@pytest.fixture
def api(monkeypatch):
    uri = os.getenv('TEST_MONGO_URI')
    connection = MongoClient(uri, tz_aware=True) if uri else mongomock.MongoClient(tz_aware=True)
    name = 'hrone_tests_' + uuid4().hex
    database = connection[name]
    monkeypatch.setattr(m, 'client', connection)
    monkeypatch.setattr(m, 'db', database)
    # mongomock has no ping implementation. Other CRUD uses the actual mock.
    if not uri:
        monkeypatch.setattr(database, 'command', lambda *a, **kw: {'ok': 1})
    m.app.state.indexes_ready = False
    with TestClient(m.app) as client:
        try:
            yield client, database
        finally:
            connection.drop_database(name)
    connection.close()


@pytest.fixture
def real_api(api):
    if not os.getenv('TEST_MONGO_URI'):
        pytest.skip('Requires TEST_MONGO_URI pointing to MongoDB 6.0+')
    return api


def seed_log(database, code, day, status='PRESENT', hours=8.0, late=0, half=False, overtime=0):
    database.attendance_logs.insert_one(dict(emp_code=code, date=day, status=status,
        punch_in=None if status in ['ABSENT', 'LEAVE'] else m.instant(millis(day,'09:30:00')),
        punch_out=None, work_hours=hours, late_minutes=late, overtime_minutes=overtime,
        half_day=half, history=[]))


def test_health_and_filtered_employee_pagination(api):
    c, db = api
    assert c.get('/health').status_code == 200
    for code, dept in [('EMP0003','Sales'),('EMP0002','Engineering'),('EMP0001','Engineering')]:
        assert c.post('/employees', json=employee(code,dept)).status_code == 201
    r=c.get('/employees',params={'department':'Engineering','page_size':1}).json()
    assert r['total']==2 and r['items'][0]['emp_code']=='EMP0001'
    assert isinstance(r['items'][0]['created_at'],int) and '_id' not in r['items'][0]
    assert c.get('/employees',params={'page':2,'page_size':1,'department':'Engineering'}).json()['items'][0]['emp_code']=='EMP0002'
    for params in [{'page':0},{'page_size':0},{'page_size':101}]:
        assert c.get('/employees',params=params).status_code==422


@pytest.mark.parametrize('bad',[1783312500,1783312500000.0,'1783312500000',True,None,4102444800001])
def test_strict_timestamps(api,bad):
    c,db=api
    c.post('/employees',json=employee())
    assert c.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':bad}).status_code==422


@pytest.mark.parametrize('changes',[{'emp_code':'BAD'},{'email':'bad'}, {'joined_on':'2026-02-30'},
    {'shift_start':'9:30'},{'shift_start':'09:30','shift_end':'09:30'},{'name':''}])
def test_employee_validation(api,changes):
    c,db=api
    assert c.post('/employees',json={**employee(),**changes}).status_code==422


@pytest.mark.parametrize('clock,late',[('09:40:00',0),('09:40:01',10),('10:15:59',45)])
def test_late_boundary_and_truncation(api,clock,late):
    c,db=api
    c.post('/employees',json=employee())
    ts=millis('2026-07-06',clock)+900
    r=c.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':ts,'work_hours':99})
    assert r.status_code==201
    data=r.json()
    assert data['late_minutes']==late and data['punch_in']==ts//1000*1000
    assert data['work_hours'] is None and 'id' not in data and '_id' not in data
    assert c.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':ts}).status_code==409


def test_errors_and_attendance_pagination(api):
    c,db=api
    assert c.post('/attendance/punch-in',json={'emp_code':'EMP9999'}).status_code==404
    c.post('/employees',json=employee())
    assert c.post('/attendance/punch-in',json={'emp_code':'EMP0001','status':'LEAVE'}).status_code==422
    for code,day in [('EMP0002','2026-07-06'),('EMP0001','2026-07-06'),('EMP0001','2026-07-07')]:
        seed_log(db,code,day)
    # Mock doesn't implement hint for count_documents; exercise query builder
    # here and actual indexed list through the real-db integration test below.
    query,hint=m.attendance_query('EMP0001','2026-07-06','2026-07-07','PRESENT')
    assert hint=='employee_date' and db.attendance_logs.count_documents(query)==2
    for params in [{'date_from':'2026-07-08','date_to':'2026-07-01'}, {'date_from':'2026-02-30'}, {'status':'X'}]:
        assert c.get('/attendance',params=params).status_code==422


@pytest.mark.parametrize('clock,expected',[('18:59:59',0),('19:00:00',30),('19:10:00',40)])
def test_overtime_rule(clock,expected):
    emp=employee(shift_start='09:30',shift_end='18:30')
    pin=m.instant(millis('2026-07-06','09:30:00'))
    pout=m.instant(millis('2026-07-06',clock))
    assert m.derive('2026-07-06',emp,pin,pout)['overtime_minutes']==expected


def test_work_hours_half_up_and_half_day():
    emp=employee(shift_start='09:30',shift_end='18:30')
    pin=m.instant(millis('2026-07-06','09:30:00'))
    assert m.compute_work_hours(pin,pin+timedelta(seconds=16218))==4.51
    assert m.derive('2026-07-06',emp,pin,pin+timedelta(seconds=16182))['half_day'] is False
    assert m.derive('2026-07-06',emp,pin,pin+timedelta(seconds=16181))['half_day'] is True


def test_overnight_early_morning_assignment():
    emp=employee(shift_start='22:00',shift_end='06:00')
    assert m.attendance_date(m.instant(millis('2026-07-07','05:59:59')),emp)=='2026-07-06'
    assert m.attendance_date(m.instant(millis('2026-07-07','06:00:00')),emp)=='2026-07-07'
    pin=m.instant(millis('2026-07-06','21:55:00'))
    pout=m.instant(millis('2026-07-07','06:40:00'))
    values=m.derive('2026-07-06',emp,pin,pout)
    assert values==dict(late_minutes=0,work_hours=8.75,overtime_minutes=40,half_day=False)


def test_correction_audit_and_noop(api):
    c,db=api
    c.post('/employees',json=employee())
    day='2026-07-06'
    c.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':millis(day,'10:05:00')})
    path='/attendance/EMP0001/'+day
    body={'reason':'biometric issue','regularized_by':'hr.admin','punch_in':millis(day,'09:28:00')}
    r=c.patch(path,json=body)
    assert r.status_code==200
    result=r.json()
    assert len(result['history'])==1 and set(result['history'][0]['changes'])=={'punch_in','late_minutes'}
    assert isinstance(result['history'][0]['changes']['punch_in']['from'],int)
    assert c.patch(path,json=body).status_code==422
    r=c.patch(path,json={'reason':'approved leave','regularized_by':'hr.admin','status':'LEAVE'})
    assert r.status_code==200 and r.json()['punch_in'] is None and len(r.json()['history'])==2
    assert c.patch(path,json={'reason':'invalid combination','regularized_by':'hr.admin','status':'ABSENT','punch_in':millis(day,'09:30:00')}).status_code==422
    assert c.patch(path,json={'reason':'no punch stored','regularized_by':'hr.admin','status':'PRESENT'}).status_code==422
    assert c.patch(path,json={'reason':'invalid timestamp','regularized_by':'hr.admin','punch_in':None}).status_code==422


def test_legacy_defaults_and_missing_history(api):
    c,db=api
    c.post('/employees',json=employee())
    seed_log(db,'EMP0001','2026-07-06',late=35)
    db.attendance_logs.update_one({}, {'$unset':{'history':'','half_day':''}})
    r=c.patch('/attendance/EMP0001/2026-07-06',json={'reason':'correct lateness','regularized_by':'hr','punch_in':millis('2026-07-06','09:28:00')})
    assert r.status_code==200 and len(r.json()['history'])==1 and r.json()['half_day'] is False


def test_explain_required_parameters(api):
    c,db=api
    for endpoint in ['employee_monthly','department_summary','late_leaderboard','department_trend','bogus']:
        assert c.get('/admin/explain/'+endpoint).status_code==422


def test_health_failure(api,monkeypatch):
    c,db=api
    from pymongo.errors import ConnectionFailure
    def unavailable(*args,**kwargs):
        raise ConnectionFailure('test outage')
    monkeypatch.setattr(db,'command',unavailable)
    assert c.get('/health').status_code==503


# Tests below use the real MongoDB server, including aggregation/window stages.
def test_punchout_and_overnight(real_api):
    c,db=real_api
    c.post('/employees',json=employee(shift_start='22:00',shift_end='06:00'))
    pin=millis('2026-07-06','21:55:00');pout=millis('2026-07-07','06:40:00')
    c.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':pin})
    assert c.post('/attendance/punch-out',json={'emp_code':'EMP0001','punched_at':pin}).status_code==422
    db.attendance_logs.update_one({}, {'$set':{'late_minutes':17}})
    r=c.post('/attendance/punch-out',json={'emp_code':'EMP0001','punched_at':pout})
    assert r.status_code==200
    result=r.json()
    assert result['work_hours']==8.75 and result['overtime_minutes']==40 and result['date']=='2026-07-06'
    assert result['late_minutes']==17 and result['history']==[]
    assert c.post('/attendance/punch-out',json={'emp_code':'EMP0001','punched_at':pout}).status_code==409


def test_parallel_creates_punches_and_corrections(real_api):
    c,db=real_api
    def parallel(action,n=8):
        barrier=Barrier(n)
        def run(i):
            barrier.wait()
            return action(i)
        with ThreadPoolExecutor(max_workers=n) as pool:
            return list(pool.map(run,range(n)))
    results=parallel(lambda _:c.post('/employees',json=employee()).status_code)
    assert results.count(201)==1 and results.count(409)==7
    pin=millis('2026-07-06','09:30:00');pout=millis('2026-07-06','18:30:00')
    results=parallel(lambda _:c.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':pin}).status_code)
    assert results.count(201)==1 and results.count(409)==7
    results=parallel(lambda _:c.post('/attendance/punch-out',json={'emp_code':'EMP0001','punched_at':pout}).status_code)
    assert results.count(200)==1 and results.count(409)==7
    results=parallel(lambda i:c.patch('/attendance/EMP0001/2026-07-06',json={
        'reason':'parallel correction '+str(i),'regularized_by':'hr','punch_in':pin+(i+1)*60000}).status_code)
    assert all(x in (200,409) for x in results)
    assert len(db.attendance_logs.find_one({})['history'])==results.count(200)


def analytics_seed(c,db):
    for code,dept,joined in [('EMP0001','Engineering','2026-07-01'),('EMP0002','Engineering','2026-07-15'),
            ('EMP0003','Engineering','2026-07-01'),('EMP0004','Engineering','2026-08-01'),('EMP0005','Sales','2026-07-01')]:
        assert c.post('/employees',json=employee(code,dept,joined)).status_code==201
    seed_log(db,'EMP0001','2026-07-06',hours=8,late=40)
    seed_log(db,'EMP0001','2026-07-07',hours=4,late=20,half=True)
    seed_log(db,'EMP0001','2026-07-11',hours=6,late=10,overtime=30)
    seed_log(db,'EMP0001','2026-07-08',status='LEAVE',hours=None)
    seed_log(db,'EMP0002','2026-07-15',hours=10,late=70)
    seed_log(db,'EMP0005','2026-07-06',hours=8,late=100)
    seed_log(db,'EMP9999','2026-07-06',hours=8,late=999)


def test_monthly_summary_and_ties(real_api):
    c,db=real_api
    analytics_seed(c,db)
    r=c.get('/analytics/employees/EMP0001/monthly',params={'month':'2026-07'})
    assert r.status_code==200
    assert r.json()==dict(emp_code='EMP0001',month='2026-07',working_days=23,present_days=1.5,
        leave_days=1,late_count=3,total_late_minutes=70,total_overtime_minutes=30,attendance_pct=6.52)
    joiner=c.get('/analytics/employees/EMP0002/monthly',params={'month':'2026-07'}).json()
    assert joiner['working_days']==13
    future=c.get('/analytics/employees/EMP0004/monthly',params={'month':'2026-07'}).json()
    assert future['working_days']==0 and future['attendance_pct'] is None
    zero=c.get('/analytics/employees/EMP0003/monthly',params={'month':'2026-07'}).json()
    assert zero['present_days']==0 and zero['working_days']==23
    assert c.get('/analytics/employees/EMP9998/monthly',params={'month':'2026-07'}).status_code==404
    rows=c.get('/analytics/departments/summary',params={'month':'2026-07','department':'Engineering'}).json()['items']
    assert rows==[dict(department='Engineering',headcount=3,present_days=2.5,avg_work_hours=7.0,
        late_count=4,total_late_minutes=140,leave_count=1,on_duty_count=0)]
    rows=c.get('/analytics/leaderboard/late',params={'month':'2026-07','limit':2}).json()['items']
    assert [(x['emp_code'],x['rank']) for x in rows]==[('EMP0005',1),('EMP0001',2),('EMP0002',2)]
    rows=c.get('/analytics/leaderboard/late',params={'month':'2026-07','limit':1,'department':'Engineering'}).json()['items']
    assert [x['rank'] for x in rows]==[1,1]


def test_trend_gaps_weekends_and_joiners(real_api):
    c,db=real_api
    analytics_seed(c,db)
    rows=c.get('/analytics/departments/Engineering/trend',params={'from':'2026-07-06','to':'2026-07-15'}).json()['items']
    assert len(rows)==10
    assert rows[0]['attendance_rate']==0.5 and rows[1]['attendance_rate']==0.25
    assert rows[1]['moving_avg_7d']==0.375 and rows[2]['attendance_rate']==0
    assert rows[5]['is_working_day'] is False and rows[5]['present_count']==1 and rows[5]['attendance_rate'] is None
    assert rows[-1]['headcount']==3 and rows[-1]['attendance_rate']==0.3333
    empty=c.get('/analytics/departments/Engineering/trend',params={'from':'2026-06-01','to':'2026-06-03'}).json()['items']
    assert len(empty)==3 and all(x['headcount']==0 and x['moving_avg_7d'] is None for x in empty)
    no_logs=c.get('/analytics/departments/Engineering/trend',params={'from':'2026-07-20','to':'2026-07-20'}).json()['items']
    assert len(no_logs)==1 and no_logs[0]['attendance_rate']==0
    assert c.get('/analytics/departments/X/trend',params={'from':'2026-07-01','to':'2026-07-01'}).status_code==404
    assert c.get('/analytics/departments/Engineering/trend',params={'from':'2026-01-01','to':'2026-04-03'}).status_code==422


def test_half_up_aggregation_rounding(real_api):
    c,db=real_api
    c.post('/employees',json=employee())
    seed_log(db,'EMP0001','2026-07-06',hours=8.42)
    seed_log(db,'EMP0001','2026-07-07',hours=8.43)
    result=c.get('/analytics/departments/summary',params={'month':'2026-07'}).json()
    assert result['items'][0]['avg_work_hours']==8.43


def test_list_and_explain_plans(real_api):
    c,db=real_api
    analytics_seed(c,db)
    result=c.get('/attendance',params={'emp_code':'EMP0001','page_size':2}).json()
    assert result['total']==4 and [x['date'] for x in result['items']]==['2026-07-11','2026-07-08']
    checks=[('attendance_list',{}),('attendance_list',{'status':'PRESENT'}),('attendance_list',{'emp_code':'EMP0001'}),
            ('employee_monthly',{'emp_code':'EMP0001','month':'2026-07'}),
            ('department_summary',{'month':'2026-07'}),('department_summary',{'month':'2026-07','department':'Engineering'}),
            ('late_leaderboard',{'month':'2026-07'}),
            ('department_trend',{'department':'Engineering','from':'2026-07-01','to':'2026-07-20'})]
    def stages(value):
        if isinstance(value,dict):
            if 'stage' in value: yield value['stage']
            for child in value.values(): yield from stages(child)
        elif isinstance(value,list):
            for child in value: yield from stages(child)
    for endpoint,params in checks:
        r=c.get('/admin/explain/'+endpoint,params=params)
        assert r.status_code==200,r.text
        names=list(stages(r.json()['explain']))
        assert 'IXSCAN' in names and 'COLLSCAN' not in names,(endpoint,names)
