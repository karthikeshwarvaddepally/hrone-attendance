# Employee Attendance and Analytics API

Python 3.11+, FastAPI, PyMongo, MongoDB 6.0+. All application logic is in `app/main.py`.

## Run

```sh
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# Linux/macOS instead: source .venv/bin/activate
pip install -r requirements.txt
```

Start local MongoDB, or use Atlas. Example local command:

```sh
docker run -d --name hrone-mongo -p 127.0.0.1:27017:27017 mongo:7
```

PowerShell configuration:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

Linux/macOS equivalent:

```sh
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000
```

Open `http://localhost:8000/docs`. Health pings MongoDB; indexes are created automatically. No seeding is required to start. The original kit's seed script can be run separately. Environment variables override a local `.env`.

## Tests and verification status

```sh
pip install -r requirements-dev.txt
python -m pytest -q
```

**26 tests passed; 6 real-MongoDB integration tests and 1 optional scale test were skipped in the build environment.** Default tests use mongomock for CRUD and Python for time helpers. MongoDB 7.0.16 could not start in that environment (`open: Operation not permitted`). Aggregation results, actual server concurrency, explain plans, startup time against seeded data, and 100,000-record performance remain unverified. Mock results do not prove those properties.

Run the full suite on your MongoDB instance (PowerShell):

```powershell
$env:TEST_MONGO_URI = "mongodb://localhost:27017"
python -m pytest -q
```

Linux/macOS:

```sh
TEST_MONGO_URI='mongodb://localhost:27017' python -m pytest -q
```

Tests create and delete their own uniquely named temporary database. The database account needs permission to create indexes and drop that database. Optional scale test:

```powershell
$env:RUN_SCALE_TESTS = "1"
python -m pytest -q tests/test_scale.py
```

## Notes

All requested routes are implemented. Analytics and explain share pipeline builders. Business dates are IST strings; instants are UTC BSON dates returned as integer milliseconds. Corrections use a conditional atomic update, an internal version and `$push` for audit history; the version is never returned.

Read `REVIEW.md` and `DECISIONS.md`. Rewrite design answers in your own words after understanding the implementation. No secrets, environment files, Dockerfile, virtual environment or data dumps are included.
