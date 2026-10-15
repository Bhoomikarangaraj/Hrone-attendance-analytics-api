# HROne Employee Attendance & Analytics API

This project implements the FastAPI attendance and analytics API defined by the assessment contract in `openapi.yaml` and the MongoDB model in `DATA_MODEL.md`.

## Overview

The service provides:
- employee creation and listing
- attendance punch-in / punch-out flows
- manual attendance regularization with audit history
- monthly employee analytics
- department summary and leaderboard metrics
- department trend analysis with rolling averages
- admin explain endpoint for query inspection

## Prerequisites

- Python 3.11
- MongoDB 7.0 or MongoDB Atlas
- a working virtual environment

## Local setup

```powershell
cd "C:\Users\srinithi varsha\Downloads\SDT_candidate_kit\candidate_kit"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python sample_seed.py
```

The application reads `MONGO_URI` and `MONGO_DB` from environment variables if present; otherwise it defaults to `mongodb://localhost:27017` and `attendance_db`.

## Run the API

```powershell
cd "C:\Users\srinithi varsha\Downloads\SDT_candidate_kit\candidate_kit"
.\.venv\Scripts\uvicorn.exe app.main:app --host 0.0.0.0 --port 8000
```

Then open:
- http://localhost:8000/
- http://localhost:8000/docs
- http://localhost:8000/health

## Test commands

```powershell
cd "C:\Users\srinithi varsha\Downloads\SDT_candidate_kit\candidate_kit"
.\.venv\Scripts\python.exe -m pytest -q
```

## Notes

- `0.0.0.0` is the server bind address, not the browser URL.
- Browser access should use `localhost` or `127.0.0.1`.
- Sample data is loaded by `sample_seed.py` and the app expects the `employees` and `attendance_logs` collections to exist.
- No secrets or `.env` files are committed to the repository.
