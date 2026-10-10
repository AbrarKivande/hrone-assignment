# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `create_employee` | Race condition during insert. Check `find_one()` then `insert_one()` allows concurrent identical inserts. | Hitting endpoint concurrently fails to enforce uniqueness rule or yields two 201s. | Added a unique index on `emp_code` and caught `DuplicateKeyError` to return 409. |
| 2 | `create_employee` | `datetime.now()` returns naive datetimes. FastAPI serialization and PyMongo handle them incorrectly. | Output API timestamp looks like `"2026-X-X"` instead of epoch integer. | Used aware UTC instants and added a response sanitizer casting to `int(ts.timestamp() * 1000)`. |
| 3 | `list_employees` | The `total` count ignores the `department` query filter. | Calling `GET /employees?department=XYZ` returns the count of ALL employees. | Passed the `q` dictionary into `count_documents(q)`. |
| 4 | `list_employees` | Missing required sorting. | The contract specifies sorted by `emp_code ASC`, but it was omitted. | Added `.sort("emp_code", ASCENDING)` before skip/limit. |
| 5 | `list_attendance` | Endpoint loads the entire collection into memory, sorts linearly, and slices arrays natively. | Massive OOM crash under load. | Used MongoDB cursor sorting `.sort(...)` and `.skip().limit()`. |
| 6 | `list_attendance` | Endpoint mutates a rogue `_id` into `id` string and returns it. | Schema check fails since `id` is not specified in the payload contract. | Stripped off all `_id` values explicitly. |
| 7 | `punch_in` | Setting `date` uses Python's `ts.date()` locally which disregards IST bounding (R1 overnight limits). | Punching in before `shift_end` on overnight shifts records as the current day rather than the previous. | Wrote `resolve_punch_in_date` checking specifically if `ts.time() < shift_end` with an overnight bound. |
| 8 | `punch_in` | Uses `datetime.now()` and ignores rule about truncating to exact whole seconds prior to computation. | Precise timing comparisons fail randomly near millisecond boundary markers. | Added `.replace(microsecond=0)` explicitly to all inserts. |
| 9 | `compute_late_minutes` | Mutating `shift_start` via `.replace` against `punch_in` datetimes is mathematically unstable natively. | Overnight boundaries incorrectly flag users as late or on-time when traversing days natively. | Synthesizes precise target instants strictly using `ISO strings` and isolated `date()` extractions. |
| 10 | `compute_overtime` | Instantiation logic fails to leap day boundaries by `timedelta(1)` for shifts spanning next-day finishes natively. | Employee punching out at 01:00 AM on overnight shifts generates deeply negative overtime calculations. | Added an explicit `+ timedelta(days=1)` to end goals when `shift_end <= shift_start`. |
| 11 | `compute_work_hours` | Uses standard Python `round()` returning Banker's Rounding results. | Results ending exactly in `0.0X5` occasionally round *down* rather than Half-Up (violating R8). | Rewrote using Python's `Decimal` type specifically invoking `ROUND_HALF_UP`. |

Also note anything you looked at and decided was **not** a defect, and why:
- I examined the Pydantic schemas (e.g. `PunchInIn`, `EmployeeIn`). While quite sparse, since they natively map to exactly the validation rules needed inside endpoints via optional markers, they do fundamentally work and didn't strictly require structural class rework. However, I heavily implemented output sanitizers to resolve response formatting cleanly.
