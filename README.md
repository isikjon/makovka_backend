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
  loyalty/rules.py            loyalty math and iiko payload parsing (pure functions)
  loyalty/service.py           loyalty sync with iiko and response assembly
  routers/auth.py             OTP + token endpoints
  routers/profile.py           profile endpoint
  routers/loyalty.py            loyalty status, purchase history, manual refresh
  jobs/loyalty_sync.py           periodic tier sync (python -m app.jobs.loyalty_sync)
alembic/                         migrations
deploy/                          systemd unit + timer for the loyalty sync job
tests/                           unit tests
```

## Tests

```
pip install -e ".[dev]"
python -m pytest -q
```
