# Starter review

Line numbers refer to the original starter.

| # | Where | What is wrong | How to notice it | Fix |
|---|---|---|---|---|
| 1 | MongoClient, 22 | Reads naive UTC times, incompatible with aware shift ends. | Subtract a read timestamp from an aware IST shift boundary. | Use tz_aware=True and normalize UTC. |
| 2 | Initialization, 22–27 | No startup indexes. | Inspect indexes or explain a large attendance query. | Create unique and query indexes idempotently. |
| 3 | compute_late_minutes, 36–37 | Floors minutes before checking grace. | 09:40:01 for a 09:30 shift returns zero. | Compare seconds with 600 before flooring. |
| 4 | compute_late_minutes, 35 | Shift start uses the punch calendar day. | 01:00 punch during a 22:00–06:00 shift. | Use the attendance date for IST shift boundaries. |
| 5 | compute_work_hours, 41 | Float rounding is not exact half-up. | 16,218 seconds must produce 4.51 hours. | Decimal division and ROUND_HALF_UP. |
| 6 | compute_overtime, 47 | Counts overtime below 30 minutes. | 18:35 exit for an 18:30 end produces 5. | Return zero below the minimum. |
| 7 | compute_overtime, 46 | Overnight end remains on the start day. | 06:40 next-day exit for a 22:00–06:00 shift. | Advance overnight end by one day. |
| 8 | EmployeeIn, 54–60 | Missing identity, email and length constraints. | Submit BAD code, invalid email or empty name. | Apply contract patterns and length bounds. |
| 9 | EmployeeIn, 58–60 | Shift times and joined_on are unchecked strings. | Submit 25:99 or 2026-02-30. | Validate clock format and real dates. |
| 10 | EmployeeIn, 58–59 | Equal shift boundaries accepted. | Set both to 09:30. | Reject equal start and end. |
| 11 | PunchInIn, 65 | Coercive integers; no epoch bounds. | Submit a timestamp string, boolean or seconds. | Bounded StrictInt; omission uses now, explicit null fails. |
| 12 | PunchInIn, 66 | Any status accepted. | Submit ABSENT or UNKNOWN. | Restrict to the three presence statuses. |
| 13 | health, 74 | Always reports ready. | Disconnect MongoDB. | Ping; return 503 on database failure. |
| 14 | create_employee, 79–83 | Check-then-insert permits duplicate races. | Concurrent creates with the same code. | Unique index; catch DuplicateKeyError as 409. |
| 15 | create_employee, 82 | Naive local clock may be stored as UTC incorrectly. | Run outside UTC. | Use aware UTC now. |
| 16 | Employee responses, 85/96 | created_at serializes as ISO text. | Inspect JSON. | Return integer epoch milliseconds. |
| 17 | list_employees, 93 | Page one skips the first page. | Request page=1, page_size=1. | Use (page - 1) * page_size. |
| 18 | list_employees, 94 | Total ignores department. | Filter Engineering with Sales also present. | Count the filtered query. |
| 19 | list_employees, 95 | Missing stable code ordering. | Insert employees in reverse code order. | Sort emp_code ascending. |
| 20 | List parameters, 89/130–131 | No pagination bounds. | page=0 or page_size=101. | Enforce page >= 1 and size 1–100. |
| 21 | punch_in, 101/113 | Unknown employee causes a dereference error. | Punch as EMP9999. | Return explicit 404. |
| 22 | punch_in, 102 | Host-local time used instead of UTC/IST rules. | Run on a non-IST host. | Parse aware UTC; convert for IST rules. |
| 23 | punch_in, 103 | Overnight previous-day assignment missing. | 05:59 punch for a 22:00–06:00 shift. | Apply R1 in IST. |
| 24 | punch_in, 102 | Subsecond precision retained. | Timestamp ends in 900 milliseconds. | Truncate before storing/calculating. |
| 25 | punch_in, 104–118 | Duplicate attendance race. | Parallel same employee/date inserts. | Unique employee/date index; catch conflict. |
| 26 | Attendance responses, 119/149 | Forbidden internal/generated id exposed. | Inspect response keys. | Hide identity; use emp_code/date. |
| 27 | Attendance responses, 121/150 | Punch/history instants returned as ISO text. | Inspect nested history changes. | Recursively convert BSON dates to milliseconds. |
| 28 | list_attendance, 127–143 | Invalid dates, reversed ranges and statuses accepted. | Bad date, reversed range or unknown status. | Validate and return 422. |
| 29 | list_attendance, 144–147 | Loads/sorts/slices matching records in Python. | Query 100,000 logs. | Indexed DB sort, skip, limit and count. |
| 30 | list_attendance, 145 | Missing secondary employee-code order. | Multiple employees have the same date. | Sort date descending, code ascending. |
| 31 | list_attendance, 150 | Missing legacy history/half_day defaults. | Read a legacy seeded record. | Supply empty history, false half_day and missing minute defaults. |
| 32 | Filter checks, 91/134/142 | Empty supplied strings silently drop filters. | Supply an empty exact-match department filter. | Check None rather than truthiness. |

## Examined and retained

- load_dotenv already preserves real environment variables by default; override=False is now explicit.
- Calendar dates should remain strings; instants should be BSON dates.
- Synchronous FastAPI handlers are appropriate for synchronous PyMongo because FastAPI runs them in a thread pool.
- work_hours should be null until punch-out, not zero.
- The starter's attendance slice offset formula was correct; performing pagination in Python was the defect.
- Punch-in history starts empty. Punch-out never adds audit entries; only corrections do.
