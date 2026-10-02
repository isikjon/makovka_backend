# makovka_backend

FastAPI + PostgreSQL backend for the Пекарня loyalty app. Handles phone/OTP auth, iikoCloud POS integration (guest creation, card issuance, loyalty sync), app content (novelties, promotions, bakeries), referrals and the admin panel.

## Stack

- FastAPI, SQLAlchemy 2.0 (async), Alembic, asyncpg
- Jinja2 (server-rendered admin panel)
- PostgreSQL

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
alembic upgrade head
python -m app.admin.cli seed-media
uvicorn app.main:app --reload
```

## Structure

```
app/
  main.py                FastAPI app, mounts the admin panel at /admin
  config.py              settings (.env)
  runtime_settings.py    settings the admin can override at runtime (app_settings table, 15 s cache)
  db.py                  async engine/session
  models.py              SQLAlchemy models
  schemas.py             request/response models
  security.py            JWT + OTP hashing
  deps.py                auth dependency
  content.py             novelties, promotions (audience + date window), bakeries
  media.py               image validation, storage and seed media
  iiko/client.py         iikoCloud API client
  loyalty/rules.py       loyalty math and iiko payload parsing (pure functions)
  loyalty/service.py     loyalty sync with iiko and response assembly
  referral/service.py    referral codes and bonus top-ups for invited friends
  routers/auth.py        OTP + token endpoints
  routers/profile.py     profile endpoint
  routers/loyalty.py     loyalty status, purchase history, manual refresh
  routers/content.py     GET /content
  routers/referral.py    GET /referral, POST /referral/apply
  routers/media.py       GET /media/{filename} (public images)
  admin/web.py           admin panel (sub-application mounted at /admin)
  admin/security.py      admin password hashing, session cookie, CSRF, login throttling
  admin/forms.py         admin form validation
  admin/queries.py       admin listings, ordering, guests
  admin/cli.py           python -m app.admin.cli (admins, seed media)
  admin/templates/       Jinja2 templates
  admin/static/          admin stylesheet
  jobs/loyalty_sync.py   periodic tier sync and referral rewards (python -m app.jobs.loyalty_sync)
alembic/                 migrations
seed_media/              images referenced by the seeded novelties
deploy/                  systemd unit + timer for the sync job, nginx snippet for the admin panel
tests/                   unit tests
```

## API

All endpoints except `/health`, `/auth/*` and `/media/*` need `Authorization: Bearer <access token>`.

- `GET /content` — active novelties, promotions visible to the current guest (tier audiences use the same tier as `/loyalty`) and bakeries. In `coffee_stamps` promotions `{n}` is replaced with the number of the discounted cup (stamp goal + 1).
- `GET /referral` — the guest's referral code (created on first request), counters and share text.
- `POST /referral/apply` `{"code": "..."}` — attach the guest to the friend who invited them (once, case-insensitive).
- `GET /media/{filename}` — public images; `image_url` fields point to `PUBLIC_MEDIA_BASE_URL/<filename>`.

## Admin panel

Available at `https://app.top1makovka.ru/admin/`. Sections: novelties, promotions and personal offers, bakeries, loyalty tiers, settings, guests (read-only).

Production setup (once): create the media directory, add `MEDIA_DIR=/opt/makovka-api/media` to the server `.env`, then run the migration, copy the seed images and create an admin.

```
sudo mkdir -p /opt/makovka-api/media
sudo chown makovka-api:makovka-api /opt/makovka-api/media
cd /opt/makovka-api/app_src
sudo -u makovka-api /opt/makovka-api/venv/bin/alembic upgrade head
sudo -u makovka-api /opt/makovka-api/venv/bin/python -m app.admin.cli seed-media
sudo -u makovka-api /opt/makovka-api/venv/bin/python -m app.admin.cli create-user --login <login>
```

`create-user` and `set-password` take the password from `ADMIN_PASSWORD` (at least 12 characters) or generate one and print it once. Include `deploy/nginx-admin.conf` into the `app.top1makovka.ru` server block next to the `/api/` location and reload nginx.

Security: scrypt password hashes; session in a signed `admin_session` cookie (HttpOnly, Secure, SameSite=Strict, Path=/admin, 12 h) that stops working when the password changes, the admin logs out or is deactivated; CSRF token on every form; every login attempt counts against its IP before the password is checked and 5 failed logins per IP in 15 minutes lock that IP for the rest of the window (`X-Real-IP` is trusted only from 127.0.0.1); at most 2 password checks run at once, extra logins get 429; strict security headers on every admin response. Uploads accept only PNG/JPEG/WebP (checked by content), up to `MEDIA_MAX_BYTES`, stored under random names.

Settings changed in the admin (tiers mode, tier sync, flat discount, coffee stamp wallet and goal, referral rewards and amount) override `.env` and apply within 15 seconds. `IIKO_ENV`, `IIKO_MUTATIONS_ENABLED` and `IIKO_TEST_GUEST_PHONE` are `.env`-only.

## Referral rewards

Off by default (`REFERRAL_REWARDS_ENABLED`). When enabled, the loyalty sync of an invited guest who has a purchase made after the code was applied tops up the wallet `LOYALTY_BONUS_WALLET_ID` of the guest and of the friend who invited them by `REFERRAL_BONUS_AMOUNT`, once per side. Without `LOYALTY_BONUS_WALLET_ID` nothing is topped up and the reward waits with an error. Both guests must be linked to iiko; in `IIKO_ENV=staging` only the test guest is topped up, and blocked top-ups (staging or `IIKO_MUTATIONS_ENABLED=false`) are retried later without using up attempts. Each side is marked in the database before iiko is called; when the outcome is unclear (timeout, 5xx, interrupted run) the reward is never retried and needs a manual check in iiko. The periodic job also processes pending rewards when tier sync is off.

## Loyalty tiers

With tiers and tier sync on, each guest gets the iiko category of their tier (added before the old one is removed); only categories of tiers and the category the guest last received from the app are ever removed. After a tier's category is changed, or tiers or tier sync are turned off, the next sync removes the previously assigned categories (the periodic job picks up those guests). Active tiers must have different thresholds, a category can belong to one tier only, and category and wallet IDs are stored as canonical UUIDs.

## Store review account

With `REVIEW_BYPASS_PHONE`, `REVIEW_BYPASS_CODE` and `REVIEW_DEMO_CARD_NUMBER` set, the review account logs in with the fixed code, sees the demo card as synced and is never linked or synced with iiko.

## Tests

```
pip install -e ".[dev]"
python -m pytest -q
```
