"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB
"""
import os
import re
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, StrictInt
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_TIMESTAMP_MS = 100_000_000_000
MAX_TIMESTAMP_MS = 4_102_444_800_000
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
ALL_STATUSES = ["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True, name="uq_employee_code")
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)], name="ix_attendance_date_employee"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fail422(message: str):
    raise HTTPException(status_code=422, detail=message)


def parse_date(value: str, field: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        fail422(f"{field} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        fail422(f"{field} must be YYYY-MM-DD")
    return parsed


def parse_month(value: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        fail422("month must be YYYY-MM")
    year, month_num = map(int, value.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    return date(year, month_num, 1), date(year, month_num, last_day)


def parse_clock(value: str, field: str):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value or ""):
        fail422(f"{field} must use HH:MM")
    return tuple(map(int, value.split(":")))


def timestamp_to_utc(value: Optional[int], field: str) -> datetime:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0)
    if not MIN_TIMESTAMP_MS <= value <= MAX_TIMESTAMP_MS:
        fail422(f"{field} must be a valid Unix timestamp in milliseconds")
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)


def ensure_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def round_decimal(value, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def attendance_date_for(punched_at: datetime, shift_start: str, shift_end: str) -> date:
    local = punched_at.astimezone(IST)
    result = local.date()
    if shift_end <= shift_start and local.time().replace(tzinfo=None) < datetime.strptime(shift_end, "%H:%M").time():
        result -= timedelta(days=1)
    return result


def late_minutes_for(punch_in: datetime, attendance_day: date, shift_start: str) -> int:
    h, m = parse_clock(shift_start, "shift_start")
    start = datetime(attendance_day.year, attendance_day.month, attendance_day.day, h, m, tzinfo=IST)
    seconds = (ensure_utc(punch_in).astimezone(IST) - start).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def shift_end_datetime(attendance_day: date, shift_start: str, shift_end: str) -> datetime:
    h, m = parse_clock(shift_end, "shift_end")
    end_day = attendance_day + timedelta(days=1) if shift_end <= shift_start else attendance_day
    return datetime(end_day.year, end_day.month, end_day.day, h, m, tzinfo=IST)


def derived_fields(emp: dict, record_date: str, punch_in: Optional[datetime], punch_out: Optional[datetime]):
    if punch_in is not None:
        late = late_minutes_for(punch_in, parse_date(record_date), emp["shift_start"])
    else:
        late = 0
    if punch_in is not None and punch_out is not None:
        seconds = (ensure_utc(punch_out) - ensure_utc(punch_in)).total_seconds()
        hours = round_decimal(seconds / 3600, 2)
        half_day = hours < 4.50
        scheduled_end = shift_end_datetime(parse_date(record_date), emp["shift_start"], emp["shift_end"])
        overtime_seconds = (ensure_utc(punch_out).astimezone(IST) - scheduled_end).total_seconds()
        overtime = int(overtime_seconds // 60) if overtime_seconds >= 1800 else 0
    else:
        hours, half_day, overtime = None, False, 0
    return {
        "late_minutes": late,
        "work_hours": hours,
        "half_day": half_day,
        "overtime_minutes": overtime,
    }


def json_record(doc: dict) -> dict:
    doc = dict(doc)
    if "_id" in doc:
        doc["id"] = str(doc.pop("_id"))
    return doc


def employee_monthly_pipeline(emp_code: str, month_start: date, month_end: date):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}},
        {"$addFields": {"is_weekday": {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 7]]}]}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", PRESENCE_STATUSES]}, "$is_weekday"]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month: str, month_end: str):
    month_start = f"{month}-01"
    return [
        {"$match": {"joined_on": {"$lte": month_end}}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]},
                {"$gte": ["$date", month_start]},
                {"$lte": ["$date", month_end]},
            ]}}},
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": {"department": "$department", "emp_code": "$emp_code"},
            "department": {"$first": "$department"},
            "present_days": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$logs.status", PRESENCE_STATUSES]},
                {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [1, 7]]}]},
            ]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$logs.status", PRESENCE_STATUSES]},
                {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
            ]}, "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$logs.status", PRESENCE_STATUSES]},
                {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
            ]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(month_start: str, month_end: str, department: Optional[str], limit: int):
    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "rank": 1}},
    ])
    return pipeline


def trend_pipeline(department: str, date_from: str, date_to: str):
    return [
        {"$match": {"department": department, "joined_on": {"$lte": date_to}}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]},
                {"$gte": ["$date", date_from]},
                {"$lte": ["$date", date_to]},
            ]}}},
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$logs.date",
            "present_count": {"$sum": {"$cond": [{"$in": ["$logs.status", PRESENCE_STATUSES]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None


class AttendancePatchIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if not re.fullmatch(r"EMP\d{4,6}", body.emp_code):
        fail422("emp_code must match EMP followed by 4 to 6 digits")
    parse_clock(body.shift_start, "shift_start")
    parse_clock(body.shift_end, "shift_end")
    if body.shift_start == body.shift_end:
        fail422("shift_start and shift_end must be different")
    joined = parse_date(body.joined_on, "joined_on")
    doc = body.model_dump()
    doc["joined_on"] = joined.isoformat()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    punched_at = timestamp_to_utc(body.punched_at, "punched_at")
    day = attendance_date_for(punched_at, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": day.isoformat(), "status": body.status,
        "punch_in": punched_at, "punch_out": None, "work_hours": None,
        "late_minutes": late_minutes_for(punched_at, day, emp["shift_start"]),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Attendance already exists for this employee and date")
    doc["id"] = str(result.inserted_id)
    doc.pop("_id", None)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        fail422("date_from must be less than or equal to date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query, {"_id": 1, "emp_code": 1, "date": 1, "status": 1, "punch_in": 1, "punch_out": 1, "work_hours": 1, "late_minutes": 1, "overtime_minutes": 1, "half_day": 1, "history": 1}).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [json_record(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    punched_at = timestamp_to_utc(body.punched_at, "punched_at")
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}, "punch_out": None},
        sort=[("punch_in", -1)],
    )
    if record is None:
        any_record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}}, sort=[("punch_in", -1)])
        if any_record is not None:
            raise HTTPException(status_code=409, detail="Attendance already has a punch-out")
        raise HTTPException(status_code=404, detail="No punch-in record found")
    punch_in_time = ensure_utc(record["punch_in"])
    seconds = (punched_at - punch_in_time).total_seconds()
    if seconds <= 0 or seconds > 24 * 3600:
        fail422("Punch-out must be after punch-in and within 24 hours")
    derived = derived_fields(emp, record["date"], punch_in_time, punched_at)
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **derived}},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Attendance was already updated")
    record.update({"punch_in": punch_in_time, "punch_out": punched_at, **derived})
    return json_record(record)


@app.patch("/attendance/{emp_code}/{date}")
def patch_attendance(emp_code: str, date: str, body: AttendancePatchIn):
    attendance_day = parse_date(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": attendance_day.isoformat()})
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    supplied = body.model_fields_set
    updates = {}
    changes = {}
    for field in ("status", "punch_in", "punch_out"):
        if field not in supplied:
            continue
        value = getattr(body, field)
        if field == "status":
            if value not in ALL_STATUSES:
                fail422("Invalid status")
        elif value is not None:
            value = timestamp_to_utc(value, field)
        old = ensure_utc(record.get(field)) if field in ("punch_in", "punch_out") else record.get(field)
        if value != old:
            updates[field] = value
            changes[field] = {"from": old, "to": value}

    new_status = updates.get("status", record.get("status"))
    new_in = ensure_utc(updates.get("punch_in", record.get("punch_in")))
    new_out = ensure_utc(updates.get("punch_out", record.get("punch_out")))

    if new_status in ("ABSENT", "LEAVE"):
        if ("punch_in" in supplied and body.punch_in is not None) or ("punch_out" in supplied and body.punch_out is not None):
            fail422("ABSENT/LEAVE cannot have punch times")
        new_in = new_out = None
        updates["punch_in"] = None
        updates["punch_out"] = None
    else:
        if new_in is None:
            fail422("Presence status requires punch_in")
        if attendance_date_for(new_in, emp["shift_start"], emp["shift_end"]) != attendance_day:
            fail422("punch_in must belong to the attendance date")
        if new_out is not None:
            elapsed = (new_out - new_in).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 3600:
                fail422("punch_out must be after punch_in and within 24 hours")

    derived = derived_fields(emp, attendance_day.isoformat(), new_in, new_out)
    updates.update(derived)

    for field, new_value in updates.items():
        old_value = ensure_utc(record.get(field)) if field in ("punch_in", "punch_out") else record.get(field)
        if old_value != new_value:
            changes.setdefault(field, {"from": old_value, "to": new_value})
            changes[field]["to"] = new_value

    if not changes:
        fail422("No changes provided")

    history_entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": "hr.admin",
        "changes": changes,
    }
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "history": record.get("history", [])},
        {"$set": updates, "$push": {"history": history_entry}},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Record changed concurrently; retry")
    return json_record(db.attendance_logs.find_one({"_id": record["_id"]}))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    month_start, month_end = parse_month(month)
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    joined_on = parse_date(employee["joined_on"], "joined_on")
    effective_start = max(month_start, joined_on)
    working_days = sum(1 for i in range((month_end - effective_start).days + 1) if (effective_start + timedelta(days=i)).weekday() < 5)
    results = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month_start, month_end)))
    summary = results[0] if results else {}
    present_days = summary.get("present_days", 0)
    pct = None if working_days == 0 else round_decimal(Decimal(str(present_days)) * Decimal(100) / Decimal(working_days), 4)
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": present_days, "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get("total_late_minutes", 0),
        "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    _, month_end_date = parse_month(month)
    pipeline = department_summary_pipeline(month, month_end_date.isoformat())
    results = list(db.employees.aggregate(pipeline))
    output = []
    for item in results:
        if item.get("headcount", 0) == 0:
            continue
        output.append({
            "department": item["_id"], "headcount": item["headcount"],
            "present_days": item["present_days"],
            "avg_work_hours": round_decimal(Decimal(str(item["work_hours_sum"])) / Decimal(str(item["work_hours_count"])), 2) if item.get("work_hours_count", 0) else None,
            "late_count": item["late_count"], "total_late_minutes": item["total_late_minutes"],
            "leave_count": item["leave_count"], "on_duty_count": item["on_duty_count"],
        })
    return {"month": month, "departments": output}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
):
    month_start, month_end = parse_month(month)
    pipeline = leaderboard_pipeline(month_start.isoformat(), month_end.isoformat(), department, limit)
    return {"month": month, "department": department, "limit": limit, "leaderboard": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str = Query(...), date_to: str = Query(...)):
    start = parse_date(date_from, "date_from")
    end = parse_date(date_to, "date_to")
    if end < start:
        fail422("date_to must be on or after date_from")
    if (end - start).days + 1 > 92:
        fail422("Date range cannot exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = trend_pipeline(department, date_from, date_to)
    daily = {item["_id"]: item for item in db.employees.aggregate(pipeline) if item.get("_id") is not None}
    rows = []
    current = start
    while current <= end:
        day_str = current.isoformat()
        working = current.weekday() < 5
        headcount = db.employees.count_documents({"department": department, "joined_on": {"$lte": day_str}})
        item = daily.get(day_str, {})
        present = item.get("present_count", 0)
        rate = round_decimal(Decimal(str(present)) / Decimal(headcount), 4) if working and headcount > 0 else None
        rows.append({
            "date": day_str, "is_working_day": working, "headcount": headcount,
            "present_count": present, "late_count": item.get("late_count", 0),
            "attendance_rate": rate, "moving_avg_7d": None,
        })
        current += timedelta(days=1)

    for i, row in enumerate(rows):
        rates = [r["attendance_rate"] for r in rows[max(0, i - 6):i + 1] if r["attendance_rate"] is not None]
        if rates:
            row["moving_avg_7d"] = round_decimal(sum(Decimal(str(x)) for x in rates) / Decimal(len(rates)), 4)
    return {"department": department, "date_from": date_from, "date_to": date_to, "trend": rows}



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
    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    skip = (page - 1) * page_size

    if endpoint == "attendance_list":
        if date_from:
            parse_date(date_from, "date_from")
        if date_to:
            parse_date(date_to, "date_to")

        if date_from and date_to and date_from > date_to:
            fail422("date_from must be <= date_to")

        query = {}

        if emp_code is not None:
            query["emp_code"] = emp_code

        if status is not None:
            query["status"] = status

        if date_from or date_to:
            query["date"] = {}

            if date_from:
                query["date"]["$gte"] = date_from

            if date_to:
                query["date"]["$lte"] = date_to

        explain = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            fail422("emp_code and month are required")

        month_start, month_end = parse_month(month)

        if db.employees.find_one({"emp_code": emp_code}) is None:
            raise HTTPException(
                status_code=404,
                detail="Employee not found",
            )

        pipeline = employee_monthly_pipeline(
            emp_code, month_start, month_end
        )

        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            fail422("month is required")

        _, month_end = parse_month(month)

        pipeline = department_summary_pipeline(
            month, month_end.isoformat()
        )

        explain = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            fail422("month is required")

        month_start, month_end = parse_month(month)

        pipeline = leaderboard_pipeline(
            month_start.isoformat(),
            month_end.isoformat(),
            department,
            limit,
        )

        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        collection = "attendance_logs"

    else:
        if not department or not date_from or not date_to:
            fail422(
                "department, date_from and date_to are required"
            )

        start = parse_date(date_from, "date_from")
        end = parse_date(date_to, "date_to")

        if end < start:
            fail422("date_to must be on or after date_from")

        if (end - start).days + 1 > 92:
            fail422("Date range cannot exceed 92 days")

        if db.employees.count_documents(
            {"department": department}
        ) == 0:
            raise HTTPException(
                status_code=404,
                detail="Department not found",
            )

        pipeline = trend_pipeline(
            department, date_from, date_to
        )

        explain = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        collection = "employees"

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain,
    }
