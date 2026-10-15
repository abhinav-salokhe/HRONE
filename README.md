# Employee Attendance & Analytics API

FastAPI service backed by MongoDB for employee records, punch-in/out, manual regularization, and attendance analytics. The HTTP contract and business rules (R1–R10) are defined in `openapi.yaml`; storage shapes are in `DATA_MODEL.md`.

## What is implemented

- All endpoints in `openapi.yaml`, including punch-out, regularization, analytics, and `/admin/explain/{endpoint}`
- IST attendance dates, overnight shifts, derived fields (late, overtime, work hours, half day)
- Unique indexes created at startup; health check pings MongoDB

## Quick Start with Docker Compose

Ensure Docker (or Docker Desktop) is running.

### 1. Build and Start Services
Start MongoDB and the FastAPI application:

```bash
docker compose up --build
```
*(Add `-d` to run in detached/background mode).*

### 2. Seed Sample Data
Load the initial sample documents from `sample_data/` into MongoDB:

```bash
docker compose run --rm seed
```

> **Tip:** You can also start the services and seed data in a single command using the `seed` profile:
> ```bash
> docker compose --profile seed up --build
> ```

### 3. Endpoints & Access
- **Interactive API Docs (Swagger UI):** http://localhost:8000/docs
- **Alternative Docs (ReDoc):** http://localhost:8000/redoc
- **Readiness Health Check:** http://localhost:8000/health
- **MongoDB:** `mongodb://localhost:27017` (database: `attendance_db`)

### 4. Stop Services
```bash
docker compose down
```
*(To also wipe stored MongoDB volume data, run `docker compose down -v`).*

## Local Setup (Without Docker Compose)

If running the Python service directly on your host machine:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Run a standalone MongoDB 7 container or local instance:

```bash
docker run -d -p 27017:27017 --name local-mongo mongo:7
```

Set `MONGO_URI` and `MONGO_DB` (optional `.env`; environment variables take precedence):

```bash
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

## Submit

Public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this `README.md`.
