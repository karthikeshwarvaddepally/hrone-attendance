# Understanding the code

Start with time helpers, then attendance endpoints, then analytics builders. Live MongoDB verification is still required.

## Request flow

FastAPI's decorator maps a method/path to a Python function. Pydantic validates the request before it runs. `EmployeeIn` checks employee details. Punch models accept bounded strict millisecond integers: omission uses now, explicit null is invalid. Extra fields are ignored so clients cannot set derived fields.

Create an employee in `/docs`, punch in, and follow `employee_or_404` → `instant` → `attendance_date` → `derive` → `insert_one`.

## Dates and time

`2026-07-06` is a business day; a punch timestamp is an exact instant. MongoDB stores instants in UTC, and JSON exposes integer milliseconds. `instant` truncates to seconds, `attendance_date` applies IST and overnight rules, and `shift_bounds` builds the scheduled boundaries.

For a 22:00–06:00 shift, a July 6 21:55 punch-in and July 7 06:40 punch-out belong to July 6: 8.75 work hours, 40 overtime minutes.

For a 09:30 shift, 09:40:00 is inside grace but 09:40:01 is outside. Compare seconds first, then floor minutes: the latter means 10 late minutes. Decimal half-up rounding happens before half-day classification.

## Concurrency and history

Check-then-insert is unsafe: two requests may both see no record. A unique index ensures only one insert succeeds. DuplicateKeyError becomes 409.

A correction reads the current values, constructs final values and records actual changes. Its update condition requires the version and original values still to match. A competing write makes that condition fail and produces 409. A successful atomic update changes values, increments the version and appends one audit entry with `$push`.

Punch-out preserves stored lateness and never appends history. No multi-document transaction is necessary for a single-document correction.

## Analytics

| Stage | Purpose |
|---|---|
| `$match` | Restrict input early. |
| `$lookup` | Join by employee code. |
| `$group` | Sum records, hours and headcount. |
| `$project` | Produce exact response fields. |
| `$setWindowFields` / `$rank` | Rank totals before applying the cutoff. |
| `$densify` | Include every requested day. |
| `$setWindowFields` / `$avg` | Average non-null rates over seven calendar rows. |

Monthly and department summaries start from employees, so zero-log employees remain visible. Working days are generated inside MongoDB. Department averages divide total hours by total completed present-status records, rather than averaging employee averages.

Leaderboard ranks only by total lateness. Employee code sorts ties afterward; putting code in the ranking key would destroy ties. Limit two may return three people when ranks are 1, 2, 2.

Trend creates the first requested day and densifies to the final day. Each day looks up employees who had joined and their attendance. Weekend rates are null; missing weekdays with headcount have rate zero. The moving average ignores null rates but still spans seven calendar rows, restricted to the requested range.

## Explain

Analytics and explain call the same builders. Explain requests execution statistics from MongoDB. IXSCAN indicates an index scan; COLLSCAN a collection scan. Check lookup scan counts and execution time as well as stage names. These properties cannot be proven by mongomock.

## Practice questions

- Why does 09:40:01 mean ten late minutes?
- What prevents duplicate attendance under concurrent requests?
- Why are timestamps different from calendar dates?
- Why use `$rank` instead of `$denseRank`?
- Why does headcount start with employees?
- Why average hours over records rather than employees?
- What does the conditional correction filter protect?
- Why are weekend rates null?
- Which checks still need real MongoDB?
