"""Employee Attendance & Analytics API.

Run: uvicorn app.main:app --port 8000
"""

import calendar
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def _dt_to_epoch_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise HTTPException(422, "Epoch milliseconds must be an integer")
    if not isinstance(value, datetime):
        return value
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _epoch_ms_to_dt(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, int):
        raise HTTPException(422, "Epoch milliseconds must be an integer")
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _round_half_up(value: float, digits: int = 2) -> float:
    quant = Decimal("1") if digits == 0 else Decimal("0." + ("0" * (digits - 1)) + "1")
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def _is_overnight(shift_start: str, shift_end: str) -> bool:
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    return end_minutes <= start_minutes


def _shift_end_for_record(punch_in_utc: datetime, shift_start: str, shift_end: str) -> datetime:
    local_dt = punch_in_utc.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    overnight = _is_overnight(shift_start, shift_end)
    if overnight and local_dt.time() < time(end_h, end_m):
        record_date = local_dt.date() - timedelta(days=1)
        shift_end_dt = datetime.combine(record_date + timedelta(days=1), time(end_h, end_m), tzinfo=IST)
    else:
        record_date = local_dt.date()
        if overnight:
            shift_end_dt = datetime.combine(record_date + timedelta(days=1), time(end_h, end_m), tzinfo=IST)
        else:
            shift_end_dt = datetime.combine(record_date, time(end_h, end_m), tzinfo=IST)
    return shift_end_dt


def _shift_start_for_record(punch_in_utc: datetime, shift_start: str, shift_end: str) -> datetime:
    local_dt = punch_in_utc.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    overnight = _is_overnight(shift_start, shift_end)
    if overnight and local_dt.time() < time(end_h, end_m):
        record_date = local_dt.date() - timedelta(days=1)
    else:
        record_date = local_dt.date()
    return datetime.combine(record_date, time(start_h, start_m), tzinfo=IST)


