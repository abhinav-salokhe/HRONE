# Employee Attendance & Analytics API

FastAPI service backed by MongoDB for employee records, punch-in/out, manual regularization, and attendance analytics. The HTTP contract and business rules (R1–R10) are defined in `openapi.yaml`; storage shapes are in `DATA_MODEL.md`.

## What is implemented

- All endpoints in `openapi.yaml`, including punch-out, regularization, analytics, and `/admin/explain/{endpoint}`
- IST attendance dates, overnight shifts, derived fields (late, overtime, work hours, half day)
- Unique indexes created at startup; health check pings MongoDB

## Local setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` (optional `.env`; environment variables take precedence). Example local MongoDB 7:

```bash
docker run -p 27017:27017 mongo:7
```

Load sample documents (shapes only; grader data is larger):

```bash
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

Open `http://127.0.0.1:8000/docs` for interactive API docs.

## Submit

Public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this `README.md`. Do not commit `.env`, secrets, or a Dockerfile.
