# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/punch-out, manual attendance corrections with an audit trail, and
attendance analytics built on MongoDB aggregation pipelines. All application code is in `app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # your own MongoDB 6.0+ (local or Atlas)
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are read from the environment (a local `.env` is also read, real environment
variables take priority). Indexes are created at startup. Interactive docs: `http://127.0.0.1:8000/docs`.

## Tests

```bash
python -m pytest -q tests/test_rules.py          # pure logic and request validation, no database needed
MONGO_URI=... python -m pytest -q tests/test_integration.py   # needs MongoDB; uses and drops the DB "attendance_integration_test"
```

`test_integration.py` covers the analytics endpoints against hand-calculated numbers (tie at the leaderboard
cutoff, employee with no logs, mid-month joiner, weekend record, overnight shift, gap days in the trend),
concurrent punch-in/punch-out, the audit trail and the explain endpoint (IXSCAN, no COLLSCAN).

## Status

All endpoints in `openapi.yaml` are implemented. TODO before submitting: record here which of the tests above
you actually ran and their result, plus anything that does not work.

See `REVIEW.md` (review of the starter) and `DECISIONS.md` (design answers).