def attendance_date_for_punch(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    local_dt = punch_in_utc.astimezone(IST)
    overnight = _is_overnight(shift_start, shift_end)
    end_h, end_m = map(int, shift_end.split(":"))
    if overnight and local_dt.time() < time(end_h, end_m):
        return (local_dt.date() - timedelta(days=1)).isoformat()
    return local_dt.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, shift_end: str) -> int:
    local_dt = punch_in_utc.astimezone(IST)
    shift_start_dt = _shift_start_for_record(punch_in_utc, shift_start, shift_end)
    delta_minutes = int((local_dt - shift_start_dt).total_seconds() // 60)
    return delta_minutes if delta_minutes > 10 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    total_hours = Decimal(str((punch_out_utc - punch_in_utc).total_seconds())) / Decimal("3600")
    return float(total_hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime_minutes(punch_out_utc: datetime, shift_start: str, shift_end: str, punch_in_utc: datetime) -> int:
    shift_end_dt = _shift_end_for_record(punch_in_utc, shift_start, shift_end)
    delta_minutes = int((punch_out_utc.astimezone(IST) - shift_end_dt).total_seconds() // 60)
    return delta_minutes if delta_minutes >= 30 else 0


def month_end_for(month: str) -> str:
    year, mon = map(int, month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-{last_day:02d}"


def working_days_count(joined_on: str, month: str) -> int:
    from_date = date.fromisoformat(max(joined_on, f"{month}-01"))
    to_date = date.fromisoformat(month_end_for(month))
    count = 0
    current = from_date
    while current <= to_date:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def serialize_history_entry(item: dict[str, Any]) -> dict[str, Any]:
    changes = {}
    for key, value in item.get("changes", {}).items():
        if isinstance(value, dict) and {"from", "to"}.issubset(value):
            changes[key] = {
                "from": _dt_to_epoch_ms(value.get("from")),
                "to": _dt_to_epoch_ms(value.get("to")),
            }
        else:
            changes[key] = value
    return {
        "at": _dt_to_epoch_ms(item.get("at")),
        "by": item.get("by"),
        "reason": item.get("reason"),
        "changes": changes,
    }


def serialize_attendance_record(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": _dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": _dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(entry) for entry in doc.get("history", [])],
    }


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": _dt_to_epoch_ms(doc.get("created_at")),
    }


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        date.fromisoformat(v)
        return v


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


@app.on_event("startup")
def startup_indexes() -> None:
    try:
        db.command("ping")
    except Exception:
        pass
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.employees.create_index("department")
    db.attendance_logs.create_index("emp_code")


@app.get("/health")
def health() -> dict[str, str]:
    try:
        db.command("ping")
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate) -> dict[str, Any]:
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(status_code=409, detail="emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        result = db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    created = db.employees.find_one({"_id": result.inserted_id})
    return serialize_employee(created)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_employee(item) for item in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    punched_at = _epoch_ms_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc)
    punched_at = punched_at.replace(microsecond=0)
    record_date = attendance_date_for_punch(punched_at, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, employee["shift_start"], employee["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    created = db.attendance_logs.find_one({"_id": result.inserted_id})
    return serialize_attendance_record(created)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    punched_at = _epoch_ms_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc)
    punched_at = punched_at.replace(microsecond=0)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")
    if punched_at <= record["punch_in"]:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    if punched_at - record["punch_in"] > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punch_out is more than 24 hours after punch_in")

    work_hours = compute_work_hours(record["punch_in"], punched_at)
    overtime_minutes = compute_overtime_minutes(punched_at, employee["shift_start"], employee["shift_end"], record["punch_in"])
    half_day = work_hours < 4.5
    update = {
        "$set": {
            "punch_out": punched_at,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }
    }
    result = db.attendance_logs.update_one({"_id": record["_id"], "punch_out": None}, update)
    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail="Record already punched out")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    if date_from and date_to and date.fromisoformat(date_from) > date.fromisoformat(date_to):
        raise HTTPException(status_code=422, detail="date_from must be less than or equal to date_to")
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size))
    return {
        "items": [serialize_attendance_record(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if current is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    old_status = current.get("status")
    old_punch_in = current.get("punch_in")
    old_punch_out = current.get("punch_out")
    old_work = current.get("work_hours")
    old_late = current.get("late_minutes", 0)
    old_overtime = current.get("overtime_minutes", 0)
    old_half = current.get("half_day", False)
    prior_history = current.get("history", [])

    new_status = body.status if body.status is not None else old_status
    new_punch_in = _epoch_ms_to_dt(body.punch_in) if body.punch_in is not None else old_punch_in
    new_punch_out = _epoch_ms_to_dt(body.punch_out) if body.punch_out is not None else old_punch_out

    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="ABSENT/LEAVE records cannot include punch times")
        new_punch_in = None
        new_punch_out = None
    else:
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence statuses require a punch_in")
        if attendance_date_for_punch(new_punch_in, employee["shift_start"], employee["shift_end"]) != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the attendance date")
        if new_punch_out is not None and new_punch_out <= new_punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if new_punch_out is not None and new_punch_out - new_punch_in > timedelta(hours=24):
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    if new_status in {"ABSENT", "LEAVE"}:
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        final_late_minutes = compute_late_minutes(new_punch_in, employee["shift_start"], employee["shift_end"])
        if new_punch_out is None:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False
        else:
            final_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            final_overtime_minutes = compute_overtime_minutes(new_punch_out, employee["shift_start"], employee["shift_end"], new_punch_in)
            final_half_day = final_work_hours < 4.5
    final_doc = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": final_work_hours,
        "late_minutes": final_late_minutes,
        "overtime_minutes": final_overtime_minutes,
        "half_day": final_half_day,
    }

    changes: dict[str, Any] = {}
    for field_name, old_value, new_value in [
        ("status", old_status, new_status),
        ("punch_in", old_punch_in, new_punch_in),
        ("punch_out", old_punch_out, new_punch_out),
        ("work_hours", old_work, final_work_hours),
        ("late_minutes", old_late, final_late_minutes),
        ("overtime_minutes", old_overtime, final_overtime_minutes),
        ("half_day", old_half, final_half_day),
    ]:
        if old_value != new_value:
            changes[field_name] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(status_code=422, detail="No changes supplied")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {key: {"from": _dt_to_epoch_ms(value["from"]) if isinstance(value["from"], datetime) else value["from"], "to": _dt_to_epoch_ms(value["to"]) if isinstance(value["to"], datetime) else value["to"]} for key, value in changes.items()},
    }

    updated = {**current, **final_doc, "history": prior_history + [history_entry]}
    result = db.attendance_logs.update_one({"_id": current["_id"], "history": prior_history}, {"$set": {"status": new_status, "punch_in": new_punch_in, "punch_out": new_punch_out, "work_hours": final_work_hours, "late_minutes": final_late_minutes, "overtime_minutes": final_overtime_minutes, "half_day": final_half_day, "history": prior_history + [history_entry]}})
    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail="Record changed by another update")
    refreshed = db.attendance_logs.find_one({"_id": current["_id"]})
    return serialize_attendance_record(refreshed)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    month_start = f"{month}-01"
    month_end = month_end_for(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$in": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}}, [1, 2, 3, 4, 5]]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}} 
        }}
    ]
    stats = next(db.attendance_logs.aggregate(pipeline), None)
    present_days = float(stats["present_days"]) if stats else 0.0
    leave_days = stats["leave_days"] if stats else 0
    late_count = stats["late_count"] if stats else 0
    total_late_minutes = stats["total_late_minutes"] if stats else 0
    total_overtime_minutes = stats["total_overtime_minutes"] if stats else 0
    working_days = working_days_count(employee["joined_on"], month)
    attendance_pct = None if working_days == 0 else _round_half_up((present_days / working_days) * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
) -> dict[str, Any]:
    month_start = f"{month}-01"
    month_end = month_end_for(month)
    employee_match: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department
    pipeline = [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$emp_code"]}, {"$gte": ["$date", month_start]}, {"$lte": ["$date", month_end]}]}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$in": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}}, [1, 2, 3, 4, 5]]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                    "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
                    "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$ne": ["$work_hours", None]}]}, 1, 0]}},
                }},
            ],
            "as": "attendance_stats",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": [{"$first": "$attendance_stats.present_days"}, 0]}},
            "late_count": {"$sum": {"$ifNull": [{"$first": "$attendance_stats.late_count"}, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": [{"$first": "$attendance_stats.total_late_minutes"}, 0]}},
            "leave_count": {"$sum": {"$ifNull": [{"$first": "$attendance_stats.leave_count"}, 0]}},
            "on_duty_count": {"$sum": {"$ifNull": [{"$first": "$attendance_stats.on_duty_count"}, 0]}},
            "work_hours_sum": {"$sum": {"$ifNull": [{"$first": "$attendance_stats.work_hours_sum"}, 0]}},
            "work_hours_count": {"$sum": {"$ifNull": [{"$first": "$attendance_stats.work_hours_count"}, 0]}},
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
    ]
    rows = list(db.employees.aggregate(pipeline))
    items = []
    for row in rows:
        avg_work_hours = None
        if row["work_hours_count"]:
            avg_work_hours = _round_half_up(float(row["work_hours_sum"]) / row["work_hours_count"], 2)
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": float(row["present_days"]),
            "avg_work_hours": avg_work_hours,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict[str, Any]:
    emp_match = {}
    if department is not None:
        emp_match["department"] = department
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$emp_code"]}, {"$gte": ["$date", f"{month}-01"]}, {"$lte": ["$date", month_end_for(month)]}]}}},
                {"$group": {"_id": None, "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$match": {"logs.total_late_minutes": {"$gt": 0}}},
        {"$project": {"_id": 0, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": "$logs.total_late_minutes", "late_count": "$logs.late_count"}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    rows = list(db.employees.aggregate(pipeline))
    ranked: list[dict[str, Any]] = []
    current_rank = 0
    previous_total = None
    for idx, row in enumerate(rows, start=1):
        total_late = row["total_late_minutes"]
        if previous_total is None or total_late != previous_total:
            current_rank = idx
            previous_total = total_late
        row["rank"] = current_rank
        ranked.append(row)
    items = [item for item in ranked if item["rank"] <= limit]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
) -> dict[str, Any]:
    if date.fromisoformat(to) < date.fromisoformat(from_):
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (date.fromisoformat(to) - date.fromisoformat(from_)).days + 1 > 92:
        raise HTTPException(status_code=422, detail="trend range must be 92 days or fewer")

    employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not employees:
        raise HTTPException(status_code=404, detail="Department not found")
    employee_codes = [emp["emp_code"] for emp in employees]
    day_cursor = date.fromisoformat(from_)
    last_day = date.fromisoformat(to)
    full_days = []
    while day_cursor <= last_day:
        full_days.append(day_cursor.isoformat())
        day_cursor += timedelta(days=1)

    day_docs = [{"date": d} for d in full_days]
    if not day_docs:
        return {"department": department, "items": []}

    pipeline = [
        {"$documents": day_docs},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$in": ["$emp_code", employee_codes]}, {"$eq": ["$date", "$$day"]}]}}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$in": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}}, [1, 2, 3, 4, 5]]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                }},
            ],
            "as": "logs",
        }},
        {"$set": {
            "present_count": {"$ifNull": [{"$first": "$logs.present_count"}, 0.0]},
            "late_count": {"$ifNull": [{"$first": "$logs.late_count"}, 0]},
            "headcount": {"$sum": [
                {"$map": {"input": employees, "as": "emp", "in": {"$cond": [{"$lte": ["$$emp.joined_on", "$date"]}, 1, 0]}}}
            ]}
        }},
        {"$project": {"_id": 0, "date": 1, "present_count": 1, "late_count": 1, "headcount": 1}},
        {"$sort": {"date": 1}},
    ]
    result = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    items = []
    for row in result:
        date_value = date.fromisoformat(row["date"])
        is_working_day = date_value.weekday() < 5
        headcount = 0
        for emp in employees:
            if emp["joined_on"] <= row["date"]:
                headcount += 1
        attendance_rate = None
        if is_working_day and headcount > 0:
            attendance_rate = _round_half_up(float(row["present_count"]) / headcount, 4)
        if not is_working_day:
            attendance_rate = None
        items.append({
            "date": row["date"],
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": float(row["present_count"]),
            "late_count": row["late_count"],
            "attendance_rate": attendance_rate,
            "moving_avg_7d": None,
        })

    rolling = []
    for idx, row in enumerate(items):
        window = []
        for j in range(max(0, idx - 6), idx + 1):
            value = items[j].get("attendance_rate")
            if value is not None:
                window.append(value)
        row["moving_avg_7d"] = _round_half_up(sum(window) / len(window), 4) if window else None
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
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
) -> dict[str, Any]:
    collection = "attendance_logs"
    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status:
            query["status"] = status
        explain = db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size).explain("executionStats")
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": month_end_for(month)}}},
            {"$group": {"_id": None, "present_days": {"$sum": 1}}}
        ]
        explain = db.attendance_logs.aggregate(pipeline).explain("executionStats")
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = [
            {"$match": {"date": {"$gte": f"{month}-01", "$lte": month_end_for(month)}}},
            {"$group": {"_id": "$status", "count": {"$sum": 1}}},
        ]
        explain = db.attendance_logs.aggregate(pipeline).explain("executionStats")
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = [
            {"$match": {"date": {"$gte": f"{month}-01", "$lte": month_end_for(month)}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {"$sort": {"total_late_minutes": -1}},
            {"$limit": limit},
        ]
        explain = db.attendance_logs.aggregate(pipeline).explain("executionStats")
    else:
        if department is None or from_ is None or to is None:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        if date.fromisoformat(to) < date.fromisoformat(from_):
            raise HTTPException(status_code=422, detail="to must be on or after from")
        pipeline = [
            {"$match": {"date": {"$gte": from_, "$lte": to}, "emp_code": {"$in": [x["emp_code"] for x in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]}}},
            {"$group": {"_id": "$date", "count": {"$sum": 1}}},
        ]
        explain = db.attendance_logs.aggregate(pipeline).explain("executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": explain}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
