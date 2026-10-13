"""
Integration tests against a REAL MongoDB 6+ (uses a throwaway database, drops it first).

    MONGO_URI=mongodb://localhost:27017 python -m pytest -q tests/test_integration.py

Expected numbers in the analytics tests were calculated by hand from the fixture below.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

os.environ["MONGO_DB"] = "attendance_integration_test"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, client, db  # noqa: E402

IST_OFFSET_MS = 5 * 3600_000 + 30 * 60_000


def ms(y, mo, d, h, mi, s=0):
    """Epoch ms of an IST wall-clock time."""
    utc = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
    return int(utc.timestamp() * 1000) - IST_OFFSET_MS


def dt(y, mo, d, h, mi):
    return datetime.fromtimestamp(ms(y, mo, d, h, mi) / 1000, tz=timezone.utc)


def emp(code, dept, joined, start="09:30", end="18:30"):
    return {"emp_code": code, "name": f"Name {code}", "email": f"{code}@x.io", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined, "created_at": dt(2026, 1, 1, 9, 0)}


def log(code, day, status="PRESENT", wh=None, late=0, ot=0, half=False):
    present = status in ("PRESENT", "WFH", "ON_DUTY")
    return {"emp_code": code, "date": f"2026-07-{day:02d}", "status": status,
            "punch_in": dt(2026, 7, day, 9, 30) if present else None,
            "punch_out": dt(2026, 7, day, 18, 30) if present and wh else None,
            "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}


@pytest.fixture()
def api():
    client.drop_database(os.environ["MONGO_DB"])
    with TestClient(app) as c:  # runs the startup hook that creates the indexes
        yield c
    client.drop_database(os.environ["MONGO_DB"])


@pytest.fixture()
def seeded(api):
    db.employees.insert_many([
        emp("EMP0001", "Eng", "2026-06-01"), emp("EMP0002", "Eng", "2026-07-15"),
        emp("EMP0003", "Eng", "2026-06-01"), emp("EMP0005", "Eng", "2026-01-01"),
        emp("EMP0004", "Ops", "2026-06-01"),
    ])
    db.attendance_logs.insert_many([
        log("EMP0001", 1, wh=9.0, late=20), log("EMP0001", 2, wh=4.0, half=True),
        log("EMP0001", 3, "LEAVE"), log("EMP0001", 4, wh=8.0, late=15, ot=40),        # Sat
        log("EMP0003", 7, wh=8.0, late=20),
        log("EMP0004", 1, "ON_DUTY", wh=8.0, late=20), log("EMP0004", 2, "WFH", wh=7.55),
        log("EMP0004", 6, "ABSENT"),
        log("EMP9999", 1, wh=8.0, late=100),                                          # unknown employee
    ])
    return api


def test_monthly(seeded):
    r = seeded.get("/analytics/employees/EMP0001/monthly?month=2026-07").json()
    assert r == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 1.5,
                 "leave_days": 1, "late_count": 2, "total_late_minutes": 35, "total_overtime_minutes": 40,
                 "attendance_pct": 6.52}
    r = seeded.get("/analytics/employees/EMP0002/monthly?month=2026-07").json()  # joined 15 July
    assert r["working_days"] == 13 and r["present_days"] == 0 and r["attendance_pct"] == 0.0
    assert seeded.get("/analytics/employees/EMP0002/monthly?month=2026-06").json()["attendance_pct"] is None
    assert seeded.get("/analytics/employees/EMP0777/monthly?month=2026-07").status_code == 404


def test_department_summary(seeded):
    items = seeded.get("/analytics/departments/summary?month=2026-07").json()["items"]
    assert items == [
        {"department": "Eng", "headcount": 4, "present_days": 2.5, "avg_work_hours": 7.25, "late_count": 3,
         "total_late_minutes": 55, "leave_count": 1, "on_duty_count": 0},
        {"department": "Ops", "headcount": 1, "present_days": 2.0, "avg_work_hours": 7.78, "late_count": 1,
         "total_late_minutes": 20, "leave_count": 0, "on_duty_count": 1},
    ]
    june = seeded.get("/analytics/departments/summary?month=2026-06").json()["items"]
    assert [i["headcount"] for i in june] == [3, 1]       # EMP0002 had not joined yet
    assert seeded.get("/analytics/departments/summary?month=2026-07&department=Ops").json()["items"][0]["department"] == "Ops"


def test_leaderboard_ties(seeded):
    rows = seeded.get("/analytics/leaderboard/late?month=2026-07&limit=2").json()["items"]
    assert [(r["rank"], r["emp_code"], r["total_late_minutes"]) for r in rows] == [
        (1, "EMP0001", 35), (2, "EMP0003", 20), (2, "EMP0004", 20)]   # tie at the cutoff stays; EMP9999 ignored
    rows = seeded.get("/analytics/leaderboard/late?month=2026-07&department=Ops").json()["items"]
    assert [(r["rank"], r["emp_code"]) for r in rows] == [(1, "EMP0004")]
    assert len(seeded.get("/analytics/leaderboard/late?month=2026-07&limit=3").json()["items"]) == 3


def test_trend(seeded):
    r = seeded.get("/analytics/departments/Eng/trend?from=2026-07-01&to=2026-07-07").json()
    rows = r["items"]
    assert [x["date"] for x in rows] == [f"2026-07-0{d}" for d in range(1, 8)]
    assert [x["is_working_day"] for x in rows] == [True, True, True, False, False, True, True]
    assert [x["headcount"] for x in rows] == [3] * 7
    assert [x["present_count"] for x in rows] == [1, 0.5, 0, 1, 0, 0, 1]
    assert [x["late_count"] for x in rows] == [1, 0, 0, 1, 0, 0, 1]
    assert [x["attendance_rate"] for x in rows] == [0.3333, 0.1667, 0.0, None, None, 0.0, 0.3333]
    assert [x["moving_avg_7d"] for x in rows] == [0.3333, 0.25, 0.1667, 0.1667, 0.1667, 0.125, 0.1667]
    assert seeded.get("/analytics/departments/Nope/trend?from=2026-07-01&to=2026-07-02").status_code == 404


def test_punch_flow_overnight_and_audit(api):
    api.post("/employees", json={"emp_code": "EMP0010", "name": "N", "email": "n@x.io", "department": "Ops",
                                 "shift_start": "22:00", "shift_end": "06:00", "joined_on": "2026-01-01"})
    assert api.post("/employees", json={"emp_code": "EMP0010", "name": "N", "email": "n@x.io",
                                        "department": "Ops", "joined_on": "2026-01-01"}).status_code == 409
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0010", "punched_at": ms(2026, 7, 6, 21, 55)})
    assert r.status_code == 201 and r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 0
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0010", "punched_at": ms(2026, 7, 6, 22, 5)}).status_code == 409
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0010", "punched_at": ms(2026, 7, 7, 6, 40)})
    assert r.status_code == 200 and r.json()["work_hours"] == 8.75 and r.json()["overtime_minutes"] == 40
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0010", "punched_at": ms(2026, 7, 7, 7, 0)}).status_code == 409
    # correction: move punch-in, history grows by exactly one entry holding only changed fields
    r = api.patch("/attendance/EMP0010/2026-07-06", json={"punch_in": ms(2026, 7, 6, 21, 0), "reason": "biometric glitch", "regularized_by": "hr"})
    assert r.status_code == 200 and r.json()["work_hours"] == 9.67 and len(r.json()["history"]) == 1
    assert set(r.json()["history"][0]["changes"]) == {"punch_in", "work_hours"}
    assert api.patch("/attendance/EMP0010/2026-07-06", json={"punch_in": ms(2026, 7, 6, 21, 0), "reason": "same again", "regularized_by": "hr"}).status_code == 422
    assert api.patch("/attendance/EMP0010/2026-07-06", json={"status": "LEAVE", "punch_in": ms(2026, 7, 6, 21, 0), "reason": "bad combo", "regularized_by": "hr"}).status_code == 422
    r = api.patch("/attendance/EMP0010/2026-07-06", json={"status": "LEAVE", "reason": "was on leave", "regularized_by": "hr"})
    assert r.status_code == 200 and r.json()["punch_in"] is None and len(r.json()["history"]) == 2
    assert api.patch("/attendance/EMP0010/2026-07-09", json={"status": "LEAVE", "reason": "no record", "regularized_by": "hr"}).status_code == 404


def test_concurrent_punch_in_and_out(api):
    api.post("/employees", json={"emp_code": "EMP0011", "name": "N", "email": "n@x.io", "department": "Ops", "joined_on": "2026-01-01"})
    body = {"emp_code": "EMP0011", "punched_at": ms(2026, 7, 6, 9, 30)}
    with ThreadPoolExecutor(10) as ex:
        codes = sorted(f.result().status_code for f in [ex.submit(api.post, "/attendance/punch-in", json=body) for _ in range(10)])
    assert codes == [201] + [409] * 9
    out = {"emp_code": "EMP0011", "punched_at": ms(2026, 7, 6, 18, 30)}
    with ThreadPoolExecutor(10) as ex:
        codes = sorted(f.result().status_code for f in [ex.submit(api.post, "/attendance/punch-out", json=out) for _ in range(10)])
    assert codes == [200] + [409] * 9


def _plan_text(node):
    import json
    return json.dumps(node)


@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", ""), ("attendance_list", "?emp_code=EMP0001&date_from=2026-07-01"),
    ("attendance_list", "?status=LEAVE"), ("employee_monthly", "?emp_code=EMP0001&month=2026-07"),
    ("department_summary", "?month=2026-07"), ("late_leaderboard", "?month=2026-07&department=Eng"),
    ("department_trend", "?department=Eng&from=2026-07-01&to=2026-07-31"),
])
def test_explain_uses_index(seeded, endpoint, params):
    r = seeded.get(f"/admin/explain/{endpoint}{params}")
    assert r.status_code == 200, r.text
    text = _plan_text(r.json()["explain"])
    assert "IXSCAN" in text and "COLLSCAN" not in text
