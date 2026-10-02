import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from app.config import get_settings, is_review_demo
from app.db import get_db
from app.deps import get_current_user, get_iiko_client
from app.iiko import sync as iiko_sync
from app.jobs.loyalty_sync import candidates_query, referral_rewards_active, tier_sync_enabled
from app.loyalty import service
from app.main import app
from app.models import User
from app.routers import loyalty as loyalty_routes
from app.routers import profile as profile_routes
from app.runtime_settings import get_effective_settings

DEMO_PHONE = "+79990000099"
DEMO_CARD = "7777000011112222"
IIKO = object()

DEMO = {"review_bypass_phone": DEMO_PHONE, "review_demo_card_number": DEMO_CARD}


class TestPredicate:
    def test_matches_only_the_bypass_phone(self, make_settings):
        settings = make_settings(**DEMO)
        assert is_review_demo(settings, DEMO_PHONE) is True
        assert is_review_demo(settings, "+79990000098") is False

    def test_needs_both_settings(self, make_settings):
        assert is_review_demo(make_settings(review_bypass_phone=DEMO_PHONE), DEMO_PHONE) is False
        assert is_review_demo(make_settings(review_demo_card_number=DEMO_CARD), "") is False


class Result:
    def scalar_one_or_none(self):
        return None


class RouteSession:
    def __init__(self):
        self.executed = 0

    async def execute(self, statement):
        self.executed += 1
        return Result()

    async def commit(self):
        pass


class Recorder:
    def __init__(self, user: User):
        self.user = user
        self.calls: list[str] = []
        self.session = RouteSession()


@pytest.fixture
def demo_env(monkeypatch, make_settings):
    settings = make_settings(**DEMO)
    recorder = Recorder(
        User(
            id=uuid.uuid4(),
            phone=DEMO_PHONE,
            name="Ревьюер",
            surname="",
            email="",
            birthdate="",
            gender="",
            promo_code="",
        )
    )
    user = recorder.user

    async def fake_db():
        yield recorder.session

    def record(name):
        async def recorded(*args, **kwargs):
            recorder.calls.append(name)
            return None

        return recorded

    async def load_tiers(db, *, active_only):
        return []

    for name in ("load_link", "load_state", "claim_sync", "claim_due_sync", "mark_link_pending", "run_loyalty_sync"):
        monkeypatch.setattr(loyalty_routes, name, record(name))
    monkeypatch.setattr(profile_routes, "link_user", record("link_user"))
    monkeypatch.setattr(service, "_load_tiers", load_tiers)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_iiko_client] = lambda: IIKO
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_effective_settings] = lambda: settings
    yield recorder
    app.dependency_overrides.clear()


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


class TestDemoRoutes:
    async def test_profile_reports_demo_card(self, demo_env, client):
        body = (await client.get("/profile")).json()
        assert body["iiko"] == {"sync_status": "synced", "card_number": DEMO_CARD, "last_error": None}
        assert demo_env.session.executed == 0

    async def test_profile_save_never_links(self, demo_env, client):
        response = await client.put("/profile", json={"name": "Ревьюер"})
        assert response.status_code == 200
        assert response.json()["iiko"]["card_number"] == DEMO_CARD
        assert demo_env.calls == []

    async def test_other_users_still_link(self, demo_env, client):
        demo_env.user.phone = "+79990000098"
        response = await client.put("/profile", json={"name": "Анна"})
        assert response.json()["iiko"] is None
        assert demo_env.calls == ["link_user"]

    @pytest.mark.parametrize(("method", "path"), [("GET", "/loyalty"), ("POST", "/loyalty/refresh")])
    async def test_loyalty_is_synced_demo_without_iiko(self, demo_env, client, method, path):
        response = await client.request(method, path)
        assert response.status_code == 200
        body = response.json()
        assert body["sync_status"] == "synced"
        assert body["card_number"] == DEMO_CARD
        assert body["stale"] is False
        assert demo_env.calls == []

    async def test_history_never_schedules_sync(self, demo_env, client, monkeypatch):
        async def build_history_out(db, user_id, state, *, limit, offset):
            return {"items": [], "total": 0, "updated_at": None}

        monkeypatch.setattr(loyalty_routes, "build_history_out", build_history_out)
        response = await client.get("/loyalty/history")
        assert response.status_code == 200
        assert demo_env.calls == ["load_link", "load_state"]


class FakeSession:
    def __init__(self, results):
        self.results = list(results)
        self.added = []

    async def execute(self, statement):
        value = self.results.pop(0)
        return type("Result", (), {"scalar_one_or_none": lambda self: value})()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass


class ExplodingIiko:
    def __getattr__(self, name):
        raise AssertionError(f"iiko.{name} must not be called for the review demo user")


class TestDemoNeverTouchesIiko:
    async def test_linking_is_skipped(self, monkeypatch, make_settings):
        user = User(id=uuid.uuid4(), phone=DEMO_PHONE, name="Ревьюер", surname="")
        db = FakeSession([user])

        @asynccontextmanager
        async def session_factory():
            yield db

        monkeypatch.setattr(iiko_sync, "async_session_factory", session_factory)
        monkeypatch.setattr(iiko_sync, "get_settings", lambda: make_settings(**DEMO))
        await iiko_sync.sync_user_with_iiko(user.id, ExplodingIiko())
        assert db.added == []

    def test_job_excludes_demo_user(self, make_settings):
        settings = make_settings(**DEMO, loyalty_tiers_enabled=True, loyalty_tier_sync_enabled=True)
        sql = str(candidates_query(settings).compile(dialect=postgresql.dialect()))
        assert "users.phone !=" in sql
        assert "referral_rewards" not in sql

    def test_job_without_demo(self, make_settings):
        settings = make_settings(loyalty_tiers_enabled=True, loyalty_tier_sync_enabled=True)
        assert "users.phone" not in str(candidates_query(settings).compile(dialect=postgresql.dialect()))


class TestJobModes:
    def test_modes(self, make_settings):
        assert tier_sync_enabled(make_settings(loyalty_tiers_enabled=True, loyalty_tier_sync_enabled=True)) is True
        assert tier_sync_enabled(make_settings(loyalty_tier_sync_enabled=True)) is False
        assert referral_rewards_active(make_settings(referral_rewards_enabled=True)) is True
        assert referral_rewards_active(make_settings(referral_rewards_enabled=True, referral_bonus_amount=0)) is False

    def test_tier_sync_off_still_visits_guests_with_applied_tier_categories(self, make_settings):
        sql = str(candidates_query(make_settings(loyalty_tiers_enabled=True)).compile(dialect=postgresql.dialect()))
        assert "loyalty_states.synced_category_id IS NOT NULL" in sql
        assert "referral_rewards" not in sql

    def test_rewards_only_mode_limits_to_users_awaiting_a_reward(self, make_settings):
        settings = make_settings(referral_rewards_enabled=True)
        sql = str(candidates_query(settings).compile(dialect=postgresql.dialect()))
        assert "users.referred_by_id IS NOT NULL" in sql
        assert "NOT (EXISTS" in sql
        assert "referral_rewards.attempts >=" in sql
