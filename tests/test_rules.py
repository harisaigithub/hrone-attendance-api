"""Pure-logic tests: no MongoDB needed.  Run:  python -m pytest -q tests/test_rules.py"""
from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from app.main import (IST, app, attendance_date, compute_late_minutes, compute_overtime, compute_work,
                      dt_to_ms, half_up_div, ms_to_dt, weekdays_between)

D = date(2026, 7, 6)  # a Monday


def ist(h, m, s=0, day=6):
    return datetime(2026, 7, day, h, m, s, tzinfo=IST)


def test_late_boundaries_r2():
    assert compute_late_minutes(ist(9, 40, 0), "09:30", D) == 0
    assert compute_late_minutes(ist(9, 40, 1), "09:30", D) == 10
    assert compute_late_minutes(ist(10, 15, 59), "09:30", D) == 45
    assert compute_late_minutes(ist(9, 0), "09:30", D) == 0


def test_overtime_r3():
    assert compute_overtime(ist(19, 0), "09:30", "18:30", D) == 30
    assert compute_overtime(ist(18, 59, 59), "09:30", "18:30", D) == 0
    assert compute_overtime(ist(18, 35), "09:30", "18:30", D) == 0
    # overnight 22:00-06:00: shift end is the next day; 06:40 is 40 min over
    assert compute_overtime(ist(6, 40, day=7), "22:00", "06:00", D) == 40


def test_work_hours_half_up_and_half_day():
    # 4.505 h = 16218 s: half-up gives 4.51 (float round() would give 4.5)
    assert compute_work(ist(9, 0), ist(9, 0).replace(hour=13, minute=30, second=18)) == (4.51, False)
    assert compute_work(ist(9, 0), ist(13, 29, 59)) == (4.5, False)   # 4.4997 -> 4.50, not < 4.50
    assert compute_work(ist(9, 0), ist(13, 29, 20)) == (4.49, True)   # 4.4889 -> 4.49
    assert compute_work(ist(9, 28), ist(18, 35)) == (9.12, False)     # sample record 1
    assert compute_work(ist(9, 30), ist(13, 0)) == (3.5, True)        # sample record 4


def test_attendance_date_overnight_r1():
    # shift 22:00-06:00: 05:30 IST on the 7th belongs to the shift that started on the 6th
    assert attendance_date(ist(5, 30, day=7), "22:00", "06:00") == date(2026, 7, 6)
    assert attendance_date(ist(6, 0, day=7), "22:00", "06:00") == date(2026, 7, 7)
    assert attendance_date(ist(21, 55), "22:00", "06:00") == date(2026, 7, 6)
    # day shift: plain IST date; 00:30 IST is still the same IST day, not the UTC day
    assert attendance_date(ist(0, 30), "09:30", "18:30") == date(2026, 7, 6)


def test_epoch_roundtrip_truncates_to_seconds():
    dt = ms_to_dt(1783312500999)
    assert dt.microsecond == 0 and dt.tzinfo == timezone.utc
    assert dt_to_ms(dt) == 1783312500000


def test_half_up_div_and_weekdays():
    assert half_up_div(5, 2) == 3 and half_up_div(4, 3) == 1 and half_up_div(1, 3) == 0
    assert weekdays_between(date(2026, 7, 1), date(2026, 7, 31)) == 23


# ---- request validation (rejected before any database access) ----------------
c = TestClient(app, raise_server_exceptions=False)


def test_instant_validation_422():
    for bad in (1783312500, 1783312500000.0, "1783312500000", True, 4102444800001):
        r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad})
        assert r.status_code == 422, bad


def test_status_and_params_422():
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422
    assert c.get("/attendance?page=0").status_code == 422
    assert c.get("/attendance?page_size=101").status_code == 422
    assert c.get("/attendance?date_from=2026-07-10&date_to=2026-07-01").status_code == 422
    assert c.get("/attendance?date_from=2026-02-30").status_code == 422
    assert c.get("/analytics/leaderboard/late?month=2026-13").status_code == 422
    assert c.get("/analytics/leaderboard/late?month=2026-07&limit=51").status_code == 422
    assert c.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01").status_code == 422
    assert c.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-06-01").status_code == 422
    assert c.get("/admin/explain/department_trend").status_code == 422
    assert c.get("/admin/explain/nope").status_code == 422


def test_employee_create_422():
    base = dict(emp_code="EMP0001", name="A", email="a@b.co", department="Eng", joined_on="2026-01-05")
    assert c.post("/employees", json={**base, "emp_code": "E1"}).status_code == 422
    assert c.post("/employees", json={**base, "email": "nope"}).status_code == 422
    assert c.post("/employees", json={**base, "joined_on": "2026-02-30"}).status_code == 422
    assert c.post("/employees", json={**base, "shift_start": "09:30", "shift_end": "09:30"}).status_code == 422
    assert c.post("/employees", json={**base, "shift_start": "9:30"}).status_code == 422


def test_patch_body_422():
    url = "/attendance/EMP0001/2026-07-06"
    assert c.patch(url, json={"reason": "abc", "regularized_by": "hr"}).status_code == 422       # reason too short
    assert c.patch(url, json={"reason": "valid reason"}).status_code == 422                       # missing regularized_by
    assert c.patch("/attendance/EMP0001/2026-7-6", json={"reason": "valid reason", "regularized_by": "hr"}).status_code == 422
