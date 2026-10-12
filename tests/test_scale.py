"""Optional 100k-document smoke/plan check; never uses the application DB."""
import os
from datetime import date, timedelta
from time import perf_counter

import pytest

from app import main as m
from .test_api import api, real_api, employee


@pytest.mark.skipif(os.getenv('RUN_SCALE_TESTS') != '1', reason='Set RUN_SCALE_TESTS=1 and TEST_MONGO_URI')
def test_100k_logs(real_api):
    c, database = real_api
    employees = [{**employee(f'EMP{i:04d}', f'Department{i % 10}'), 'created_at': m.now()}
                 for i in range(1000)]
    database.employees.insert_many(employees)
    for offset in range(100):
        day = (date(2026, 7, 1) + timedelta(days=offset)).isoformat()
        database.attendance_logs.insert_many([
            dict(emp_code=e['emp_code'], date=day, status='PRESENT', punch_in=m.now(),
                 punch_out=None, work_hours=8.0, late_minutes=i % 20, overtime_minutes=0,
                 half_day=False, history=[])
            for i, e in enumerate(employees)
        ])
    assert database.attendance_logs.count_documents({}) == 100000
    # Simulate the grader's pre-seeded DB with only default indexes.
    database.employees.drop_indexes()
    database.attendance_logs.drop_indexes()
    start = perf_counter()
    m.create_indexes()
    assert perf_counter() - start < 20
    calls = [
        ('/attendance', {}),
        ('/analytics/employees/EMP0001/monthly', {'month': '2026-07'}),
        ('/analytics/departments/summary', {'month': '2026-07'}),
        ('/analytics/leaderboard/late', {'month': '2026-07', 'limit': 10}),
        ('/analytics/departments/Department1/trend', {'from': '2026-07-01', 'to': '2026-07-31'}),
    ]
    for path, params in calls:
        start = perf_counter()
        response = c.get(path, params=params)
        duration = perf_counter() - start
        assert response.status_code == 200, response.text
        assert duration < 20, (path, duration)
        print(f'{path}: {duration:.3f}s')
    checks = [
        ('attendance_list', {}),
        ('employee_monthly', {'emp_code': 'EMP0001', 'month': '2026-07'}),
        ('department_summary', {'month': '2026-07'}),
        ('late_leaderboard', {'month': '2026-07'}),
        ('department_trend', {'department': 'Department1', 'from': '2026-07-01', 'to': '2026-07-31'}),
    ]
    def stages(value):
        if isinstance(value, dict):
            if 'stage' in value:
                yield value['stage']
            for child in value.values():
                yield from stages(child)
        elif isinstance(value, list):
            for child in value:
                yield from stages(child)
    for endpoint, params in checks:
        response = c.get('/admin/explain/' + endpoint, params=params)
        assert response.status_code == 200, response.text
        names = list(stages(response.json()['explain']))
        assert 'IXSCAN' in names and 'COLLSCAN' not in names, (endpoint, names)
