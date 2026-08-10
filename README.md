# makovka_backend

FastAPI + PostgreSQL backend for the Маковка loyalty app. Handles phone/OTP auth and iikoCloud POS integration (guest creation, card issuance, loyalty sync).

## Stack

- FastAPI, SQLAlchemy 2.0 (async), Alembic, asyncpg
- PostgreSQL

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

## Structure

```
app/
  main.py          FastAPI app
  config.py         settings
  db.py              async engine/session
  models.py           SQLAlchemy models
  schemas.py            request/response models
  security.py             JWT + OTP hashing
  deps.py                   auth dependency
  iiko/client.py             iikoCloud API client
  routers/auth.py             OTP + token endpoints
  routers/profile.py           profile endpoint
alembic/                         migrations
```
