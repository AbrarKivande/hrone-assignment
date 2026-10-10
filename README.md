# HROne Employee Attendance & Analytics API

This repository contains the completed submission for the HROne Software Engineer Trainee technical assignment.

## Completion Status
**This is a 100% complete submission.**
All endpoints, business rules (R1-R10), bug fixes, and MongoDB analytics aggregations (Part A, B, and C) have been fully implemented according to the OpenAPI contract. 

## Requirements
- Python 3.11+
- MongoDB 6.0+

## Local Setup
```bash
python -m venv .venv 
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB 
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

## Documentation
- `app/main.py`: Contains the complete, fixed Fastapi implementation with proper MongoDB queries guaranteeing IXSCAN patterns without looping in Python.
- `REVIEW.md`: Details the ~11 structural defects found natively in the starter code, and how they were patched.
- `DECISIONS.md`: Details my database index choices, race condition resolution behavior, dense-ranking methodology, headcount filter mapping, and scale-up suggestions. Code executes precisely handling overnight-shifts correctly, bounds, and Half-Up Rounding safely overcoming native MongoDB logic traps.
