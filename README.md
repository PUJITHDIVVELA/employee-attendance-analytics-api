# Employee Attendance & Analytics API

A high-performance backend API built with **Python 3.12**, **FastAPI**, and **MongoDB** for managing employee profiles, processing punch-in/punch-out attendance transactions with automated lateness and overtime derivations, handling manual regularizations with full audit history, and generating real-time analytics.

---

## Technical Stack
- **Language**: Python 3.11+ (Python 3.12 recommended)
- **Framework**: FastAPI
- **ASGI Server**: Uvicorn
- **Database**: MongoDB 6.0+ (Tested with MongoDB 8.0)
- **Driver**: PyMongo 4.7+
- **Data Validation**: Pydantic v2

---

## Environment Setup & Prerequisites

### 1. Prerequisites
- Python 3.11+
- MongoDB 6.0 or newer running locally or accessible via MongoDB Atlas.

### 2. Local Setup

Create and activate a virtual environment:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:
```bash
pip install -r requirements.txt
```

Configure environment variables:
```bash
cp .env.example .env
```
Ensure your `.env` contains:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

---

## Database Seeding & Startup

1. **Seed Sample Data**:
```bash
python sample_seed.py
```

2. **Start the API Application**:
```bash
uvicorn app.main:app --port 8000
```

3. **Swagger API Documentation**:
Once the application is running, open your browser and navigate to:
`http://localhost:8000/docs`

---

## Running Automated Tests

Run the test suite using `pytest`:
```bash
source .venv/bin/activate
PYTHONPATH=. pytest scratch/test_api.py scratch/test_seeded.py
```

---

## Core Implemented Endpoints

### System & Health
- `GET /health`: Readiness probe returning 200 OK when MongoDB ping succeeds, otherwise 503.

### Employee Management
- `POST /employees`: Create employee with `emp_code` unique index enforcement.
- `GET /employees`: List employees sorted by `emp_code` ascending with department filtering and pagination.

### Attendance Operations
- `POST /attendance/punch-in`: Record punch-in, calculate attendance date (R1) and lateness (R2).
- `POST /attendance/punch-out`: Record punch-out, calculate work hours (R4), overtime (R3), and half-day status (R5).
- `GET /attendance`: List attendance records with filtering (`emp_code`, `date_from`, `date_to`, `status`), sorted by date DESC and emp_code ASC.
- `PATCH /attendance/{emp_code}/{date}`: Regularize attendance records with full audit trailing in `history`.

### Analytics & Admin
- `GET /analytics/employees/{emp_code}/monthly`: Monthly attendance summary for one employee.
- `GET /analytics/departments/summary`: Monthly department summary aggregated across collections.
- `GET /analytics/leaderboard/late`: Late-comers competition leaderboard.
- `GET /analytics/departments/{department}/trend`: Daily attendance trend with 7-day moving average.
- `GET /admin/explain/{endpoint}`: Returns raw MongoDB query execution stats (`explain`) for query optimization analysis.

---

## Git Submission Notes
Included mandatory submission files:
- `app/main.py`
- `requirements.txt`
- `REVIEW.md`
- `DECISIONS.md`
- `README.md`
