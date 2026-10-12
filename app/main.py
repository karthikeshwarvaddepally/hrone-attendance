"""Attendance API. All application logic lives here, as required by the kit."""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import Decimal128, ObjectId, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Millis = Annotated[StrictInt, Field(ge=100000000000, le=4102444800000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def create_indexes():
    db.employees.create_index("emp_code", unique=True, name="employee_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="department_code")
    db.employees.create_index("joined_on", name="joined_on")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="department_joined")
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)], unique=True, name="employee_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_employee")
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_employee")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="employee_punch")


@asynccontextmanager
async def lifespan(app):
    # A failed DB connection keeps health at 503. Retry index creation on health.
    try:
        create_indexes()
        app.state.indexes_ready = True
    except PyMongoError:
        app.state.indexes_ready = False
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# Validation and time helpers -------------------------------------------------
def fail(message):
    raise HTTPException(422, message)


def parse_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        fail("Date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        fail("Invalid calendar date")


def month_bounds(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        fail("Month must be YYYY-MM")
    try:
        y, m = map(int, value.split("-"))
        first = date(y, m, 1)
        last = date(y, m, calendar.monthrange(y, m)[1])
        return first, last
    except ValueError:
        fail("Invalid calendar month")


def validate_range(start, end):
    a = parse_date(start) if start is not None else None
    b = parse_date(end) if end is not None else None
    if a and b and a > b:
        fail("Start date must not exceed end date")
    return a, b


def now():
    return datetime.now(UTC).replace(microsecond=0)


def instant(value):
    return now() if value is None else EPOCH + timedelta(seconds=value // 1000)


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def epoch_ms(value):
    delta = aware(value) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def json_value(value, hide_internal=True):
    """Convert instants, including nested history; strip internal database keys."""
    if isinstance(value, datetime):
        return epoch_ms(value)
    if isinstance(value, dict):
        return {k: json_value(v, hide_internal) for k, v in value.items()
                if not hide_internal or k not in {"_id", "_version"}}
    if isinstance(value, list):
        return [json_value(v, hide_internal) for v in value]
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, ObjectId):
        return str(value)  # Needed for raw explain bounds, never attendance identity.
    if not hide_internal and not isinstance(value, (str, int, float, bool, type(None))):
        # Atlas may include BSON Timestamp/Binary cluster metadata in explain.
        return json.loads(json_util.dumps(value))
    return value


def attendance_response(doc):
    result = json_value(doc)
    for key, default in [("history", []), ("half_day", False), ("late_minutes", 0),
                         ("overtime_minutes", 0), ("punch_in", None), ("punch_out", None), ("work_hours", None)]:
        result.setdefault(key, default)
    return result


def attendance_date(ts, employee):
    local = aware(ts).astimezone(IST)
    day = local.date()
    if employee["shift_end"] <= employee["shift_start"] and local.strftime("%H:%M") < employee["shift_end"]:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(day, employee):
    start = datetime.fromisoformat(day + "T" + employee["shift_start"]).replace(tzinfo=IST)
    end = datetime.fromisoformat(day + "T" + employee["shift_end"]).replace(tzinfo=IST)
    if employee["shift_end"] <= employee["shift_start"]:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in, start):
    seconds = int((aware(punch_in) - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in, punch_out):
    seconds = int((aware(punch_out) - aware(punch_in)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out, end):
    minutes = int((aware(punch_out) - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def derive(day, employee, punch_in, punch_out):
    start, end = shift_bounds(day, employee)
    if punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        duration = (aware(punch_out) - aware(punch_in)).total_seconds()
        if not 0 < duration <= 86400:
            fail("Punch-out must be after punch-in and within 24 hours")
    hours = None if punch_out is None else compute_work_hours(punch_in, punch_out)
    return {
        "late_minutes": compute_late_minutes(punch_in, start),
        "work_hours": hours,
        "overtime_minutes": 0 if punch_out is None else compute_overtime(punch_out, end),
        "half_day": hours is not None and hours < 4.50,
    }


def employee_or_404(code):
    employee = db.employees.find_one({"emp_code": code})
    if employee is None:
        raise HTTPException(404, "Employee not found")
    return employee


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def validate_calendar(self):
        parse_date(self.joined_on)
        if self.shift_start == self.shift_end:
            fail("Shift start and end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Millis = None  # Omission uses now; explicit null fails validation.
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Millis = None


class CorrectionIn(BaseModel):
    status: Status = None
    punch_in: Millis = None
    punch_out: Millis = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# CRUD and conditional atomic writes -----------------------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
        if not getattr(app.state, "indexes_ready", False):
            create_indexes()
            app.state.indexes_ready = True
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = {**body.model_dump(), "created_at": now()}
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return json_value(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    items = db.employees.find(query).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [json_value(x) for x in items], "total": db.employees.count_documents(query),
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = employee_or_404(body.emp_code)
    ts = instant(body.punched_at)
    day = attendance_date(ts, employee)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status,
           "punch_in": ts, "punch_out": None, "history": [], **derive(day, employee, ts, None)}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already recorded for this date")
    return attendance_response(doc)


def snapshot_filter(record):
    # Version changes on every write. Field guards also protect legacy records.
    query = {"_id": record["_id"], "_version": record.get("_version", {"$exists": False})}
    for key in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        query[key] = record[key] if key in record else {"$exists": False}
    return query


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = employee_or_404(body.emp_code)
    ts = instant(body.punched_at)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)], hint="employee_punch",
    )
    if record is None:
        raise HTTPException(404, "No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    computed = derive(record["date"], employee, record["punch_in"], ts)
    computed.pop("late_minutes")  # Punch-out must preserve stored lateness.
    values = {"punch_out": ts, **computed}
    updated = db.attendance_logs.find_one_and_update(
        snapshot_filter(record), {"$set": values, "$inc": {"_version": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Record changed; retry with current state")
    return attendance_response(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: CorrectionIn):
    parse_date(date)
    employee = employee_or_404(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "Attendance record not found")
    fields = body.model_fields_set
    status = body.status if "status" in fields else record["status"]
    pin = instant(body.punch_in) if "punch_in" in fields else record.get("punch_in")
    pout = instant(body.punch_out) if "punch_out" in fields else record.get("punch_out")
    if status in ["ABSENT", "LEAVE"]:
        if fields & {"punch_in", "punch_out"}:
            fail("Absent/leave corrections cannot supply punch times")
        pin = pout = None
    elif pin is None:
        fail("Presence requires a punch-in")
    if pin is not None:
        pin = aware(pin).replace(microsecond=0)
        if attendance_date(pin, employee) != date:
            fail("Punch-in must stay on the attendance date")
    if pout is not None:
        pout = aware(pout).replace(microsecond=0)
    final = {"status": status, "punch_in": pin, "punch_out": pout, **derive(date, employee, pin, pout)}
    defaults = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    changes = {k: {"from": record.get(k, defaults.get(k)), "to": v}
               for k, v in final.items() if record.get(k, defaults.get(k)) != v}
    if not changes:
        fail("Correction changes nothing")
    entry = {"at": now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        snapshot_filter(record), {"$set": final, "$push": {"history": entry}, "$inc": {"_version": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Record changed; retry with current state")
    return attendance_response(updated)


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    validate_range(date_from, date_to)
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    hint = "employee_date" if emp_code is not None else "status_date_employee" if status is not None else "date_employee"
    return query, hint


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: str | None = None, date_to: str | None = None,
                    status: Status | None = None, page: Page = 1, page_size: PageSize = 20):
    query, hint = attendance_query(emp_code, date_from, date_to, status)
    cursor = db.attendance_logs.find(query).hint(hint).sort([("date", -1), ("emp_code", 1)])
    items = cursor.skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_response(x) for x in items],
            "total": db.attendance_logs.count_documents(query, hint=hint), "page": page, "page_size": page_size}


# Aggregation builders: shared verbatim by analytics and explain ---------------
def mongo_round(expr, places=2):
    # All reported metrics are nonnegative. Decimal arithmetic avoids $round's
    # ties-to-even and binary-float artifacts.
    scale = 10 ** places
    return {"$cond": [{"$eq": [expr, None]}, None, {"$toDouble": {"$divide": [
        {"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, scale]}, Decimal128("0.5")]}}, scale,
    ]}}]}


def mongo_day(value):
    return {"$dateFromString": {"dateString": value, "format": "%Y-%m-%d", "timezone": "UTC"}}


def weekday(expr):
    return {"$and": [{"$gte": [{"$dayOfWeek": expr}, 2]}, {"$lte": [{"$dayOfWeek": expr}, 6]}]}


def present_value(weekdays_only=False):
    conditions = [{"$in": ["$status", PRESENCE]}]
    if weekdays_only:
        conditions.append(weekday(mongo_day("$date")))
    return {"$cond": [{"$and": conditions}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def log_summary_group():
    completed = {"$and": [{"$in": ["$status", PRESENCE]}, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    return {"$group": {
        "_id": None, "present_days": {"$sum": present_value(True)},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "hours_sum": {"$sum": {"$cond": [completed, {"$toDecimal": "$work_hours"}, 0]}},
        "hours_count": {"$sum": {"$cond": [completed, 1, 0]}},
    }}


def logs_lookup(first, last):
    return {"$lookup": {
        "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "stats",
        "pipeline": [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}, log_summary_group()],
    }}


def stats_field(key):
    return {"$ifNull": [{"$arrayElemAt": ["$stats." + key, 0]}, 0]}


def working_days(first, last):
    # Calendar generation and join-date exclusion both execute inside MongoDB.
    return {"$size": {"$filter": {
        "input": {"$map": {"input": {"$range": [0, (last - first).days + 1]}, "as": "offset",
                            "in": {"$dateAdd": {"startDate": mongo_day(first.isoformat()), "unit": "day", "amount": "$$offset"}}}},
        "as": "day", "cond": {"$and": [weekday("$$day"), {"$gte": ["$$day", mongo_day("$joined_on")]}]},
    }}}


def monthly_pipeline(code, month):
    first, last = month_bounds(month)
    return "employees", [
        {"$match": {"emp_code": code}}, logs_lookup(first, last),
        {"$set": {"working_days": working_days(first, last),
                  **{k: stats_field(k) for k in ["present_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes"]}}},
        {"$project": {"_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": 1,
                      "present_days": 1, "leave_days": 1, "late_count": 1, "total_late_minutes": 1, "total_overtime_minutes": 1,
                      "attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]},
                          mongo_round({"$multiply": [{"$divide": [{"$toDecimal": "$present_days"}, "$working_days"]}, 100]}), None]}}},
    ], "employee_code"


def summary_pipeline(month, department=None):
    first, last = month_bounds(month)
    query = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        query["department"] = department
    return "employees", [
        {"$match": query}, logs_lookup(first, last),
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                    **{k: {"$sum": stats_field(k)} for k in ["present_days", "late_count", "total_late_minutes", "leave_days", "on_duty_count", "hours_sum", "hours_count"]}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "late_count": 1,
                      "total_late_minutes": 1, "leave_count": "$leave_days", "on_duty_count": 1,
                      "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]},
                                                   mongo_round({"$divide": ["$hours_sum", "$hours_count"]}), None]}}},
        {"$sort": {"department": 1}},
    ], "department_joined" if department is not None else "joined_on"


def leaderboard_pipeline(month, limit=10, department=None):
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return "attendance_logs", pipeline, "date_employee"


def trend_pipeline(department, start, end):
    first, last = validate_range(start, end)
    if (last - first).days + 1 > 92:
        fail("Trend range must contain at most 92 days")
    # Seed a calendar with a matching employee so empty attendance ranges still
    # have a row. Explicit densify bounds are lower-inclusive, upper-exclusive.
    lower = datetime.combine(first, datetime.min.time(), UTC)
    upper = datetime.combine(last, datetime.min.time(), UTC) + timedelta(days=1)
    pipeline = [
        {"$match": {"department": department}}, {"$limit": 1},
        {"$project": {"_id": 0, "day": {"$literal": lower}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}},
                  "is_working_day": weekday("$day")}},
        {"$lookup": {"from": "employees", "let": {"day_date": "$date"}, "as": "daily", "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day_date"]}}},
            {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                         "let": {"day_date": "$$day_date"}, "as": "logs",
                         "pipeline": [{"$match": {"$expr": {"$eq": ["$date", "$$day_date"]}}}]}},
            {"$set": {"log": {"$arrayElemAt": ["$logs", 0]}}},
            {"$group": {"_id": None, "headcount": {"$sum": 1},
                        "present_count": {"$sum": {"$cond": [{"$in": ["$log.status", PRESENCE]},
                            {"$cond": [{"$ifNull": ["$log.half_day", False]}, 0.5, 1]}, 0]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]}, 1, 0]}}}},
        ]}},
        {"$set": {k: {"$ifNull": [{"$arrayElemAt": ["$daily." + k, 0]}, 0]}
                  for k in ["headcount", "present_count", "late_count"]}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                mongo_round({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": {"$toDecimal": "$attendance_rate"}, "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": mongo_round("$moving_avg_7d", 4)}},
        {"$sort": {"date": 1}},
    ]
    return "employees", pipeline, "department_joined"


def aggregate(spec):
    collection, pipeline, hint = spec
    return [json_value(x) for x in db[collection].aggregate(pipeline, hint=hint)]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    result = aggregate(monthly_pipeline(emp_code, month))
    if not result:
        raise HTTPException(404, "Employee not found")
    return result[0]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: str | None = None):
    return {"month": month, "items": aggregate(summary_pipeline(month, department))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: Limit = 10, department: str | None = None):
    return {"month": month, "items": aggregate(leaderboard_pipeline(month, limit, department))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, start: Annotated[str, Query(alias="from")], end: Annotated[str, Query(alias="to")]):
    spec = trend_pipeline(department, start, end)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "Department not found")
    return {"department": department, "items": aggregate(spec)}


ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: ExplainEndpoint, emp_code: str | None = None, month: str | None = None,
                     department: str | None = None, limit: Limit = 10, date_from: str | None = None,
                     date_to: str | None = None, status: Status | None = None,
                     start: Annotated[str | None, Query(alias="from")] = None,
                     end: Annotated[str | None, Query(alias="to")] = None,
                     page: Page = 1, page_size: PageSize = 20):
    if endpoint == "attendance_list":
        query, hint = attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {"find": collection, "filter": query, "sort": {"date": -1, "emp_code": 1},
                   "skip": (page - 1) * page_size, "limit": page_size, "hint": hint}
    else:
        if endpoint != "department_trend" and month is None:
            fail("month is required")
        if endpoint == "employee_monthly":
            if emp_code is None:
                fail("emp_code is required")
            spec = monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            spec = summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            spec = leaderboard_pipeline(month, limit, department)
        else:
            if department is None or start is None or end is None:
                fail("department, from and to are required")
            spec = trend_pipeline(department, start, end)
        collection, pipeline, hint = spec
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": json_value(result, hide_internal=False)}
