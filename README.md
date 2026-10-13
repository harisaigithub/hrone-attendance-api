# Employee Attendance & Analytics API

A small REST API for tracking employee attendance. Employees punch in and out under shift-based business
rules, HR can correct records with a full audit trail, and a set of analytics endpoints (monthly summaries,
department summaries, a late-arrivals leaderboard and a daily trend) are computed inside MongoDB with
aggregation pipelines.

Built with **Python 3.11+**, **FastAPI**, **PyMongo** and **MongoDB 6.0+**. All application code is in
[`app/main.py`](app/main.py).

## Contents

- [What it does](#what-it-does)
- [Project structure](#project-structure)
- [Setup and run](#setup-and-run)
- [Configuration](#configuration)
- [API overview](#api-overview)
- [Business rules](#business-rules)
- [How the analytics work](#how-the-analytics-work)
- [Concurrency and data integrity](#concurrency-and-data-integrity)
- [Indexes](#indexes)
- [Tests](#tests)
- [Status and known limits](#status-and-known-limits)
- [Use of AI assistance](#use-of-ai-assistance)
- [Other documents](#other-documents)

## What it does

- **Employees:** create and list employees (unique `emp_code`, per-employee shift times, department).
- **Punch-in / punch-out:** one attendance record per employee per day. Lateness, overtime, worked hours and
  half-day status are calculated when the record is written. Overnight shifts (for example 22:00 to 06:00)
  are supported.
- **Corrections:** a record can be corrected with a reason and the name of the person making the change.
  Every correction appends an entry to the record's `history` showing exactly which fields changed.
- **Analytics:** monthly summary per employee, monthly summary per department, a ranked late-arrivals
  leaderboard, and a day-by-day department trend with a 7-day moving average.
- **Explain endpoint:** shows the MongoDB query plan for each list or analytics query, so index usage can be
  checked.

## Project structure

```
.
├── app/
│   ├── __init__.py
│   └── main.py              # configuration, business rules, models, all endpoints, pipelines
├── tests/
│   ├── test_rules.py        # business rules and request validation (no database needed)
│   └── test_integration.py  # end-to-end tests against a real MongoDB
├── requirements.txt
├── README.md                # this file
├── REVIEW.md                # review of the supplied starter code
├── DECISIONS.md             # design decisions
└── .gitignore
```

`main.py` is organised top to bottom as: configuration and indexes, time helpers, business rules, request
models, response serialisation, employees, attendance, analytics, explain.

## Setup and run

You need Python 3.11 or newer, Git, and a MongoDB 6.0+ database (a local server or a free MongoDB Atlas
cluster).

**Windows (PowerShell)**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Then provide the database settings (see [Configuration](#configuration)) and start the server:

```bash
uvicorn app.main:app --port 8000
```

Open <http://127.0.0.1:8000/docs> for the interactive API page, where every endpoint can be called from the
browser. `GET /health` returns `{"status": "ok"}` when MongoDB is reachable and `503` when it is not.

Indexes are created automatically at startup.

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

The values are read from the environment. A local `.env` file is also read, and real environment variables
take priority over it. `.env` files are listed in `.gitignore` and are not part of the repository.

Example `.env` for Atlas:

```
MONGO_URI=mongodb+srv://<user>:<password>@<cluster-host>/
MONGO_DB=attendance_db
```

If the password contains special characters such as `@`, URL-encode them (`@` becomes `%40`).

## API overview

| Method | Path | Purpose | Main responses |
|---|---|---|---|
| GET | `/health` | Liveness and MongoDB connectivity | 200, 503 |
| POST | `/employees` | Create an employee | 201, 409 duplicate `emp_code`, 422 |
| GET | `/employees` | List employees (filter by `department`, paged) | 200, 422 |
| POST | `/attendance/punch-in` | Start today's record | 201, 404, 409, 422 |
| POST | `/attendance/punch-out` | Close the open record | 200, 404, 409, 422 |
| GET | `/attendance` | List records (filter by employee, dates, status; paged) | 200, 422 |
| PATCH | `/attendance/{emp_code}/{date}` | Correct a record and append to its history | 200, 404, 409, 422 |
| GET | `/analytics/employees/{emp_code}/monthly` | One employee's month | 200, 404, 422 |
| GET | `/analytics/departments/summary` | Per-department month totals | 200, 422 |
| GET | `/analytics/leaderboard/late` | Ranked late arrivals for a month | 200, 422 |
| GET | `/analytics/departments/{department}/trend` | Daily attendance rate and 7-day average | 200, 404, 422 |
| GET | `/admin/explain/{endpoint}` | MongoDB query plan for a list or analytics query | 200, 422 |

Conventions:

- Times are sent and returned as **epoch milliseconds** (integers). Sending seconds, strings, floats or
  booleans is rejected with `422`.
- Attendance dates are `YYYY-MM-DD` strings in **IST** (UTC+05:30).
- Paged lists accept `page` (from 1) and `page_size` (1 to 100) and return `items`, `total`, `page`,
  `page_size`.
- Invalid input always gives `422`; unknown employees or records give `404`; conflicting state (already
  punched in or out, concurrent edit) gives `409`.

## Business rules

- **Attendance date.** A record belongs to the IST date of the punch-in. For an overnight shift, a punch-in
  earlier than the shift end belongs to the shift that started the previous day.
- **Late minutes.** A punch-in is late only if it is more than 10 minutes after the shift start. The stored
  value is the whole minutes since shift start, otherwise 0.
- **Overtime.** Whole minutes after shift end, counted only when they reach 30 minutes. For overnight shifts
  the shift ends on the following day.
- **Work hours.** Punch-out minus punch-in, rounded half-up to two decimals using integer arithmetic (no
  floating point rounding).
- **Half day.** A record with fewer than 4.50 worked hours is a half day and counts as 0.5 in presence
  totals.
- **Presence statuses.** `PRESENT`, `WFH` and `ON_DUTY` count as present; `ABSENT` and `LEAVE` do not.
- **Time precision.** Instants are truncated to whole seconds. A punch-out must be after the punch-in and
  within 24 hours of it.
- **Corrections.** A correction may change status and punch times. Late minutes, overtime, work hours and
  half-day are recalculated. `ABSENT` and `LEAVE` clear the punch times. A request that changes nothing is
  rejected. Each accepted change appends `{at, by, reason, changes}` to `history`, where `changes` contains
  only the fields whose value actually changed, with `from` and `to`.
- **Working days (monthly summary).** Monday to Friday from the later of the join date and the first of the
  month, to the end of the month. The attendance percentage is `null` when there are no working days.

## How the analytics work

All aggregation runs inside MongoDB. Python only does calendar arithmetic and the final rounding of
integers.

- **Employee monthly.** One `$match` on the `(emp_code, date)` index, then `$project` and `$group` to total
  presence on weekdays, leave days, late counts and minutes, and overtime.
- **Department summary.** Starts from `employees` (joined on or before month end) and uses `$lookup` into
  `attendance_logs` on the indexed `emp_code`. Starting from employees means people with no logs still count
  toward headcount. The average work hours is the mean over all qualifying records, not an average of
  averages.
- **Late leaderboard.** `$group` by employee, `$lookup` the employee (logs of unknown employees are dropped),
  then `$setWindowFields` with `$rank`. Ties share a rank and the limit is applied after ranking, so a tie at
  the cutoff keeps all tied employees.
- **Department trend.** Builds one event per employee (counts toward headcount from the join date) and one
  per log, groups by day, fills gaps with `$densify`, computes a running headcount with `$setWindowFields`,
  then the daily attendance rate and its 7-day moving average. Weekends have no rate, and the moving average
  uses the available non-null daily rates.

Rates and averages are rounded half-up on integer values, so results do not depend on floating point
rounding.

## Concurrency and data integrity

- **Punch-in** inserts the record directly. A unique index on `(emp_code, date)` lets MongoDB accept one of
  several simultaneous requests; the others get `409`. There is no read-then-write race.
- **Punch-out** updates only a record whose `punch_out` is still empty, so only one of several simultaneous
  requests succeeds; the others get `409`.
- **Corrections** apply only if the record is still in the state that was read (same status, punch times and
  history length). Otherwise the request gets `409` and no history entry is lost.
- **Employees** are protected by a unique `emp_code` index, so duplicate creation is decided by the database.

## Indexes

Created at startup (idempotent).

| Collection | Index | Used for |
|---|---|---|
| `employees` | `emp_code` (unique) | Lookup, uniqueness, `$lookup` joins, sorted listing |
| `employees` | `department, emp_code` | Department filter with sorted listing |
| `employees` | `department, joined_on` | Department summary and trend start stage |
| `employees` | `joined_on` | Summary across all departments |
| `attendance_logs` | `emp_code, date` (unique) | One record per day, PATCH, monthly summary, `$lookup` joins |
| `attendance_logs` | `date desc, emp_code` | Default list order and month range scans |
| `attendance_logs` | `status, date desc, emp_code` | Status-filtered lists |
| `attendance_logs` | `emp_code, punch_in desc` | Finding the open record at punch-out |

`GET /admin/explain/{endpoint}` runs `explain` with execution statistics for `attendance_list`,
`employee_monthly`, `department_summary`, `late_leaderboard` and `department_trend`, so the plans can be
inspected. The integration tests assert that each plan uses `IXSCAN` and never `COLLSCAN`.

## Tests

```bash
pip install pytest httpx

# Business rules and request validation, no database needed
python -m pytest -q tests/test_rules.py

# End-to-end tests against a real MongoDB (uses MONGO_URI from the environment or .env)
python -m pytest -q tests/test_integration.py

# Everything
python -m pytest -q tests
```

`test_integration.py` uses and drops its own database, `attendance_integration_test`, so it never touches
your normal data. It checks the analytics endpoints against numbers calculated by hand, including a tie at
the leaderboard cutoff, an employee with no logs, a mid-month joiner, a weekend record, an overnight shift
and gap days in the trend. It also covers the audit trail, concurrent punch-in and punch-out, and index use
in the explain output.

## Status and known limits

- **Verified:** all 23 tests (10 in `test_rules.py`, 13 in `test_integration.py`) pass against a MongoDB
  Atlas M0 cluster, running Python 3.14.
- **Not implemented:** authentication and authorisation, a frontend, pagination cursors for very deep pages,
  and rate limiting.
- **Not tested:** behaviour under heavy load or with very large data sets. `DECISIONS.md` describes how I
  would approach scaling.
- **Interpretation choices:** the 7-day moving average is computed from the rounded daily rates shown in the
  response, and a trend range of up to 92 days inclusive is accepted.

## Use of AI assistance

I used an AI assistant (Claude, by Anthropic) while working on this assignment. I read the supplied kit,
ran the code and the tests myself against MongoDB Atlas, reviewed the result and can explain it. See
`DECISIONS.md` for the reasoning behind the main design choices.

## Other documents

- [`REVIEW.md`](REVIEW.md): defects found in the supplied starter code and how each was fixed.
- [`DECISIONS.md`](DECISIONS.md): design decisions (indexes, concurrency, ties, headcount, scaling).
