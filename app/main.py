"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv(override=False)

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = frozenset({"PRESENT", "WFH", "ON_DUTY"})
PRESENCE_LIST = list(PRESENCE)
WEEKDAY_MATCH = {
    "$and": [
        {
            "$gte": [
                {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "+05:30"}}},
                2,
            ]
        },
        {
            "$lte": [
                {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "+05:30"}}},
                6,
            ]
        },
    ]
}
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def ensure_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique"
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp_code")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)], name="date_status")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(value: float, ndigits: int) -> float:
    quant = Decimal("1") if ndigits == 0 else Decimal("0." + "0" * (ndigits - 1) + "1")
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def truncate_seconds(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(microsecond=0)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_ist(dt: datetime) -> datetime:
    return to_utc(dt).astimezone(IST)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return int(to_utc(truncate_seconds(dt)).timestamp() * 1000)


def epoch_ms_to_dt(ms: int) -> datetime:
    return truncate_seconds(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


def parse_hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def ist_instant_on_date(date_str: str, hhmm: str) -> datetime:
    d = date.fromisoformat(date_str)
    t = parse_hhmm(hhmm)
    return datetime(d.year, d.month, d.day, t.hour, t.minute, t.second, tzinfo=IST)


def attendance_date_from_punch(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    ts = to_ist(truncate_seconds(punch_in))
    d = ts.date()
    if is_overnight(shift_start, shift_end) and ts.timetz().replace(tzinfo=None) < parse_hhmm(shift_end):
        d = d - timedelta(days=1)
    return d.isoformat()


def punch_on_attendance_date(punch_in: datetime, record_date: str, shift_start: str, shift_end: str) -> bool:
    return attendance_date_from_punch(punch_in, shift_start, shift_end) == record_date


def shift_start_instant(record_date: str, shift_start: str) -> datetime:
    return ist_instant_on_date(record_date, shift_start)


def shift_end_instant(record_date: str, shift_start: str, shift_end: str) -> datetime:
    end = ist_instant_on_date(record_date, shift_end)
    if is_overnight(shift_start, shift_end):
        end = end + timedelta(days=1)
    return end


def compute_late_minutes(punch_in: datetime, record_date: str, shift_start: str, shift_end: str) -> int:
    start = shift_start_instant(record_date, shift_start)
    pin = to_ist(truncate_seconds(punch_in))
    elapsed_sec = (pin - start).total_seconds()
    if elapsed_sec <= 600:
        return 0
    return int(elapsed_sec // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_overtime_minutes(
    punch_out: datetime, record_date: str, shift_start: str, shift_end: str
) -> int:
    end = shift_end_instant(record_date, shift_start, shift_end)
    pout = to_ist(truncate_seconds(punch_out))
    minutes = int((pout - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50


def recompute_derived(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    record_date: str,
    shift_start: str,
    shift_end: str,
) -> dict[str, Any]:
    if status in ("ABSENT", "LEAVE"):
        return {
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    if punch_in is None:
        raise HTTPException(422, "presence status requires punch_in")
    late = compute_late_minutes(punch_in, record_date, shift_start, shift_end)
    if punch_out is None:
        return {
            "punch_in": truncate_seconds(punch_in),
            "punch_out": None,
            "work_hours": None,
            "late_minutes": late,
            "overtime_minutes": 0,
            "half_day": False,
        }
    wh = compute_work_hours(punch_in, punch_out)
    return {
        "punch_in": truncate_seconds(punch_in),
        "punch_out": truncate_seconds(punch_out),
        "work_hours": wh,
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(punch_out, record_date, shift_start, shift_end),
        "half_day": compute_half_day(wh),
    }


def employee_to_api(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = dt_to_epoch_ms(doc.get("created_at"))
    return out


def history_entry_to_api(entry: dict) -> dict:
    changes = {}
    for key, val in entry.get("changes", {}).items():
        if key in ("punch_in", "punch_out"):
            changes[key] = {"from": dt_to_epoch_ms(val.get("from")), "to": dt_to_epoch_ms(val.get("to"))}
        else:
            changes[key] = val
    return {
        "at": dt_to_epoch_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes,
    }


def attendance_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_entry_to_api(h) for h in doc.get("history") or []],
    }


def month_bounds(month: str) -> tuple[str, str, date, date]:
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    if m == 12:
        end = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(y, m + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat(), start, end


def working_days_in_range(start: date, end: date, joined_on: date) -> int:
    d = max(start, joined_on)
    count = 0
    while d <= end:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


def build_attendance_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict:
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
    return q


def attendance_list_cursor(q: dict, page: int, page_size: int):
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return total, cursor


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    punch_out: Optional[int] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = truncate_seconds(datetime.now(timezone.utc))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    items = [
        employee_to_api(d)
        for d in db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = epoch_ms_to_dt(body.punched_at) if body.punched_at else truncate_seconds(datetime.now(timezone.utc))
    d = attendance_date_from_punch(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts, d, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": truncate_seconds(ts),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_to_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = epoch_ms_to_dt(body.punched_at) if body.punched_at else truncate_seconds(datetime.now(timezone.utc))
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        closed = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
            sort=[("punch_in", DESCENDING)],
        )
        if closed and closed.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")
        raise HTTPException(404, "no open punch-in found")
    pin = record["punch_in"]
    if ts <= pin:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - pin).total_seconds() > 86400:
        raise HTTPException(422, "punched_at must be within 24 hours of punch_in")
    derived = recompute_derived(
        record["status"],
        pin,
        ts,
        record["date"],
        emp["shift_start"],
        emp["shift_end"],
    )
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": derived},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out")
    return attendance_to_api(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total, cursor = attendance_list_cursor(q, page, page_size)
    items = [attendance_to_api(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = get_employee_or_404(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "attendance record not found")

    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "cannot set punch times with ABSENT or LEAVE")

    old = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": int(record.get("late_minutes") or 0),
        "overtime_minutes": int(record.get("overtime_minutes") or 0),
        "half_day": bool(record.get("half_day", False)),
    }

    new_status = body.status if body.status is not None else old["status"]
    new_pin = epoch_ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
    new_pout = epoch_ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]

    if new_status in ("ABSENT", "LEAVE"):
        new_pin, new_pout = None, None
    else:
        if new_pin is None:
            raise HTTPException(422, "presence status requires punch_in")
        if not punch_on_attendance_date(new_pin, date, emp["shift_start"], emp["shift_end"]):
            raise HTTPException(422, "punch_in must fall on the record attendance date")
        if new_pout is not None:
            if new_pout <= new_pin:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_pout - new_pin).total_seconds() > 86400:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    derived = recompute_derived(new_status, new_pin, new_pout, date, emp["shift_start"], emp["shift_end"])
    new_state = {"status": new_status, **derived}

    changes: dict[str, Any] = {}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        before = old[key]
        after = new_state[key]
        if before != after:
            changes[key] = {"from": before, "to": after}

    if not changes:
        raise HTTPException(422, "no changes requested")

    history_entry = {
        "at": truncate_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    prev_len = len(record.get("history") or [])
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, prev_len]}},
        {"$set": new_state, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(409, "concurrent regularization conflict")
    return attendance_to_api(result)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = get_employee_or_404(emp_code)
    date_from, date_to, start, end = month_bounds(month)
    joined = date.fromisoformat(emp["joined_on"])
    working_days = working_days_in_range(start, end, joined)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": date_from, "$lte": date_to}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", PRESENCE_LIST]},
                                    WEEKDAY_MATCH,
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]
    agg = list(db.attendance_logs.aggregate(pipeline))
    stats = agg[0] if agg else {}
    present_days = float(stats.get("present_days", 0))
    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    date_from, date_to, start, end = month_bounds(month)
    end_str = end.isoformat()
    emp_match: dict[str, Any] = {"joined_on": {"$lte": end_str}}
    if department:
        emp_match["department"] = department

    employees = list(db.employees.find(emp_match, {"_id": 0, "emp_code": 1, "department": 1, "joined_on": 1}))
    by_dept: dict[str, list] = {}
    for e in employees:
        by_dept.setdefault(e["department"], []).append(e)

    log_pipeline = [
        {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.joined_on": {"$lte": end_str}}},
    ]
    if department:
        log_pipeline.append({"$match": {"emp.department": department}})

    log_pipeline.extend(
        [
            {
                "$group": {
                    "_id": "$emp.department",
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", PRESENCE_LIST]},
                                        WEEKDAY_MATCH,
                                    ]
                                },
                                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                                0,
                            ]
                        }
                    },
                    "work_hours_sum": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", PRESENCE_LIST]},
                                        {"$ne": ["$work_hours", None]},
                                    ]
                                },
                                "$work_hours",
                                0,
                            ]
                        }
                    },
                    "work_hours_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", PRESENCE_LIST]},
                                        {"$ne": ["$work_hours", None]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }
            },
            {"$sort": {"_id": 1}},
        ]
    )
    log_stats = {r["_id"]: r for r in db.attendance_logs.aggregate(log_pipeline)}

    items = []
    for dept in sorted(by_dept.keys()):
        headcount = len(by_dept[dept])
        if headcount == 0:
            continue
        stats = log_stats.get(dept, {})
        wh_count = stats.get("work_hours_count", 0)
        avg_wh = None
        if wh_count:
            avg_wh = round_half_up(stats["work_hours_sum"] / wh_count, 2)
        items.append(
            {
                "department": dept,
                "headcount": headcount,
                "present_days": float(stats.get("present_days", 0)),
                "avg_work_hours": avg_wh,
                "late_count": int(stats.get("late_count", 0)),
                "total_late_minutes": int(stats.get("total_late_minutes", 0)),
                "leave_count": int(stats.get("leave_count", 0)),
                "on_duty_count": int(stats.get("on_duty_count", 0)),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    date_from, date_to, _, end = month_bounds(month)
    end_str = end.isoformat()
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.joined_on": {"$lte": end_str}}},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.extend(
        [
            {
                "$group": {
                    "_id": "$emp_code",
                    "name": {"$first": "$emp.name"},
                    "department": {"$first": "$emp.department"},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
        ]
    )
    rows = list(db.attendance_logs.aggregate(pipeline))
    ranked: list[dict] = []
    rank = 0
    prev_total: Optional[int] = None
    for i, row in enumerate(rows, start=1):
        if row["total_late_minutes"] != prev_total:
            rank = i
            prev_total = row["total_late_minutes"]
        if rank <= limit:
            ranked.append(
                {
                    "rank": rank,
                    "emp_code": row["_id"],
                    "name": row["name"],
                    "department": row["department"],
                    "total_late_minutes": int(row["total_late_minutes"]),
                    "late_count": int(row["late_count"]),
                }
            )
    return {"month": month, "items": ranked}


def build_department_trend_pipeline(department: str, from_: str, to: str) -> list[dict]:
    start_date = date.fromisoformat(from_)
    end_date = date.fromisoformat(to)
    start_bound = datetime.combine(start_date, time.min)
    end_bound = datetime.combine(end_date + timedelta(days=1), time.min)
    day_dow = {
        "$dayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "+05:30"}}
    }
    return [
        {"$documents": [{"date": start_bound}]},
        {
            "$densify": {
                "field": "date",
                "range": {"step": 1, "unit": "day", "bounds": [start_bound, end_bound]},
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "date": "$date",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {"$unwind": "$emp"},
                    {"$match": {"emp.department": department}},
                ],
                "as": "logs",
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "staff",
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date",
                "headcount": {"$size": "$staff"},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$log.status", PRESENCE_LIST]},
                                    {
                                        "$cond": [
                                            {"$ifNull": ["$$log.half_day", False]},
                                            0.5,
                                            1.0,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "is_working_day": {
                    "$and": [{"$gte": [day_dow, 2]}, {"$lte": [day_dow, 6]}]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    if to < from_:
        raise HTTPException(422, "to must be >= from")
    if (date.fromisoformat(to) - date.fromisoformat(from_)).days > 91:
        raise HTTPException(422, "date range must not exceed 92 days")

    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")

    rows = list(db.employees.aggregate(build_department_trend_pipeline(department, from_, to)))
    items_raw: list[dict] = []
    for row in rows:
        headcount = int(row["headcount"])
        present = float(row["present_count"])
        rate = None
        if row["is_working_day"] and headcount > 0:
            rate = round_half_up(present / headcount, 4)
        items_raw.append(
            {
                "date": row["date"],
                "is_working_day": bool(row["is_working_day"]),
                "headcount": headcount,
                "present_count": present,
                "late_count": int(row["late_count"]),
                "attendance_rate": rate,
            }
        )

    rates_for_avg: list[Optional[float]] = [r["attendance_rate"] for r in items_raw]
    items = []
    for i, row in enumerate(items_raw):
        window = [rates_for_avg[j] for j in range(max(0, i - 6), i + 1) if rates_for_avg[j] is not None]
        moving = round_half_up(sum(window) / len(window), 4) if window else None
        items.append({**row, "moving_avg_7d": moving})

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status)
        explain = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(
            (page - 1) * page_size
        ).limit(page_size).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        get_employee_or_404(emp_code)
        date_from_m, date_to_m, _, _ = month_bounds(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": date_from_m, "$lte": date_to_m}}}]
        
        explain = db.attendance_logs.aggregate(pipeline).explain()
        
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required"
            )

        date_from_m, date_to_m, _, end = month_bounds(month)
        end_str = end.isoformat()

        pipeline: list[dict] = [
            {
                "$match": {
                    "date": {
                        "$gte": date_from_m,
                        "$lte": date_to_m
                    }
                }
            }
        ]

        if department:
            pipeline.extend([
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "emp_code",
                        "foreignField": "emp_code",
                        "as": "emp"
                    }
                },
                {"$unwind": "$emp"},
                {
                    "$match": {
                        "emp.department": department,
                        "emp.joined_on": {"$lte": end_str}
                    }
                }
            ])

        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "queryPlanner"
        })

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain
        }

    
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")

        date_from_m, date_to_m, _, end = month_bounds(month)
        end_str = end.isoformat()

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": date_from_m,
                        "$lte": date_to_m
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp"
                }
            },
            {"$unwind": "$emp"},
            {
                "$match": {
                    "emp.joined_on": {"$lte": end_str}
                }
            }
        ]

        if department:
            pipeline.append({
                "$match": {"emp.department": department}
            })

        # Add the leaderboard calculations here
        pipeline.extend([
            {
                "$group": {
                    "_id": "$emp_code",
                    "employee_name": {"$first": "$emp.name"},
                    "department": {"$first": "$emp.department"},
                    "total_late_minutes": {
                        "$sum": "$late_minutes"
                    },
                    "late_days": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$late_minutes", 0]},
                                1,
                                0
                            ]
                        }
                    }
                }
            },
            {"$sort": {"total_late_minutes": -1}},
            {"$limit": 10}
        ])

        # Execute the aggregation and return the actual results
        results = list(db.attendance_logs.aggregate(pipeline))

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "leaderboard": results
        }


 
    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required"
            )

        # Validate date format
        try:
            start_date = date.fromisoformat(from_)
            end_date = date.fromisoformat(to)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="from and to must be valid dates in YYYY-MM-DD format"
            )

        # Validate date range
        if end_date < start_date:
            raise HTTPException(
                status_code=422,
                detail="to must be greater than or equal to from"
            )

        if (end_date - start_date).days > 91:
            raise HTTPException(
                status_code=422,
                detail="date range must not exceed 92 days"
            )

        try:
            # Build the department trend pipeline
            pipeline = build_department_trend_pipeline(
                department,
                start_date.isoformat(),
                end_date.isoformat()
            )

            results = list(db.aggregate(pipeline))

            return {
                "endpoint": endpoint,
                "department": department,
                "from": start_date.isoformat(),
                "to": end_date.isoformat(),
                "total_days": len(results),
                "items": results
            }

        except HTTPException:
            raise

        except Exception as exc:
            import traceback
            traceback.print_exc()

            raise HTTPException(
                status_code=500,
                detail=f"{type(exc).__name__}: {str(exc)}"
            )



    raise HTTPException(422, "unknown endpoint")


