"""
Employee Attendance & Analytics API (FastAPI + MongoDB).

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is read, but real environment variables win)

Layout of this file: configuration -> time helpers -> business rules (R1-R5) ->
models -> serialisation -> employees -> attendance -> analytics -> explain.
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment

log = logging.getLogger("attendance")

# tz_aware=True makes PyMongo return UTC-aware datetimes instead of naive ones, so
# we never mix naive and aware values in arithmetic.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    emp, logs = db.employees, db.attendance_logs
    # Uniqueness of the business key is enforced by the database.
    emp.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    emp.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
    emp.create_index([("joined_on", ASCENDING)], name="joined_on")
    # One record per (emp_code, date): makes punch-in race-safe, serves PATCH, the
    # monthly summary and the per-employee attendance list.
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique")
    # Serves GET /attendance sort order and every month / date-range analytics scan.
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp")
    logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp")
    # Serves "latest record at or before a punch-out time" for one employee.
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in_desc")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        log.exception("could not create indexes at startup; /health will retry")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_request: Request, _exc: PyMongoError):
    # Never leak driver internals to the client.
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_MS = timedelta(milliseconds=1)
_ONE_SEC = timedelta(seconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // _ONE_MS


def at_ist(day: date, hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=IST)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM strings compare correctly


# --------------------------------------------------------------------------- #
# Business rules R1-R5
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: IST date of the punch-in; an early-morning punch-in on an overnight shift
    belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        h, m = shift_end.split(":")
        if local.time() < time(int(h), int(m)):
            day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, shift_start: str, att_date: date) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since shift start."""
    seconds = (punch_in - at_ist(att_date, shift_start)) // _ONE_SEC
    return seconds // 60 if seconds > 600 else 0


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, att_date: date) -> int:
    """R3: whole minutes after shift end, counted only if >= 30. Overnight shifts end the next day."""
    end_day = att_date + timedelta(days=1) if is_overnight(shift_start, shift_end) else att_date
    minutes = ((punch_out - at_ist(end_day, shift_end)) // _ONE_SEC) // 60
    return minutes if minutes >= 30 else 0


def compute_work(punch_in: datetime, punch_out: datetime) -> tuple[float, bool]:
    """R4 + R5. Rounds half-up with integer arithmetic (no float / banker's rounding).
    hundredths of an hour = seconds / 36; half-up = floor((2 * seconds + 36) / 72)."""
    seconds = (punch_out - punch_in) // _ONE_SEC
    hundredths = (2 * seconds + 36) // 72
    return hundredths / 100, hundredths < 450


def derived_fields(emp: dict, att_date: date, status: str, punch_in, punch_out) -> dict:
    if status not in PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, emp["shift_start"], att_date),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        out["work_hours"], out["half_day"] = compute_work(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], att_date)
    return out


def half_up_div(numerator: int, denominator: int) -> int:
    """round-half-up of numerator / denominator for non-negative integers."""
    return (2 * numerator + denominator) // (2 * denominator)


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def parse_date(value: str, name: str = "date") -> date:
    if not DATE_RE.match(value):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid calendar date")


def month_bounds(month: str) -> tuple[date, date]:
    try:
        first = date(int(month[:4]), int(month[5:7]), 1)
    except ValueError:
        raise HTTPException(422, "month is not valid")
    nxt = date(first.year + 1, 1, 1) if first.month == 12 else date(first.year, first.month + 1, 1)
    return first, nxt - timedelta(days=1)


def weekdays_between(first: date, last: date) -> int:
    n, d = 0, first
    while d <= last:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
# strict=True: floats, strings and booleans are rejected (422). The bounds reject seconds.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=TIME_PATTERN)
    shift_end: str = Field("18:30", pattern=TIME_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serialisation (BSON -> API)
# --------------------------------------------------------------------------- #
def _api_value(v):
    return dt_to_ms(v) if isinstance(v, datetime) else v


def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": _api_value(d.get("created_at")),
    }


