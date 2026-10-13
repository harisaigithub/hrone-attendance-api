# REVIEW.md

Defects found in the starter `app/main.py`. Every one is fixed in the submitted file.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Builds shift start from the punch-in's own (UTC) date and clock, so "09:30" means 09:30 UTC, not IST. | Sample record EMP0003: punch-in 10:05 IST (04:35Z) returns 0, should be 35. | Shift start is built as IST on the attendance date (`at_ist`). |
| 2 | `compute_late_minutes` | Floors to minutes first, then tests `> 10`. 09:40:01 gives 10, so it is wrongly "not late"; also ignores overnight shifts. | Punch 09:40:01, shift 09:30: expected 10, got 0. | Compare whole seconds `> 600`, then floor to minutes (R2). |
| 3 | `compute_work_hours` | `round()` is banker's rounding on floats, not half-up. | 16218 s (4.505 h) gives 4.5, expected 4.51. | Integer arithmetic: `(2*secs+36)//72` hundredths (R4). |
| 4 | `compute_overtime` | No 30-minute minimum; uses the date string as shift end day so overnight shifts are wrong; subtracts an aware datetime from naive punch-out (TypeError). | Punch-out 18:35 on an 18:30 shift returns 5, should be 0. | Rewritten per R3 with aware datetimes and next-day end for overnight shifts. |
| 5 | `punch_in` | `datetime.fromtimestamp(ms/1000)` / `datetime.now()` are naive server-local time; the date is the server's date, not IST, and R1 (overnight) is ignored. | Punch at 00:30 IST on a UTC server gets the previous day's date. | Aware UTC datetimes, truncated to seconds, `attendance_date()` implements R1. |
| 6 | `punch_in` | `find_one` then `insert_one` is a race, and there is no unique index. | Two parallel punch-ins both return 201. | Unique `(emp_code, date)` index; `DuplicateKeyError` becomes 409. |
| 7 | `punch_in` | Unknown employee: `emp` is `None`, `emp["shift_start"]` raises TypeError, a 500. | Punch-in for `EMP9999`. | 404 before anything else. |
| 8 | `punch_in` | `status` is a free string (`ABSENT`, `"x"` accepted); `if body.punched_at` treats 0 as missing; `punched_at` accepts strings, floats and seconds (pydantic lax mode). | `{"status":"ABSENT"}` returns 201; `punched_at: 1783312500` accepted. | `PresenceStatus` literal; strict int with range check (422); `is not None` test. |
| 9 | `punch_in` response | Returns `id` (ObjectId string) and raw datetimes; contract says no ids and epoch milliseconds. | Response contains `"id"` and ISO timestamps. | `record_out()` builds the exact contract shape. |
| 10 | `create_employee` | check-then-insert race, no unique index; `created_at` is naive local `datetime.now()` and returned as a datetime; no validation of `emp_code` format, email, times, `joined_on`, or `shift_start != shift_end`. | Parallel creates give two 201; `emp_code:"x"` accepted. | Unique index + 409; pydantic patterns and validators; `created_at` UTC, returned in epoch ms. |
| 11 | `list_employees` | `skip = page * page_size` skips the whole first page; `total` counts all employees, not the filtered set; no sort; no bounds on `page`/`page_size`. | `?page=1` returns page 2; `?department=X` total is wrong. | `(page-1)*page_size`, `count_documents(filter)`, sort by `emp_code`, `Query(ge=1, le=100)`. |
| 12 | `list_attendance` | Loads every matching document into Python, sorts there, slices there. Sorts on date only (no `emp_code` tie-break). No index. Leaks `_id` as `id`. | 100k records: slow and memory heavy. | Server-side filter, sort, skip, limit; indexes; contract-shaped output. |
| 13 | `list_attendance` | Legacy documents lack `history`/`half_day`; `date_from > date_to` and invalid dates/status/pages are not 422. | Query with `date_from` after `date_to` returns 200. | `record_out` applies defaults; validation added. |
| 14 | `/health` | Always returns 200, never touches MongoDB. | Stop MongoDB: still "ok". | Pings MongoDB, 503 on failure. |
| 15 | Setup | `MongoClient` is not `tz_aware`, so every datetime read back is naive; no indexes created at startup. | Naive/aware `TypeError` on arithmetic. | `tz_aware=True`; `ensure_indexes()` in the lifespan hook. |

Looked at and judged fine: `load_dotenv()` does not override real environment variables, which matches the
contract; blocking PyMongo calls inside plain `def` endpoints run in FastAPI's thread pool, so that is acceptable;
the `IST` constant is correct (UTC+05:30).
