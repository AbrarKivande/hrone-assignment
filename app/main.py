import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


def startup_event():
    # Indexes idempotently created on startup
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)])


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", on_startup=[startup_event])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def to_epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def sanitize_doc(doc: dict) -> dict:
    if "_id" in doc:
        del doc["_id"]
    if "id" in doc:
        del doc["id"]
        
    for k, v in doc.items():
        if isinstance(v, datetime):
            doc[k] = to_epoch_ms(v)
        elif isinstance(v, list):
            doc[k] = [sanitize_doc(x) if isinstance(x, dict) else x for x in v]
        elif isinstance(v, dict):
            doc[k] = sanitize_doc(v)
    return doc


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    if not punch_in:
        return 0
    h, m = map(int, shift_start.split(":"))
    dt_attendance = datetime.fromisoformat(attendance_date).date()
    start = datetime.combine(dt_attendance, datetime.strptime(shift_start, "%H:%M").time(), tzinfo=IST)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    if not punch_in or not punch_out:
        return None
    hours = (punch_out - punch_in).total_seconds() / 3600
    return float(Decimal(str(hours)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    if not punch_out:
        return 0
    dt_attendance = datetime.fromisoformat(attendance_date).date()
    end_time = datetime.strptime(shift_end, "%H:%M").time()
    end_date = dt_attendance
    if shift_end <= shift_start:
        end_date += timedelta(days=1)
    end = datetime.combine(end_date, end_time, tzinfo=IST)
    minutes = int((punch_out - end).total_seconds() / 60)
    return minutes if minutes >= 30 else 0


def resolve_punch_in_date(ts: datetime, shift_start: str, shift_end: str) -> str:
    ts_ist = ts.astimezone(IST)
    calc_date = ts_ist.date()
    # shift_start <= shift_end means overlapping midnight but R1 uses: shift_end <= shift_start
    if shift_end <= shift_start:
        shift_end_time = datetime.strptime(shift_end, "%H:%M").time()
        if ts_ist.time() < shift_end_time:
            calc_date -= timedelta(days=1)
    return calc_date.isoformat()


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
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
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str


# --------------------------------------------------------------------------- #
# System & Employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")
    
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    
    return sanitize_doc(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    
    skip = (max(1, page) - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    
    return {
        "items": [sanitize_doc(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size
    }


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in ["PRESENT", "WFH", "ON_DUTY"]:
        raise HTTPException(422, "Invalid status for punch-in")
        
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    ts = from_epoch_ms(body.punched_at) if body.punched_at else datetime.now(timezone.utc)
    ts = ts.replace(microsecond=0)
    d = resolve_punch_in_date(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return sanitize_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    ts = from_epoch_ms(body.punched_at) if body.punched_at else datetime.now(timezone.utc)
    ts = ts.replace(microsecond=0)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)]
    )
    
    if not record:
        raise HTTPException(404, "No valid punch-in found")
        
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
        
    if (ts - record["punch_in"]).total_seconds() > 86400 or ts <= record["punch_in"]:
        raise HTTPException(422, "Invalid punch-out time relative to punch-in")

    wh = compute_work_hours(record["punch_in"], ts)
    ov = compute_overtime(ts, emp["shift_start"], emp["shift_end"], record["date"])
    hd = bool(wh < 4.50)

    update_doc = {
        "$set": {
            "punch_out": ts,
            "work_hours": wh,
            "overtime_minutes": ov,
            "half_day": hd
        }
    }
    
    db.attendance_logs.update_one({"_id": record["_id"]}, update_doc)
    
    record["punch_out"] = ts
    record["work_hours"] = wh
    record["overtime_minutes"] = ov
    record["half_day"] = hd
    
    return sanitize_doc(record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from > date_to")
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (max(1, page) - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size))

    return {
        "items": [sanitize_doc(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Record not found")

    new_status = body.status if body.status else record["status"]
    
    pin = from_epoch_ms(body.punch_in).replace(microsecond=0) if body.punch_in is not None else record.get("punch_in")
    pout = from_epoch_ms(body.punch_out).replace(microsecond=0) if body.punch_out is not None else record.get("punch_out")

    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE records cannot have punch times")
        pin = None
        pout = None
    else:
        if not pin:
            raise HTTPException(422, "Presence requires a punch_in")
        temp_date = resolve_punch_in_date(pin, emp["shift_start"], emp["shift_end"])
        if temp_date != date:
            raise HTTPException(422, "punch_in does not fall on the stated attendance date")

    if pin and pout:
        if pout <= pin or (pout - pin).total_seconds() > 86400:
            raise HTTPException(422, "pout must be > pin and within 24 hr")
            
    if not body.status and body.punch_in is None and body.punch_out is None:
        raise HTTPException(422, "No changes detected")

    wh = compute_work_hours(pin, pout) if (pin and pout) else None
    lm = compute_late_minutes(pin, emp["shift_start"], date) if pin else 0
    ov = compute_overtime(pout, emp["shift_start"], emp["shift_end"], date) if pout else 0
    hd = bool(wh is not None and wh < 4.50)
    
    if new_status in ["ABSENT", "LEAVE"]:
        wh, lm, ov, hd = None, 0, 0, False

    changes = {}
    updates = {
        "status": new_status, "punch_in": pin, "punch_out": pout,
        "work_hours": wh, "late_minutes": lm, "overtime_minutes": ov, "half_day": hd
    }

    for k, v in updates.items():
        if record.get(k) != v:
            changes[k] = {"from": record.get(k), "to": v}

    if not changes:
        raise HTTPException(422, "No changes detected")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    updates["history"] = record.get("history", []) + [history_entry]
    
    try:
        db.attendance_logs.update_one({"_id": record["_id"]}, {"$set": updates})
    except Exception as e:
        raise HTTPException(409, "Conflict updating record")

    updated_record = db.attendance_logs.find_one({"_id": record["_id"]})
    return sanitize_doc(updated_record)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #

HALF_UP_EXPR = {
    "$divide": [
        { "$floor": { "$add": [ { "$multiply": ["$attendance_pct_raw", 100] }, 0.5 ] } },
        100
    ]
}

HALF_UP_AVG_EXPR = {
    "$divide": [
        { "$floor": { "$add": [ { "$multiply": ["$avg_work_hours_raw", 100] }, 0.5 ] } },
        100
    ]
}

HALF_UP_RATE_EXPR = {
    "$divide": [
        { "$floor": { "$add": [ { "$multiply": ["$attendance_rate_raw", 10000] }, 0.5 ] } },
        10000
    ]
}

HALF_UP_MA_EXPR = {
    "$divide": [
        { "$floor": { "$add": [ { "$multiply": ["$moving_avg_raw", 10000] }, 0.5 ] } },
        10000
    ]
}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., regex=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    start_str = f"{month}-01"
    
    y, m = map(int, month.split("-"))
    if m == 12:
        end_str = f"{y+1}-01-01"
    else:
        end_str = f"{y}-{m+1:02d}-01"
    
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": { "$gte": start_str, "$lt": end_str }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "logs": { "$push": "$$ROOT" }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        { "$unwind": "$emp" },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "month": { "$literal": month },
                "logs": 1,
                "joined_on": "$emp.joined_on"
            }
        },
        {
            "$addFields": {
                "working_days": {
                    "$size": {
                        "$filter": {
                            "input": {
                                "$map": {
                                    "input": {
                                        # Max 31 days in a month. Just using dense list and clipping it properly
                                        "$range": [0, 31]
                                    },
                                    "as": "d",
                                    "in": { "$dateAdd": { "startDate": { "$dateFromString": { "dateString": start_str } }, "unit": "day", "amount": "$$d" } }
                                }
                            },
                            "as": "date_obj",
                            "cond": {
                                "$and": [
                                    { "$lt": [ "$$date_obj", { "$dateFromString": { "dateString": end_str } } ] },
                                    { "$gte": [ "$$date_obj", { "$dateFromString": { "dateString": "$joined_on" } } ] },
                                    { "$in": [ { "$isoDayOfWeek": "$$date_obj" }, [1, 2, 3, 4, 5] ] }
                                ]
                            }
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "emp_code": 1,
                "month": 1,
                "working_days": 1,
                "leave_days": {
                    "$size": {
                        "$filter": { "input": "$logs", "as": "L", "cond": { "$eq": ["$$L.status", "LEAVE"] } }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": { "input": "$logs", "as": "L", "cond": { "$gt": ["$$L.late_minutes", 0] } }
                    }
                },
                "total_late_minutes": { "$sum": "$logs.late_minutes" },
                "total_overtime_minutes": { "$sum": "$logs.overtime_minutes" },
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "L",
                                    "cond": {
                                        "$and": [
                                            { "$in": ["$$L.status", ["PRESENT", "WFH", "ON_DUTY"]] },
                                            { "$in": [ { "$isoDayOfWeek": { "$dateFromString": { "dateString": "$$L.date" } } }, [1, 2, 3, 4, 5] ] }
                                        ]
                                    }
                                }
                            },
                            "as": "L2",
                            "in": { "$cond": [ { "$eq": ["$$L2.half_day", True] }, 0.5, 1 ] }
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_pct_raw": {
                    "$cond": [
                        { "$eq": ["$working_days", 0] },
                        None,
                        { "$multiply": [ { "$divide": ["$present_days", "$working_days"] }, 100 ] }
                    ]
                }
            }
        },
        {
            "$addFields": {
                "attendance_pct": { "$cond": [ { "$eq": ["$attendance_pct_raw", None] }, None, HALF_UP_EXPR ] }
            }
        },
        { "$project": { "attendance_pct_raw": 0 } }
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if not res:
        # Evaluate manually if no logs exist but employee exists
        start_date_obj = datetime.strptime(start_str, "%Y-%m-%d").date()
        if m == 12:
            end_date_obj = datetime(y+1, 1, 1).date()
        else:
            end_date_obj = datetime(y, m+1, 1).date()
            
        joined = datetime.strptime(emp["joined_on"], "%Y-%m-%d").date()
        working = 0
        cur = start_date_obj
        while cur < end_date_obj:
            if cur >= joined and cur.isoweekday() <= 5:
                working += 1
            cur += timedelta(days=1)
            
        return {
            "emp_code": emp_code, "month": month, "working_days": working, "present_days": 0.0,
            "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0,
            "attendance_pct": 0.0 if working > 0 else None
        }

    return res[0]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., regex=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    start_str = f"{month}-01"
    y, m = map(int, month.split("-"))
    if m == 12:
        end_str = f"{y+1}-01-01"
        end_display = f"{y}-12-31" 
    else:
        end_display = (datetime(y, m+1, 1) - timedelta(days=1)).strftime("%Y-%m-%d")
        end_str = f"{y}-{m+1:02d}-01"

    match_dict = { "joined_on": { "$lte": end_display } }
    if department:
        match_dict["department"] = department

    pipeline = [
        { "$match": match_dict },
        {
            "$group": {
                "_id": "$department",
                "headcount": { "$sum": 1 },
                "emp_codes": { "$push": "$emp_code" }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": { "emps": "$emp_codes" },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": { "$in": ["$emp_code", "$$emps"] },
                            "date": { "$gte": start_str, "$lt": end_str }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
             "$unwind": { "path": "$logs", "preserveNullAndEmptyArrays": True }
        },
        {
             "$group": {
                 "_id": "$_id",
                 "headcount": { "$first": "$headcount" },
                 "late_count_arr": { "$push": { "$cond": [ { "$gt": ["$logs.late_minutes", 0] }, 1, 0 ] } },
                 "total_late_min_arr": { "$push": { "$cond": [ { "$gt": ["$logs.late_minutes", 0] }, "$logs.late_minutes", 0 ] } },
                 "leave_count_arr": { "$push": { "$cond": [ { "$eq": ["$logs.status", "LEAVE"] }, 1, 0 ] } },
                 "on_duty_count_arr": { "$push": { "$cond": [ { "$eq": ["$logs.status", "ON_DUTY"] }, 1, 0 ] } },
                 "work_hours_arr": { "$push": { "$cond": [ { "$and": [ { "$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]] }, { "$ne": ["$logs.work_hours", None] } ] }, "$logs.work_hours", None ] } },
                 "present_logs": {
                     "$push": {
                         "$cond": [
                             { "$and": [ { "$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]] }, { "$in": [ { "$isoDayOfWeek": { "$dateFromString": { "dateString": "$logs.date" } } }, [1, 2, 3, 4, 5] ] } ] },
                             { "$cond": [ { "$eq": ["$logs.half_day", True] }, 0.5, 1 ] },
                             0
                         ]
                     }
                 }
             }
        },
        {
             "$addFields": {
                 "late_count": { "$sum": "$late_count_arr" },
                 "total_late_minutes": { "$sum": "$total_late_min_arr" },
                 "leave_count": { "$sum": "$leave_count_arr" },
                 "on_duty_count": { "$sum": "$on_duty_count_arr" },
                 "present_days": { "$sum": "$present_logs" },
                 "wh_filtered": {
                     "$filter": { "input": "$work_hours_arr", "as": "w", "cond": { "$ne": ["$$w", None] } }
                 }
             }
        },
        {
             "$addFields": {
                 "avg_work_hours_raw": {
                     "$cond": [
                         { "$eq": [ { "$size": "$wh_filtered" }, 0 ] },
                         None,
                         { "$avg": "$wh_filtered" }
                     ]
                 }
             }
        },
        {
             "$addFields": {
                 "avg_work_hours": { "$cond": [ { "$eq": ["$avg_work_hours_raw", None] }, None, HALF_UP_AVG_EXPR ] }
             }
        },
        {
             "$project": {
                 "_id": 0,
                 "department": "$_id",
                 "headcount": 1,
                 "present_days": 1,
                 "avg_work_hours": 1,
                 "late_count": 1,
                 "total_late_minutes": 1,
                 "leave_count": 1,
                 "on_duty_count": 1
             }
        },
        { "$sort": { "department": ASCENDING } }
    ]

    res = list(db.employees.aggregate(pipeline))
    return { "month": month, "items": res }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., regex=r"^\d{4}-(0[1-9]|1[0-2])$"), 
    limit: int = 10,
    department: Optional[str] = None
):
    start_str = f"{month}-01"
    y, m = map(int, month.split("-"))
    if m == 12:
        end_str = f"{y+1}-01-01"
    else:
        end_str = f"{y}-{m+1:02d}-01"

    pipeline = [
        {
            "$match": {
                "date": { "$gte": start_str, "$lt": end_str },
                "late_minutes": { "$gt": 0 }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": { "$sum": "$late_minutes" },
                "late_count": { "$sum": 1 }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        { "$unwind": "$emp" }
    ]

    if department:
        pipeline.append({ "$match": { "emp.department": department } })

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": { "total_late_minutes": DESCENDING },
                "output": {
                    "rank": { "$rank": {} }
                }
            }
        },
        {
            "$match": { "rank": { "$lte": limit } }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$sort": { "total_late_minutes": DESCENDING, "emp_code": ASCENDING }
        }
    ])

    res = list(db.attendance_logs.aggregate(pipeline))
    return { "month": month, "items": res }


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(alias="from"), to_date: str = Query(alias="to")):
    if to_date < from_date:
        raise HTTPException(422, "to >= from is required")
        
    start_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
    end_dt = datetime.strptime(to_date, "%Y-%m-%d").date()
    if (end_dt - start_dt).days > 92:
        raise HTTPException(422, "Range exceeds 92 days")
        
    emp_count = db.employees.count_documents({"department": department})
    if emp_count == 0:
        raise HTTPException(404, "Department has no employees")

    end_plus_1 = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    pipeline = [
        { "$match": { "department": department } },
        {
            "$group": {
                "_id": None,
                "emps": { "$push": { "emp_code": "$emp_code", "joined_on": "$joined_on" } }
            }
        },
        {
            "$addFields": {
                "dummy_date": { "$dateFromString": { "dateString": from_date } }
            }
        },
        {
            "$densify": {
                "field": "dummy_date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [ 
                        { "$dateFromString": { "dateString": from_date } }, 
                        { "$dateFromString": { "dateString": end_plus_1 } } 
                    ]
                }
            }
        },
        {
            "$addFields": {
                "date_str": { "$dateToString": { "format": "%Y-%m-%d", "date": "$dummy_date" } }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": { "curr_date": "$date_str", "emps_arr": "$emps" },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    { "$eq": ["$date", "$$curr_date"] },
                                    { "$in": ["$emp_code", "$$emps_arr.emp_code"] }
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": {
                    "$lte": [ { "$isoDayOfWeek": "$dummy_date" }, 5 ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$emps",
                            "as": "e",
                            "cond": { "$lte": ["$$e.joined_on", "$date_str"] }
                        }
                    }
                },
                "present_count_arr": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "L",
                                "cond": { "$in": ["$$L.status", ["PRESENT", "WFH", "ON_DUTY"]] }
                            }
                        },
                        "as": "L2",
                        "in": { "$cond": [ { "$eq": ["$$L2.half_day", True] }, 0.5, 1 ] }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "L",
                            "cond": { "$gt": ["$$L.late_minutes", 0] }
                        }
                    }
                }
            }
        },
        {
            "$addFields": { "present_count": { "$sum": "$present_count_arr" } }
        },
        {
            "$addFields": {
                "attendance_rate_raw": {
                    "$cond": [
                        { "$and": [ { "$eq": ["$is_working_day", True] }, { "$gt": ["$headcount", 0] } ] },
                        { "$divide": ["$present_count", "$headcount"] },
                        None
                    ]
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [ { "$eq": ["$attendance_rate_raw", None] }, None, HALF_UP_RATE_EXPR ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": { "date": ASCENDING },
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "moving_avg_7d": {
                    "$cond": [ { "$eq": ["$moving_avg_raw", None] }, None, HALF_UP_MA_EXPR ]
                }
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        },
        { "$sort": { "date": ASCENDING } }
    ]

    res = list(db.employees.aggregate(pipeline))
    return { "department": department, "items": res }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., regex="^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = 1,
    page_size: Optional[int] = 20
):
    if endpoint == "department_summary":
        if not month: raise HTTPException(422)
        end_display = month + "-31" # Simplification for matched filter boundary
        y, m = map(int, month.split("-"))
        if m == 12: end_display = f"{y}-12-31" 
        else: end_display = (datetime(y, m+1, 1) - timedelta(days=1)).strftime("%Y-%m-%d")
        
        match_dict = { "joined_on": { "$lte": end_display } }
        if department: match_dict["department"] = department
        
        pipeline = [{ "$match": match_dict }, { "$group": { "_id": "$department" } }]
        stats = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": stats}
        
    elif endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status: q["status"] = status
        skip = (max(1, page) - 1) * page_size
        
        stats = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": skip, "limit": page_size}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": stats}
        
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422)
        start_str = f"{month}-01"
        y, m = map(int, month.split("-"))
        if m == 12: end_str = f"{y+1}-01-01"
        else: end_str = f"{y}-{m+1:02d}-01"
        pipeline = [{ "$match": { "emp_code": emp_code, "date": { "$gte": start_str, "$lt": end_str } } }]
        stats = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": stats}
        
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422)
        start_str = f"{month}-01"
        y, m = map(int, month.split("-"))
        if m == 12: end_str = f"{y+1}-01-01"
        else: end_str = f"{y}-{m+1:02d}-01"
        pipeline = [{ "$match": { "date": { "$gte": start_str, "$lt": end_str }, "late_minutes": { "$gt": 0 } } }]
        stats = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": stats}
        
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date: raise HTTPException(422)
        pipeline = [{ "$match": { "department": department } }]
        stats = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": stats}
