"""
Employee Attendance & Analytics API

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import re
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# MongoDB setup
mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
mongo_db_name = os.getenv("MONGO_DB", "attendance_db")

try:
    client = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=2000)
    db = client[mongo_db_name]
except Exception as e:
    db = None

IST = timezone(timedelta(hours=5, minutes=30))


def create_indexes():
    if db is None:
        return
    try:
        # Unique index on employees.emp_code
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        # Index on employees.department
        db.employees.create_index([("department", ASCENDING)])
        
        # Unique compound index on attendance_logs.(emp_code, date)
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        # Query index for listing attendance
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        # Query index for department analytics / filtering by status / dates
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])
        db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)])
    except Exception as e:
        print(f"Warning creating indexes: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #
def round_half_up(val: float, decimals: int) -> float:
    d = Decimal(str(val))
    q = Decimal('10') ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def to_epoch_millis(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_millis(ms: int) -> datetime:
    # returns timezone-aware UTC datetime with whole second precision (R1)
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def compute_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        punch_in_dt = punch_in_dt.astimezone(IST)
    
    punch_in_dt = punch_in_dt.replace(microsecond=0)
    ist_date = punch_in_dt.date()
    
    sh_in, sm_in = map(int, shift_start.split(":"))
    sh_out, sm_out = map(int, shift_end.split(":"))
    
    is_overnight = (sh_out < sh_in) or (sh_out == sh_in and sm_out <= sm_in)
    if is_overnight:
        punch_in_time = punch_in_dt.time()
        shift_end_time = datetime.strptime(shift_end, "%H:%M").time()
        if punch_in_time < shift_end_time:
            ist_date = ist_date - timedelta(days=1)
            
    return ist_date.isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, date_str: str) -> int:
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        punch_in_dt = punch_in_dt.astimezone(IST)
    punch_in_dt = punch_in_dt.replace(microsecond=0)
    
    sh, sm = map(int, shift_start.split(":"))
    d_obj = date.fromisoformat(date_str)
    shift_start_dt = datetime(d_obj.year, d_obj.month, d_obj.day, sh, sm, 0, tzinfo=IST)
    
    elapsed_sec = (punch_in_dt - shift_start_dt).total_seconds()
    if elapsed_sec > 600:  # strictly > 10:00 (600s)
        return int(elapsed_sec // 60)
    return 0


def compute_overtime_minutes(punch_out_dt: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    if punch_out_dt.tzinfo is None:
        punch_out_dt = punch_out_dt.replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        punch_out_dt = punch_out_dt.astimezone(IST)
    punch_out_dt = punch_out_dt.replace(microsecond=0)
    
    sh_in, sm_in = map(int, shift_start.split(":"))
    sh_out, sm_out = map(int, shift_end.split(":"))
    is_overnight = (sh_out < sh_in) or (sh_out == sh_in and sm_out <= sm_in)
    
    d_obj = date.fromisoformat(date_str)
    shift_end_date = d_obj + timedelta(days=1) if is_overnight else d_obj
    shift_end_dt = datetime(shift_end_date.year, shift_end_date.month, shift_end_date.day, sh_out, sm_out, 0, tzinfo=IST)
    
    elapsed_sec = (punch_out_dt - shift_end_dt).total_seconds()
    if elapsed_sec >= 1800:  # >= 30 min
        return int(elapsed_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    p_in = punch_in_dt.replace(microsecond=0)
    p_out = punch_out_dt.replace(microsecond=0)
    sec = (p_out - p_in).total_seconds()
    raw_hours = sec / 3600.0
    return round_half_up(raw_hours, 2)


def is_working_day(date_str: str) -> bool:
    d_obj = date.fromisoformat(date_str)
    return d_obj.weekday() < 5


def format_attendance_doc(doc: dict) -> dict:
    hist_list = []
    for h in doc.get("history", []):
        h_formatted = {
            "at": to_epoch_millis(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {}
        }
        changes_in = h.get("changes", {})
        for k, v in changes_in.items():
            if k in ("punch_in", "punch_out"):
                h_formatted["changes"][k] = {
                    "from": to_epoch_millis(v.get("from")),
                    "to": to_epoch_millis(v.get("to"))
                }
            else:
                h_formatted["changes"][k] = {
                    "from": v.get("from"),
                    "to": v.get("to")
                }
        hist_list.append(h_formatted)
        
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_millis(doc.get("punch_in")),
        "punch_out": to_epoch_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": hist_list
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(...)

    @field_validator("joined_on")
    def validate_joined_on(cls, v):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def check_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


def validate_epoch_ms(val: Optional[int]) -> Optional[int]:
    if val is None:
        return None
    if not isinstance(val, int) or isinstance(val, bool):
        raise HTTPException(422, "Timestamp must be an integer epoch milliseconds")
    if val < 100000000000 or val > 4102444800000:
        raise HTTPException(422, "Timestamp out of valid epoch milliseconds range")
    return val


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    def check_punched_at(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    def check_status(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY for punch-in")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    def check_punched_at(cls, v):
        return validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    def check_timestamps(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    def check_status(cls, v):
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("Invalid status value")
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health", tags=["system"])
def health():
    if db is None:
        raise HTTPException(503, "Database connection not initialized")
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(503, f"Database unavailable: {str(e)}")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    now_dt = datetime.now(timezone.utc)
    doc = body.model_dump()
    doc["created_at"] = now_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = to_epoch_millis(now_dt)
    return doc


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size

    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = []
    for emp in cursor:
        emp["created_at"] = to_epoch_millis(emp.get("created_at"))
        items.append(emp)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punch_in_dt = from_epoch_millis(body.punched_at)
    else:
        punch_in_dt = datetime.now(timezone.utc).replace(microsecond=0)

    att_date = compute_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, f"Employee already punched in for attendance date {att_date}")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punch_out_dt = from_epoch_millis(body.punched_at)
    else:
        punch_out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Find candidate record: punch_in <= punched_at
    candidates = list(
        db.attendance_logs.find(
            {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}}
        ).sort("punch_in", DESCENDING).limit(1)
    )

    if not candidates:
        raise HTTPException(404, "No matching punch-in record found for punch-out time")

    record = candidates[0]
    punch_in_dt = record["punch_in"]

    # Validate elapsed time
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, "punch_out time must be strictly after punch_in time")

    if (punch_out_dt - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, "punch_out time cannot be more than 24 hours after punch_in")

    w_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_mins = compute_overtime_minutes(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])
    h_day = bool(w_hours < 4.50)

    # Atomic update preventing double punch-out
    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": w_hours,
                "overtime_minutes": ot_mins,
                "half_day": h_day,
            }
        },
        return_document=True,
    )

    if res is None:
        raise HTTPException(409, "Attendance record is already punched out")

    return format_attendance_doc(res)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")

    q = {}
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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size

    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str,
    date_str: str = Path(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(404, "Attendance record not found for given date")

    # Determine final target values
    new_status = body.status if body.status is not None else record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch_in and punch_out cannot be set for ABSENT or LEAVE status")
        final_punch_in = None
        final_punch_out = None
    else:
        # Presence status
        if body.punch_in is not None:
            final_punch_in = from_epoch_millis(body.punch_in)
        else:
            final_punch_in = record.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(422, "Presence status requires a valid punch_in time")

        # R1 constraint: punch_in must stay on record's attendance date
        calc_date = compute_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date_str:
            raise HTTPException(422, f"New punch_in belongs to date {calc_date}, not {date_str}")

        if body.punch_out is not None:
            final_punch_out = from_epoch_millis(body.punch_out)
        else:
            final_punch_out = record.get("punch_out")

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(422, "punch_out must be strictly after punch_in")
            if (final_punch_out - final_punch_in).total_seconds() > 86400:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        final_late_minutes = compute_late_minutes(final_punch_in, emp["shift_start"], date_str)
        if final_punch_out is not None:
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime_minutes(
                final_punch_out, emp["shift_start"], emp["shift_end"], date_str
            )
            final_half_day = bool(final_work_hours < 4.50)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

    # Compare changes
    changes = {}

    def check_change(field_name, old_val, new_val):
        if old_val != new_val:
            changes[field_name] = {"from": old_val, "to": new_val}

    check_change("status", record.get("status"), new_status)
    check_change("punch_in", record.get("punch_in"), final_punch_in)
    check_change("punch_out", record.get("punch_out"), final_punch_out)
    check_change("work_hours", record.get("work_hours"), final_work_hours)
    check_change("late_minutes", record.get("late_minutes", 0), final_late_minutes)
    check_change("overtime_minutes", record.get("overtime_minutes", 0), final_overtime_minutes)
    check_change("half_day", record.get("half_day", False), final_half_day)

    if not changes:
        raise HTTPException(422, "Regularization request did not change any field values")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency check / history append
    curr_history_len = len(record.get("history", []))
    res = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            f"history.{curr_history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )

    if res is None:
        raise HTTPException(409, "Concurrent modification detected on attendance record")

    return format_attendance_doc(res)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, m_num = map(int, month.split("-"))
    # Working days calculation
    if m_num == 12:
        next_m = date(year + 1, 1, 1)
    else:
        next_m = date(year, m_num + 1, 1)
    days_in_m = (next_m - timedelta(days=1)).day

    joined_on_dt = date.fromisoformat(emp["joined_on"])
    start_date = max(date(year, m_num, 1), joined_on_dt)
    end_date = date(year, m_num, days_in_m)

    working_days = 0
    if start_date <= end_date:
        curr = start_date
        while curr <= end_date:
            if curr.weekday() < 5:
                working_days += 1
            curr += timedelta(days=1)

    month_prefix = f"^{month}-"
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": month_prefix}}},
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_present",
                                    {"$gte": ["$dow", 2]},
                                    {"$lte": ["$dow", 6]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        res = agg_res[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late = int(res.get("total_late_minutes", 0))
        total_ot = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        raw_pct = (present_days / working_days) * 100.0
        attendance_pct = round_half_up(raw_pct, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    year, m_num = map(int, month.split("-"))
    if m_num == 12:
        last_day = f"{year}-12-31"
    else:
        next_m = date(year, m_num + 1, 1) - timedelta(days=1)
        last_day = next_m.isoformat()

    emp_match = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    month_prefix = f"^{month}-"

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"e_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$e_code"]},
                                    {"$regexMatch": {"input": "$date", "regex": month_prefix}},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                            "half_day": 1,
                            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code",
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs.is_present", False]},
                                    {"$gte": ["$logs.dow", 2]},
                                    {"$lte": ["$logs.dow", 6]},
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs.is_present", False]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "all_work_hours": {"$push": "$work_hours_list"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]
    return pipeline


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        dept_name = r["_id"]
        # Flatten all_work_hours
        flat_wh = []
        for emp_wh in r.get("all_work_hours", []):
            if isinstance(emp_wh, list):
                flat_wh.extend(emp_wh)

        if flat_wh:
            avg_wh = round_half_up(sum(flat_wh) / len(flat_wh), 2)
        else:
            avg_wh = None

        items.append({
            "department": dept_name,
            "headcount": r["headcount"],
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, department: Optional[str] = None):
    month_prefix = f"^{month}-"
    match_stage = {
        "date": {"$regex": month_prefix},
        "late_minutes": {"$gt": 0},
    }

    pipeline = [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, department)
    rows = list(db.attendance_logs.aggregate(pipeline))

    # Competition ranking (1, 2, 2, 4)
    ranked_items = []
    current_rank = 1
    for i, row in enumerate(rows):
        if i > 0:
            prev = rows[i - 1]
            if row["total_late_minutes"] == prev["total_late_minutes"]:
                rank = ranked_items[-1]["rank"]
            else:
                rank = i + 1
        else:
            rank = 1

        if rank > limit:
            break

        ranked_items.append({
            "rank": rank,
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": row["total_late_minutes"],
            "late_count": row["late_count"],
        })

    return {"month": month, "items": ranked_items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str):
    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"e_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$e_code"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "emp_code": 1,
                "joined_on": 1,
                "log_date": "$logs.date",
                "status": "$logs.status",
                "late_minutes": "$logs.late_minutes",
                "half_day": "$logs.half_day",
                "is_present": {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
            }
        },
        {"$densify": {"field": "log_date", "range": {"step": 1, "unit": "day", "bounds": [from_date, (date.fromisoformat(to_date) + timedelta(days=1)).isoformat()]}}},
    ]
    return pipeline


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)

    if d_to < d_from:
        raise HTTPException(422, "to date cannot be before from date")

    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    dept_emps = list(db.employees.find({"department": department}))
    if not dept_emps:
        raise HTTPException(404, "Department not found or has no employees")

    # Fetch logs for department employees in range
    emp_codes = [e["emp_code"] for e in dept_emps]
    logs = list(
        db.attendance_logs.find(
            {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        )
    )

    # Index logs by date
    logs_by_date: Dict[str, List[dict]] = {}
    for log in logs:
        logs_by_date.setdefault(log["date"], []).append(log)

    items = []
    rates = []

    curr_d = d_from
    while curr_d <= d_to:
        d_str = curr_d.isoformat()
        is_work_day = curr_d.weekday() < 5

        headcount = sum(1 for e in dept_emps if e["joined_on"] <= d_str)

        day_logs = logs_by_date.get(d_str, [])
        present_count = 0.0
        late_count = 0

        for l in day_logs:
            if l["status"] in ("PRESENT", "WFH", "ON_DUTY"):
                present_count += 0.5 if l.get("half_day", False) else 1.0
            if l.get("late_minutes", 0) > 0:
                late_count += 1

        if is_work_day and headcount > 0:
            raw_rate = present_count / headcount
            attendance_rate = round_half_up(raw_rate, 4)
        else:
            attendance_rate = None

        rates.append(attendance_rate)

        # 7-day moving average over non-null rates in [max(0, i-6) .. i]
        window_rates = [r for r in rates[max(0, len(rates) - 7) :] if r is not None]
        if window_rates:
            moving_avg_7d = round_half_up(sum(window_rates) / len(window_rates), 4)
        else:
            moving_avg_7d = None

        items.append({
            "date": d_str,
            "is_working_day": is_work_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg_7d,
        })

        curr_d += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #

@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., enum=["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = {}
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
        skip = (page - 1) * page_size
        cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
        exp = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": skip, "limit": page_size}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month parameters are required")
        month_prefix = f"^{month}-"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": month_prefix}}},
            {"$group": {"_id": None, "total": {"$sum": 1}}}
        ]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month parameter is required")
        pipeline = build_department_summary_pipeline(month, department)
        exp = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month parameter is required")
        pipeline = build_late_leaderboard_pipeline(month, department)
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to parameters are required")
        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department})]
        q = {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_date, "$lte": to_date}}
        exp = db.command("explain", {"find": "attendance_logs", "filter": q}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    raise HTTPException(422, "Unsupported endpoint for explain")
