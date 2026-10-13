"""Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded; real env vars win)
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False -> real environment variables take priority

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# Instants in the API: strict int ms. Rejects seconds, floats, strings, bools.
Epoch = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
MonthQ = Annotated[str, Query(pattern=MONTH_RE)]


# Startup: indexes

def ensure_indexes() -> None:
    e, a = db.employees, db.attendance_logs
    e.create_index([("emp_code", 1)], unique=True, name="emp_code_uq")
    e.create_index([("department", 1), ("emp_code", 1)], name="dept_emp")
    e.create_index([("joined_on", 1)], name="joined_on")
    # one record per (employee, day): enforces the punch-in race guarantee
    a.create_index([("emp_code", 1), ("date", 1)], unique=True, name="emp_date_uq")
    a.create_index([("date", -1), ("emp_code", 1)], name="date_emp")          # list + leaderboard
    a.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp")
    a.create_index([("emp_code", 1), ("punch_in", -1)], name="emp_punchin")   # punch-out lookup


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# Time + rule helpers  (R1 - R5, R8)

def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    dt = aware(dt)
    return None if dt is None else (dt - EPOCH) // timedelta(milliseconds=1)


def half_up(x, places: int) -> float:
    return float(Decimal(x).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def parse_date(s: str, name: str = "date") -> date:
    if not DATE_RE.match(s):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid date")


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return _hm(shift_end) <= _hm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of punch_in; early-morning punch on an overnight shift -> previous day."""
    local = aware(punch_in).astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end):
        h, m = _hm(shift_end)
        if local.hour * 3600 + local.minute * 60 + local.second < h * 3600 + m * 60:
            d -= timedelta(days=1)
    return d.isoformat()


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    d, (h, m) = date.fromisoformat(date_str), _hm(shift_start)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    d, (h, m) = date.fromisoformat(date_str), _hm(shift_end)
    end = datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
    return end + timedelta(days=1) if is_overnight(shift_start, shift_end) else end


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: late only if strictly > 600 s after shift_start; then floor minutes since shift_start."""
    secs = int((aware(punch_in) - shift_start_dt(date_str, shift_start)).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up (Decimal, not float round)."""
    secs = int((aware(punch_out) - aware(punch_in)).total_seconds())
    return half_up(Decimal(secs) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: floor minutes after shift_end, only if >= 30."""
    secs = int((aware(punch_out) - shift_end_dt(date_str, shift_start, shift_end)).total_seconds())
    mins = secs // 60 if secs > 0 else 0
    return mins if mins >= 30 else 0


def derive(status: str, pin, pout, emp: dict, date_str: str) -> dict:
    """All derived fields for a record (R2 - R5). Absent/leave -> zeros."""
    if status not in PRESENCE or pin is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(pin, emp["shift_start"], date_str)
    if pout is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    wh = compute_work_hours(pin, pout)
    return {
        "work_hours": wh,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(pout, emp["shift_start"], emp["shift_end"], date_str),
        "half_day": wh < 4.5,
    }


# Serialisation (BSON -> API)

def _conv(v):
    return dt_to_ms(v) if isinstance(v, datetime) else v


def ser_history(history) -> list:
    return [
        {
            "at": dt_to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {
                k: {"from": _conv(v.get("from")), "to": _conv(v.get("to"))}
                for k, v in (h.get("changes") or {}).items()
            },
        }
        for h in (history or [])
    ]


def ser_att(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")),
        "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": ser_history(d.get("history")),
    }


def ser_emp(e: dict) -> dict:
    return {
        "emp_code": e["emp_code"], "name": e["name"], "email": e["email"],
        "department": e["department"], "shift_start": e["shift_start"],
        "shift_end": e["shift_end"], "joined_on": e["joined_on"],
        "created_at": dt_to_ms(e.get("created_at")),
    }


# Models

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[Epoch] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[Epoch] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[Epoch] = None
    punch_out: Optional[Epoch] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# Health + employees

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # unique index decides, no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return ser_emp(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [ser_emp(e) for e in cur], "total": total, "page": page, "page_size": page_size}


# Attendance

def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": d, "status": body.status,
        "punch_in": ts, "punch_out": None,
        **derive(body.status, ts, None, emp, d),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) -> exactly one 201
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return ser_att(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = aware(rec["punch_in"])
    if ts <= pin or ts - pin > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    d = derive(rec["status"], pin, ts, emp, rec["date"])
    new = {"punch_out": ts, "work_hours": d["work_hours"],
           "overtime_minutes": d["overtime_minutes"], "half_day": d["half_day"]}
    # atomic: only the first closer matches `punch_out: None`
    out = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": new}, return_document=ReturnDocument.AFTER,
    )
    if not out:
        raise HTTPException(409, "already punched out")
    return ser_att(out)


def build_list_spec(emp_code, date_from, date_to, status, page, page_size) -> dict:
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    q: dict[str, Any] = {}
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
    return {"filter": q, "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size, "limit": page_size}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
    status: Optional[Status] = None, page: Page = 1, page_size: PageSize = 20,
):
    s = build_list_spec(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(s["filter"]) if s["filter"] else db.attendance_logs.estimated_document_count()
    cur = db.attendance_logs.find(s["filter"]).sort(list(s["sort"].items())).skip(s["skip"]).limit(s["limit"])
    return {"items": [ser_att(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: Annotated[str, Path()], body: RegularizeIn):
    parse_date(date)
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    status = body.status or rec["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot carry punch times")
        pin = pout = None
    else:
        pin = ms_to_dt(body.punch_in) if body.punch_in is not None else aware(rec.get("punch_in"))
        if pin is None:
            raise HTTPException(422, "a presence status requires punch_in")
        if attendance_date(pin, emp["shift_start"], emp["shift_end"]) != rec["date"]:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        pout = ms_to_dt(body.punch_out) if body.punch_out is not None else aware(rec.get("punch_out"))
        if pout is not None and (pout <= pin or pout - pin > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    new = {"status": status, "punch_in": pin, "punch_out": pout, **derive(status, pin, pout, emp, rec["date"])}
    old = {
        "status": rec["status"], "punch_in": aware(rec.get("punch_in")), "punch_out": aware(rec.get("punch_out")),
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    hist = rec.get("history")
    flt = {"_id": rec["_id"], "status": rec["status"],
           "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
           "history": {"$size": len(hist)} if isinstance(hist, list) else {"$exists": False}}
    out = db.attendance_logs.find_one_and_update(
        flt, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if not out:
        raise HTTPException(409, "record was modified concurrently, retry")
    return ser_att(out)


# Analytics: shared expressions

WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
# "present2" = 2 x present days, so half days stay integers
PRESENT2 = {"$cond": [{"$and": [{"$in": ["$status", PRESENCE]}, WEEKDAY]},
                      {"$cond": [{"$ifNull": ["$half_day", False]}, 1, 2]}, 0]}
PRESENT2_ANYDAY = {"$cond": [{"$in": ["$status", PRESENCE]},
                             {"$cond": [{"$ifNull": ["$half_day", False]}, 1, 2]}, 0]}
LATE_FLAG = {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
HAS_WH = {"$and": [{"$in": ["$status", PRESENCE]}, {"$isNumber": "$work_hours"}]}
WH_CENTS = {"$cond": [HAS_WH, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}
IS = lambda s: {"$cond": [{"$eq": ["$status", s]}, 1, 0]}  


def month_range(month: str) -> tuple[str, str]:
    try:
        y, m = int(month[:4]), int(month[5:7])
        first = date(y, m, 1)
        last = date(y, m, calendar.monthrange(y, m)[1])
    except (ValueError, IndexError):
        raise HTTPException(422, "month must be YYYY-MM")
    return first.isoformat(), last.isoformat()


def count_weekdays(start: date, end: date) -> int:
    n, d = 0, start
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


# ---- employee monthly ----
def build_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present2": {"$sum": PRESENT2},
            "leave_days": {"$sum": IS("LEAVE")},
            "late_count": {"$sum": LATE_FLAG},
            "total_late_minutes": {"$sum": LATE_MIN},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQ):
    emp = get_employee(emp_code)
    first, last = month_range(month)
    start = max(date.fromisoformat(first), date.fromisoformat(emp["joined_on"]))
    end = date.fromisoformat(last)
    working = count_weekdays(start, end) if start <= end else 0
    row = next(iter(db.attendance_logs.aggregate(build_monthly_pipeline(emp_code, month))), {})
    present = Decimal(row.get("present2", 0)) / 2
    pct = half_up(present * 100 / working, 2) if working else None
    return {
        "emp_code": emp_code, "month": month, "working_days": working,
        "present_days": float(present), "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


# ---- department summary (starts from employees so zero-log people count) ----
def build_summary_pipeline(month: str, department: Optional[str]) -> tuple[list, dict]:
    first, last = month_range(month)
    match: dict[str, Any] = {"joined_on": {"$lte": last}}  # R9
    if department:
        match["department"] = department
    hint = {"department": 1, "emp_code": 1} if department else {"joined_on": 1}
    pipeline = [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$project": {"_id": 0, "p2": PRESENT2, "cents": WH_CENTS,
                              "n_wh": {"$cond": [HAS_WH, 1, 0]}, "late": LATE_FLAG,
                              "late_min": LATE_MIN, "leave": IS("LEAVE"), "duty": IS("ON_DUTY")}},
            ],
            "as": "logs",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "p2": {"$sum": {"$sum": "$logs.p2"}},
            "cents": {"$sum": {"$sum": "$logs.cents"}},
            "n_wh": {"$sum": {"$sum": "$logs.n_wh"}},
            "late_count": {"$sum": {"$sum": "$logs.late"}},
            "total_late_minutes": {"$sum": {"$sum": "$logs.late_min"}},
            "leave_count": {"$sum": {"$sum": "$logs.leave"}},
            "on_duty_count": {"$sum": {"$sum": "$logs.duty"}},
        }},
        {"$sort": {"_id": 1}},
    ]
    return pipeline, hint


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQ, department: Optional[str] = None):
    pipeline, hint = build_summary_pipeline(month, department)
    items = []
    for r in db.employees.aggregate(pipeline, hint=hint):
        n = r["n_wh"]
        items.append({
            "department": r["_id"], "headcount": r["headcount"],
            "present_days": float(Decimal(r["p2"]) / 2),
            "avg_work_hours": half_up(Decimal(r["cents"]) / n / 100, 2) if n else None,
            "late_count": r["late_count"], "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"], "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


# ---- late leaderboard (window function: competition ranking) ----
def build_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> tuple[list, dict]:
    first, last = month_range(month)
    p: list[dict] = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees drop out here
    ]
    if department:
        p.append({"$match": {"emp.department": department}})  # rank inside the department only
    p += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return p, {"date": -1, "emp_code": 1}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQ, limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: Optional[str] = None):
    p, hint = build_leaderboard_pipeline(month, limit, department)
    return {"month": month, "items": list(db.attendance_logs.aggregate(p, hint=hint))}


# ---- department trend ($unionWith seed + $densify + $setWindowFields) ----
def build_trend_pipeline(department: str, d_from: str, d_to: str) -> list:
    f, t = parse_date(d_from, "from"), parse_date(d_to, "to")
    if t < f:
        raise HTTPException(422, "to must be >= from")
    if (t - f).days + 1 > 92:
        raise HTTPException(422, "range exceeds 92 days")
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    codes = [e["emp_code"] for e in emps]
    joined = sorted(e["joined_on"] for e in emps)
    lo = datetime(f.year, f.month, f.day, tzinfo=UTC)
    hi = datetime(t.year, t.month, t.day, tzinfo=UTC) + timedelta(days=1)  # $densify upper bound is exclusive
    win = {"documents": [-6, "current"]}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": d_from, "$lte": d_to}}},
        {"$project": {"_id": 0, "date": 1, "w": PRESENT2_ANYDAY, "late": LATE_FLAG}},
        # seed row so the series exists even when the range has no records at all
        {"$unionWith": {"pipeline": [{"$documents": [{"date": d_from, "w": 0, "late": 0}]}]}},
        {"$group": {"_id": "$date", "present2": {"$sum": "$w"}, "late_count": {"$sum": "$late"}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "present2": {"$ifNull": ["$present2", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "dow": {"$isoDayOfWeek": "$d"},
        }},
        {"$addFields": {
            "is_working_day": {"$lte": ["$dow", 5]},
            "headcount": {"$size": {"$filter": {"input": {"$literal": joined}, "as": "j",
                                                 "cond": {"$lte": ["$$j", "$date"]}}}},
        }},
        # rate in 1e-4 units, half-up with integer arithmetic: floor((10000*p2 + hc) / (2*hc))
        {"$addFields": {"rate_int": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": [10000, "$present2"]}, "$headcount"]},
                                    {"$multiply": [2, {"$max": ["$headcount", 1]}]}]}},
            None]}}},
        {"$addFields": {"has_rate": {"$cond": [{"$eq": ["$rate_int", None]}, 0, 1]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "w_sum": {"$sum": "$rate_int", "window": win},
            "w_n": {"$sum": "$has_rate", "window": win},
        }}},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": {"$divide": ["$present2", 2]},
            "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$rate_int", None]}, None, {"$divide": ["$rate_int", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$gt": ["$w_n", 0]},
                {"$divide": [{"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$w_sum"]}, "$w_n"]},
                                                     {"$multiply": [2, "$w_n"]}]}}, 10000]}, None]},
        }},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    p = build_trend_pipeline(department, from_, to)
    return {"department": department, "items": list(db.attendance_logs.aggregate(p))}


# Admin: explain (same builders as the real endpoints)
def _need(**kw):
    missing = [k for k, v in kw.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing required parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
    page: Page = 1, page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        s = build_list_spec(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        inner = {"find": coll, "filter": s["filter"], "sort": s["sort"], "skip": s["skip"], "limit": s["limit"]}
    else:
        hint = None
        if endpoint == "employee_monthly":
            _need(emp_code=emp_code, month=month)
            coll, p = "attendance_logs", build_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            _need(month=month)
            coll = "employees"
            p, hint = build_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            _need(month=month)
            coll = "attendance_logs"
            p, hint = build_leaderboard_pipeline(month, limit, department)
        else:
            _need(department=department, **{"from": from_}, to=to)
            coll, p = "attendance_logs", build_trend_pipeline(department, from_, to)
        inner = {"aggregate": coll, "pipeline": p, "cursor": {}}
        if hint:
            inner["hint"] = hint
    raw = db.command({"explain": inner, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll,
            "explain": json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))}