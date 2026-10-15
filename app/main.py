"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(value: float, places: int = 2) -> float:
    """Round positive attendance values using the contract's half-up rule."""
    from decimal import Decimal, ROUND_HALF_UP

    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    """Return whole minutes late, but only when more than 10 minutes late."""
    local_punch_in = punch_in.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    start = local_punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    elapsed_seconds = (local_punch_in - start).total_seconds()
    if elapsed_seconds <= 600:
        return 0
    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    hours = (punch_out - punch_in).total_seconds() / 3600
    return round_half_up(hours, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str = "09:30") -> int:
    """Return whole overtime minutes only when at least 30 minutes past shift end."""
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    if shift_end <= shift_start:
        end += timedelta(days=1)
    local_out = punch_out.astimezone(IST)
    elapsed_seconds = (local_out - end).total_seconds()
    elapsed_minutes = int(elapsed_seconds // 60)
    return elapsed_minutes if elapsed_minutes >= 30 else 0


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



# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def ensure_indexes():
    # Unique indexes enforce the API's concurrency/idempotency contract.
    db.employees.create_index("emp_code", unique=True, name="uniq_employee_code")
    # Existing test data may already contain duplicate employee/date records.
    # Do not prevent the whole API from starting if MongoDB cannot build this
    # unique index; resolve those duplicates, then create the index successfully.
    from pymongo.errors import OperationFailure
    try:
        db.attendance_logs.create_index(
            [("emp_code", 1), ("date", 1)], unique=True, name="uniq_attendance_employee_date"
        )
    except OperationFailure as exc:
        print("WARNING: Could not create unique attendance index; resolve duplicate records:", exc)
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)], name="attendance_punch_lookup"
    )


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for item in items:
        if isinstance(item.get("created_at"), datetime):
            created_at = item["created_at"]
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            item["created_at"] = int(created_at.timestamp() * 1000)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):

    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(
            status_code=422,
            detail="Status must be PRESENT, WFH, or ON_DUTY",
        )

    if body.punched_at is not None:
        if (
            isinstance(body.punched_at, bool)
            or not isinstance(body.punched_at, int)
            or body.punched_at < 100000000000
        ):
            raise HTTPException(
                status_code=422,
                detail="punched_at must be epoch milliseconds",
            )

        try:
            ts = datetime.fromtimestamp(
                body.punched_at / 1000,
                tz=timezone.utc,
            )
        except (ValueError, OverflowError, OSError):
            raise HTTPException(
                status_code=422,
                detail="punched_at is outside the supported timestamp range",
            )
    else:
        ts = datetime.now(timezone.utc)
    

    # Store timestamps at whole-second precision.
    ts = ts.replace(microsecond=0)

    # Convert to IST to determine the attendance date.
    ist_ts = ts.astimezone(IST)
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    # For an overnight shift, early punches belong to the previous shift date.
    attendance_date = ist_ts.date()
    if shift_end <= shift_start and ist_ts.strftime("%H:%M") < shift_end:
        attendance_date -= timedelta(days=1)

    date_str = attendance_date.isoformat()

    try:
        doc = {
            "emp_code": body.emp_code,
            "date": date_str,
            "status": body.status,
            "punch_in": ts,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": compute_late_minutes(ist_ts, shift_start),
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        db.attendance_logs.insert_one(doc)
    except Exception as exc:
        # Duplicate attendance for the same employee/date should return 409.
        from pymongo.errors import DuplicateKeyError

        if isinstance(exc, DuplicateKeyError):
            raise HTTPException(
                status_code=409,
                detail="Attendance already exists for this date",
            )
        raise

    # Return API timestamps as epoch milliseconds.
    doc["punch_in"] = int(ts.timestamp() * 1000)
    doc.pop("_id", None)
    return doc

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        if (
            isinstance(body.punched_at, bool)
            or not isinstance(body.punched_at, int)
            or body.punched_at < 100000000000
        ):
            raise HTTPException(
                status_code=422,
                detail="punched_at must be epoch milliseconds",
            )
        try:
            ts = datetime.fromtimestamp(
                body.punched_at / 1000, tz=timezone.utc
            )
        except (ValueError, OverflowError, OSError):
            raise HTTPException(
                status_code=422,
                detail="Timestamp is outside the supported range",
            )
    else:
        ts = datetime.now(timezone.utc)

    ts = ts.replace(microsecond=0)

    # Find the latest open attendance record for this employee.
    record = db.attendance_logs.find_one(
    {
        "emp_code": body.emp_code,
        "punch_in": {"$ne": None, "$lte": ts},
        "punch_out": None,
    },
    sort=[("punch_in", -1)],
)
    if not record:
        raise HTTPException(
            status_code=404, detail="No punch-in record found"
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409, detail="Attendance already punched out"
        )

    punch_in_time = record["punch_in"]
    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc)

    elapsed = (ts - punch_in_time).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )

    ist_punch_in = punch_in_time.astimezone(IST)
    ist_punch_out = ts.astimezone(IST)
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    work_hours = compute_work_hours(ist_punch_in, ist_punch_out)

    # Apply the minimum 30-minute overtime rule, including overnight shifts.
    overtime_minutes = compute_overtime(
        ist_punch_out, shift_end, record["date"], shift_start
    )

    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance was updated by another request",
        )

    return {
        "emp_code": body.emp_code,
        "date": record["date"],
        "status": record["status"],
        "punch_in": int(punch_in_time.timestamp() * 1000),
        "punch_out": int(ts.timestamp() * 1000),
        "work_hours": work_hours,
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
        "history": record.get("history", []),
    }

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    def as_epoch_ms(value):
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)

    for doc in docs:
        for field in ("punch_in", "punch_out"):
            doc[field] = as_epoch_ms(doc.get(field))
        for entry in doc.get("history", []):
            if "at" in entry:
                entry["at"] = as_epoch_ms(entry["at"])
            for change in entry.get("changes", {}).values():
                for key in ("from", "to"):
                    if key in change:
                        change[key] = as_epoch_ms(change[key])

    return {"items": docs, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Remaining contract endpoints
# --------------------------------------------------------------------------- #
from pydantic import BaseModel, Field
from calendar import monthrange
from decimal import Decimal, ROUND_HALF_UP

class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def _epoch_ms(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)
    return value


def _validate_month(month: str):
    try:
        parsed = datetime.strptime(month, "%Y-%m")
        if parsed.strftime("%Y-%m") != month:
            raise ValueError
        return parsed.year, parsed.month
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM")


def _period(year, month):
    first = f"{year:04d}-{month:02d}-01"
    last = f"{year:04d}-{month:02d}-{monthrange(year, month)[1]:02d}"
    return first, last


def _working_days(start_date, end_date):
    count = 0
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def _present_fraction(record):
    if record.get("status") not in {"PRESENT", "WFH", "ON_DUTY"}:
        return 0.0
    if datetime.fromisoformat(record["date"]).weekday() >= 5:
        return 0.0
    return 0.5 if record.get("half_day", False) else 1.0


def _api_record(record):
    if not record:
        return None
    record.pop("_id", None)
    for field in ("punch_in", "punch_out"):
        record[field] = _epoch_ms(record.get(field))
    record.setdefault("history", [])
    for entry in record["history"]:
        entry["at"] = _epoch_ms(entry.get("at"))
        for change in entry.get("changes", {}).values():
            for key in ("from", "to"):
                if key in change and isinstance(change[key], datetime):
                    change[key] = _epoch_ms(change[key])
    return record


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    try:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
        if target_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "Attendance record not found")
    updates = {}
    if body.status is not None:
        if body.status not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
            raise HTTPException(422, "Invalid status")
        updates["status"] = body.status
    for field, value in (("punch_in", body.punch_in), ("punch_out", body.punch_out)):
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 100000000000 or value > 4102444800000:
                raise HTTPException(422, f"{field} must be epoch milliseconds")
            try:
                updates[field] = datetime.fromtimestamp(value / 1000, timezone.utc).replace(microsecond=0)
            except (ValueError, OverflowError, OSError):
                raise HTTPException(422, f"Invalid {field}")
    status = updates.get("status", old.get("status"))
    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot include punch times")
        updates["punch_in"] = None
        updates["punch_out"] = None
        updates.update(work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
    else:
        pin = updates.get("punch_in", old.get("punch_in"))
        pout = updates.get("punch_out", old.get("punch_out"))
        if pin is None:
            raise HTTPException(422, "Presence status requires punch_in")
        pin_ist = pin.replace(tzinfo=timezone.utc).astimezone(IST) if pin.tzinfo is None else pin.astimezone(IST)
        shift_end, shift_start = emp["shift_end"], emp["shift_start"]
        expected_date = pin_ist.date()
        if shift_end <= shift_start and pin_ist.strftime("%H:%M") < shift_end:
            expected_date -= timedelta(days=1)
        if expected_date.isoformat() != date:
            raise HTTPException(422, "punch_in must belong to the attendance date")
        updates["late_minutes"] = compute_late_minutes(pin_ist, shift_start)
        if pout is None:
            updates.update(work_hours=None, overtime_minutes=0, half_day=False)
        else:
            pin_utc = pin.replace(tzinfo=timezone.utc) if pin.tzinfo is None else pin.astimezone(timezone.utc)
            pout_utc = pout.replace(tzinfo=timezone.utc) if pout.tzinfo is None else pout.astimezone(timezone.utc)
            elapsed = (pout_utc - pin_utc).total_seconds()
            if elapsed <= 0 or elapsed > 86400:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
            hours = compute_work_hours(pin_utc, pout_utc)
            updates["work_hours"] = hours
            updates["overtime_minutes"] = compute_overtime(pout_utc, shift_end, date, shift_start)
            updates["half_day"] = hours < 4.50
    changed = {}
    for key, newval in updates.items():
        oldval = old.get(key)
        if isinstance(oldval, datetime) and isinstance(newval, datetime):
            old_cmp = oldval.replace(tzinfo=timezone.utc) if oldval.tzinfo is None else oldval.astimezone(timezone.utc)
            new_cmp = newval.replace(tzinfo=timezone.utc) if newval.tzinfo is None else newval.astimezone(timezone.utc)
            same = old_cmp == new_cmp
        else:
            same = oldval == newval
        if not same:
            changed[key] = {"from": oldval, "to": newval}
    if not changed:
        raise HTTPException(422, "Request does not change the record")
    history_entry = {"at": datetime.now(timezone.utc).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changed}
    result = db.attendance_logs.update_one({"_id": old["_id"], "history": old.get("history", [])}, {"$set": updates, "$push": {"history": history_entry}})
    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record changed concurrently")
    fresh = db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
    return _api_record(fresh)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    year, mon = _validate_month(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "Employee not found")
    first, last = _period(year, mon)
    start = max(datetime.fromisoformat(first).date(), datetime.fromisoformat(emp["joined_on"]).date())
    end = datetime.fromisoformat(last).date()
    working_days = _working_days(start, end) if start <= end else 0
    logs = list(db.attendance_logs.find({"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}, {"_id": 0}))
    present_days = sum(_present_fraction(r) for r in logs)
    leave_days = sum(r.get("status") == "LEAVE" for r in logs)
    late_logs = [r for r in logs if r.get("late_minutes", 0) > 0]
    total_late = sum(r.get("late_minutes", 0) for r in logs)
    overtime = sum(r.get("overtime_minutes", 0) for r in logs)
    pct = round_half_up(present_days / working_days * 100, 4) if working_days else None
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": round_half_up(present_days), "leave_days": int(leave_days), "late_count": len(late_logs), "total_late_minutes": int(total_late), "total_overtime_minutes": int(overtime), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    year, mon = _validate_month(month)
    first, last = _period(year, mon)
    eq = {"joined_on": {"$lte": last}}
    if department:
        eq["department"] = department
    emps = list(db.employees.find(eq, {"_id": 0}))
    by_dept = {}
    for emp in emps:
        by_dept.setdefault(emp["department"], []).append(emp)
    logs = list(db.attendance_logs.find({"date": {"$gte": first, "$lte": last}}, {"_id": 0}))
    emp_map = {e["emp_code"]: e for e in emps}
    items = []
    for dept, members in sorted(by_dept.items()):
        member_codes = {e["emp_code"] for e in members}
        dlogs = [r for r in logs if r.get("emp_code") in member_codes]
        present = sum(_present_fraction(r) for r in dlogs)
        worked = [r.get("work_hours") for r in dlogs if r.get("status") in {"PRESENT", "WFH", "ON_DUTY"} and r.get("work_hours") is not None]
        late = [r for r in dlogs if r.get("late_minutes", 0) > 0]
        items.append({"department": dept, "headcount": len(members), "present_days": round_half_up(present), "avg_work_hours": round_half_up(sum(worked) / len(worked)) if worked else None, "late_count": len(late), "total_late_minutes": sum(r.get("late_minutes", 0) for r in dlogs), "leave_count": sum(r.get("status") == "LEAVE" for r in dlogs), "on_duty_count": sum(r.get("status") == "ON_DUTY" for r in dlogs)})
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    year, mon = _validate_month(month)
    first, last = _period(year, mon)
    employees = list(db.employees.find({}, {"_id": 0}))
    emp_map = {e["emp_code"]: e for e in employees if department is None or e.get("department") == department}
    logs = list(db.attendance_logs.find({"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}, {"_id": 0}))
    totals = {}
    counts = {}
    for log in logs:
        code = log.get("emp_code")
        if code not in emp_map:
            continue
        totals[code] = totals.get(code, 0) + log.get("late_minutes", 0)
        counts[code] = counts.get(code, 0) + 1
    rows = sorted(totals, key=lambda c: (-totals[c], c))
    items = []
    previous_total = None
    rank = 0
    for index, code in enumerate(rows, start=1):
        if totals[code] != previous_total:
            rank = index
            previous_total = totals[code]
        if rank > limit:
            break
        e = emp_map[code]
        items.append({"rank": rank, "emp_code": code, "name": e["name"], "department": e["department"], "total_late_minutes": totals[code], "late_count": counts[code]})
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    try:
        start = datetime.strptime(from_date, "%Y-%m-%d").date()
        end = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(422, "from and to must be YYYY-MM-DD")
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "Invalid date range; maximum 92 days")
    employees = list(db.employees.find({"department": department}, {"_id": 0}))
    if not employees:
        raise HTTPException(404, "Department not found")
    logs = list(db.attendance_logs.find({"date": {"$gte": from_date, "$lte": to_date}}, {"_id": 0}))
    codes = {e["emp_code"] for e in employees}
    rows = []
    current = start
    rates = []
    while current <= end:
        day = current.isoformat()
        day_logs = [r for r in logs if r.get("emp_code") in codes and r.get("date") == day]
        headcount = sum(e.get("joined_on", "9999-12-31") <= day for e in employees)
        present_count = sum((0.5 if r.get("half_day", False) else 1.0) for r in day_logs if r.get("status") in {"PRESENT", "WFH", "ON_DUTY"})
        late_count = sum(r.get("late_minutes", 0) > 0 for r in day_logs)
        working = current.weekday() < 5
        rate = round_half_up(present_count / headcount, 4) if working and headcount else None
        rates.append(rate)
        window = [v for v in rates[-7:] if v is not None]
        moving = round_half_up(sum(window) / len(window), 4) if window else None
        rows.append({"date": day, "is_working_day": working, "headcount": headcount, "present_count": round_half_up(present_count), "late_count": int(late_count), "attendance_rate": rate, "moving_avg_7d": moving})
        current += timedelta(days=1)
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if status: q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        explain = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size}, verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        year, mon = _validate_month(month); first, last = _period(year, mon)
        explain = db.command("explain", {"find": "attendance_logs", "filter": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}}, verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint in {"department_summary", "late_leaderboard"}:
        if not month: raise HTTPException(422, "month is required")
        year, mon = _validate_month(month); first, last = _period(year, mon)
        match = {"date": {"$gte": first, "$lte": last}}
        if endpoint == "late_leaderboard": match["late_minutes"] = {"$gt": 0}
        explain = db.command("explain", {"find": "attendance_logs", "filter": match}, verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")
        explain = db.command("explain", {"find": "attendance_logs", "filter": {"date": {"$gte": from_date, "$lte": to_date}}}, verbosity="executionStats")
        collection = "attendance_logs"
    else:
        raise HTTPException(422, "Unsupported endpoint")
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