def record_out(d: dict) -> dict:
    history = [
        {
            "at": _api_value(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": {k: {"from": _api_value(c.get("from")), "to": _api_value(c.get("to"))}
                        for k, c in h.get("changes", {}).items()},
        }
        for h in d.get("history") or []
    ]
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": _api_value(d.get("punch_in")),
        "punch_out": _api_value(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": history,
    }


PageParam = Annotated[int, Query(ge=1)]
PageSizeParam = Annotated[int, Query(ge=1, le=100)]
MonthParam = Annotated[str, Query(pattern=MONTH_PATTERN)]


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # unique index decides races, no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageParam = 1, page_size: PageSizeParam = 20):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derived_fields(emp, att_date, body.status, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index makes this race-safe
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record that started at or before the punch-out (works across midnight).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"]:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts - rec["punch_in"] > timedelta(hours=24):
        raise HTTPException(422, "punch-out is more than 24 hours after punch-in")
    fields = derived_fields(emp, date.fromisoformat(rec["date"]), rec["status"], rec["punch_in"], ts)
    # The filter on punch_out: None makes this a compare-and-set: only one of two
    # simultaneous punch-outs can match.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_find_args(emp_code, date_from, date_to, status, page, page_size):
    """Filter / sort / paging shared by GET /attendance and its explain."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q, {"date": -1, "emp_code": 1}, (page - 1) * page_size, page_size


def _checked_date(value: Optional[str], name: str) -> Optional[str]:
    if value is not None:
        parse_date(value, name)
    return value


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    _checked_date(date_from, "date_from")
    _checked_date(date_to, "date_to")
    q, sort, skip, limit = attendance_find_args(emp_code, date_from, date_to, status, page, page_size)
    # Without filters an exact count is a full scan; the estimate is O(1) and exact for "all".
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    cur = db.attendance_logs.find(q).sort(list(sort.items())).skip(skip).limit(limit)
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


def date_from_str(s: str) -> date:
    # The PATCH path parameter is called `date`, which shadows the datetime.date class inside
    # that function, so the conversion lives here.
    return date.fromisoformat(s)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date")
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old_status = rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status
    new_in, new_out = old_in, old_out

    if new_status in PRESENCE:
        if body.punch_in is not None:
            new_in = ms_to_dt(body.punch_in)
        if body.punch_out is not None:
            new_out = ms_to_dt(body.punch_out)
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]).isoformat() != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and not (timedelta(0) < new_out - new_in <= timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    else:  # ABSENT / LEAVE clear both punch times
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch times cannot be supplied with ABSENT or LEAVE")
        new_in = new_out = None

    if (new_status, new_in, new_out) == (old_status, old_in, old_out):
        raise HTTPException(422, "the request changes nothing")

    new_values = {
        "status": new_status, "punch_in": new_in, "punch_out": new_out,
        **derived_fields(emp, date_from_str(date), new_status, new_in, new_out),
    }
    old_values = {
        "status": old_status, "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old_values[k], "to": v} for k, v in new_values.items() if old_values[k] != v}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: only apply if the record is still exactly as we read it
    # (same history length and same status / punch times). Otherwise 409, never a lost entry.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "status": old_status,
            "punch_in": old_in,
            "punch_out": old_out,
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
        },
        {"$set": new_values, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
# Per-log facts, computed once and reused by every pipeline. Everything here reads the
# STORED derived values (late_minutes, overtime_minutes, half_day, work_hours); nothing is
# recomputed from punch times. Missing legacy fields count as 0 / false.
# "pw2" is twice the present-day weight of a log (2 = full day, 1 = half day, 0 = not present)
# so all sums stay integers and are halved only at the end.
_IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
_LATE = {"$ifNull": ["$late_minutes", 0]}
_IS_WEEKDAY = {"$not": [{"$in": [
    {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, [1, 7]]}]}  # 1=Sun, 7=Sat
_WEIGHT2 = {"$cond": [{"$ifNull": ["$half_day", False]}, 1, 2]}
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}
LOG_FACTS = {
    "_id": 0,
    "date": 1,
    "pw2": {"$cond": [_IS_PRESENT, _WEIGHT2, 0]},
    "pw2wk": {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _WEIGHT2, 0]},
    "late": _LATE,
    "latec": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]},
    "ot": {"$ifNull": ["$overtime_minutes", 0]},
    "leave": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
    "duty": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
    # hundredths of an hour as an integer, so the mean can be rounded half-up exactly
    "whc": {"$cond": [_HAS_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, None]},
    "whn": {"$cond": [_HAS_HOURS, 1, 0]},
}


def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},  # (emp_code, date) index
        {"$project": LOG_FACTS},
        {"$group": {
            "_id": None,
            "pw2wk": {"$sum": "$pw2wk"},
            "leave": {"$sum": "$leave"},
            "late_count": {"$sum": "$latec"},
            "late_total": {"$sum": "$late"},
            "ot_total": {"$sum": "$ot"},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthParam):
    first, last = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first.isoformat(), last.isoformat())))
    r = rows[0] if rows else {"pw2wk": 0, "leave": 0, "late_count": 0, "late_total": 0, "ot_total": 0}
    # R7: working days run from joined_on for a mid-month joiner (calendar arithmetic, not analytics).
    joined = max(first, date.fromisoformat(emp["joined_on"]))
    working_days = weekdays_between(joined, last) if joined <= last else 0
    pct = None
    if working_days > 0:
        # present_days / working_days * 100 in hundredths = pw2wk * 5000 / working_days
        pct = half_up_div(r["pw2wk"] * 5000, working_days) / 100
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": r["pw2wk"] / 2,
        "leave_days": r["leave"],
        "late_count": r["late_count"],
        "total_late_minutes": r["late_total"],
        "total_overtime_minutes": r["ot_total"],
        "attendance_pct": pct,
    }


def department_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    # Start from employees so that people with no logs still count (R9); the $lookup uses
    # the (emp_code, date) index of attendance_logs.
    match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}, {"$project": LOG_FACTS}],
            "as": "logs",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "pw2wk": {"$sum": {"$sum": "$logs.pw2wk"}},
            "whc": {"$sum": {"$sum": "$logs.whc"}},
            "whn": {"$sum": {"$sum": "$logs.whn"}},
            "late_count": {"$sum": {"$sum": "$logs.latec"}},
            "late_total": {"$sum": {"$sum": "$logs.late"}},
            "leave": {"$sum": {"$sum": "$logs.leave"}},
            "duty": {"$sum": {"$sum": "$logs.duty"}},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthParam, department: Optional[str] = None):
    first, last = month_bounds(month)
    rows = db.employees.aggregate(department_summary_pipeline(first.isoformat(), last.isoformat(), department))
    items = []
    for r in rows:
        # mean over records (not an average of averages), rounded half-up on exact integers
        avg = half_up_div(r["whc"], r["whn"]) / 100 if r["whn"] else None
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": r["pw2wk"] / 2,
            "avg_work_hours": avg,
            "late_count": r["late_count"],
            "total_late_minutes": r["late_total"],
            "leave_count": r["leave"],
            "on_duty_count": r["duty"],
        })
    return {"month": month, "items": items}


def late_leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}, "cnt": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # ranking happens inside the department
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped.
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking, so ties at the cutoff stay
        {"$sort": {"total": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": "$total", "late_count": "$cnt"}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthParam, limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: Optional[str] = None):
    first, last = month_bounds(month)
    items = list(db.attendance_logs.aggregate(
        late_leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)))
    return {"month": month, "items": items}


def department_trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    from_s, to_s = d_from.isoformat(), d_to.isoformat()

    def midnight(d: date) -> datetime:
        return datetime(d.year, d.month, d.day)  # naive datetime is stored by PyMongo as 00:00 UTC

    return [
        {"$match": {"department": department, "joined_on": {"$lte": to_s}}},  # (department, joined_on) index
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": from_s, "$lte": to_s}}}, {"$project": LOG_FACTS}],
            "as": "logs",
        }},
        # One "event" per employee (counts toward headcount from max(joined_on, from)) plus one per log.
        {"$project": {"_id": 0, "ev": {"$concatArrays": [
            [{"d": {"$cond": [{"$gt": ["$joined_on", from_s]}, "$joined_on", from_s]}, "head": 1, "pw2": 0, "late": 0}],
            {"$map": {"input": "$logs", "as": "l",
                      "in": {"d": "$$l.date", "head": 0, "pw2": "$$l.pw2", "late": "$$l.latec"}}},
        ]}}},
        {"$unwind": "$ev"},
        {"$replaceRoot": {"newRoot": "$ev"}},
        {"$group": {"_id": "$d", "head": {"$sum": "$head"}, "pw2": {"$sum": "$pw2"}, "late": {"$sum": "$late"}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # Gap filling: one document per calendar day in [from, to] (upper bound is exclusive).
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day",
                                                "bounds": [midnight(d_from), midnight(d_to) + timedelta(days=1)]}}},
        {"$set": {"head": {"$ifNull": ["$head", 0]}, "pw2": {"$ifNull": ["$pw2", 0]},
                  "late": {"$ifNull": ["$late", 0]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "headcount": {"$sum": "$head", "window": {"documents": ["unbounded", "current"]}}}}},
        {"$set": {"is_wd": {"$not": [{"$in": [{"$dayOfWeek": "$day"}, [1, 7]]}]}}},
        # rate in ten-thousandths, rounded half-up on integers: pw2 / (2 * headcount) * 10000
        {"$set": {"rate4": {"$cond": [
            {"$and": ["$is_wd", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$pw2", 5000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "sum_r": {"$sum": "$rate4", "window": {"documents": [-6, "current"]}},
            "cnt_r": {"$sum": {"$cond": [{"$eq": ["$rate4", None]}, 0, 1]}, "window": {"documents": [-6, "current"]}},
        }}},
        {"$set": {"avg4": {"$cond": [{"$gt": ["$cnt_r", 0]},
                                     {"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$sum_r"]}, "$cnt_r"]},
                                                             {"$multiply": [2, "$cnt_r"]}]}},
                                     None]}}},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": "$is_wd",
            "headcount": 1,
            "present_count": {"$divide": ["$pw2", 2]},
            "late_count": "$late",
            "attendance_rate": {"$cond": [{"$eq": ["$rate4", None]}, None, {"$divide": ["$rate4", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$eq": ["$avg4", None]}, None, {"$divide": ["$avg4", 10000]}]},
        }},
    ]


def check_trend_range(d_from: str, d_to: str) -> tuple[date, date]:
    a, b = parse_date(d_from, "from"), parse_date(d_to, "to")
    if b < a:
        raise HTTPException(422, "to must not be before from")
    if (b - a).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    return a, b


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[str, Query(alias="from")], to: str):
    d_from, d_to = check_trend_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = list(db.employees.aggregate(department_trend_pipeline(department, d_from, d_to)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
ExplainName = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                      "department_trend"]


def _need(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing query parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainName,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    # Each branch builds the query with the same function the real endpoint uses.
    if endpoint == "attendance_list":
        _checked_date(date_from, "date_from")
        _checked_date(date_to, "date_to")
        q, sort, skip, lim = attendance_find_args(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        cmd = {"find": collection, "filter": q, "sort": sort, "skip": skip, "limit": lim}
    elif endpoint == "employee_monthly":
        _need(emp_code=emp_code, month=month)
        first, last = month_bounds(month)
        collection = "attendance_logs"
        cmd = {"aggregate": collection, "cursor": {},
               "pipeline": monthly_pipeline(emp_code, first.isoformat(), last.isoformat())}
    elif endpoint == "department_summary":
        _need(month=month)
        first, last = month_bounds(month)
        collection = "employees"
        cmd = {"aggregate": collection, "cursor": {},
               "pipeline": department_summary_pipeline(first.isoformat(), last.isoformat(), department)}
    elif endpoint == "late_leaderboard":
        _need(month=month)
        first, last = month_bounds(month)
        collection = "attendance_logs"
        cmd = {"aggregate": collection, "cursor": {},
               "pipeline": late_leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)}
    else:  # department_trend
        _need(department=department, **{"from": from_, "to": to})
        d_from, d_to = check_trend_range(from_, to)
        collection = "employees"
        cmd = {"aggregate": collection, "cursor": {}, "pipeline": department_trend_pipeline(department, d_from, d_to)}

    raw = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS)),
    }
