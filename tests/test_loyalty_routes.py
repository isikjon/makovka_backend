import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.db import get_db
from app.deps import get_current_user, get_iiko_client
from app.loyalty import service
from app.main import app
from app.models import IikoLink, LoyaltyState, SyncStatus, User
from app.routers import loyalty as loyalty_routes
from app.runtime_settings import get_effective_settings
from app.schemas import PurchaseHistoryOut

USER_ID = uuid.uuid4()
CARD = "1234567890123456"
IIKO = object()


class RouteSession:
    def __init__(self):
        self.in_transaction = False

    def begin_work(self) -> None:
        self.in_transaction = True

    async def commit(self) -> None:
        self.in_transaction = False


@dataclass
class Env:
    link: IikoLink | None = None
    state: LoyaltyState | None = None
    name: str = "Анна"
    claim_result: bool = True
    claims: list[bool] = field(default_factory=list)
    scheduled: list[tuple] = field(default_factory=list)
    session: RouteSession = field(default_factory=RouteSession)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _link(status: SyncStatus = SyncStatus.synced, customer_id: str | None = "guest-1", updated_ago=timedelta(0)):
    return IikoLink(
        user_id=USER_ID,
        sync_status=status,
        iiko_customer_id=customer_id,
        card_number=CARD if customer_id else None,
        updated_at=_now() - updated_ago,
    )


def _state(synced_ago: timedelta, updated_ago: timedelta) -> LoyaltyState:
    return LoyaltyState(
        user_id=USER_ID,
        total_spend=Decimal("7500"),
        bonus_balance=Decimal("40"),
        total_savings=Decimal("300"),
        synced_at=_now() - synced_ago,
        updated_at=_now() - updated_ago,
        last_error=None,
    )


@pytest.fixture
def env(monkeypatch, make_settings):
    current = Env()
    settings = make_settings(loyalty_tiers_enabled=True)

    async def fake_db():
        yield current.session

    async def load_link(db, user_id):
        db.begin_work()
        return current.link

    async def load_state(db, user_id):
        db.begin_work()
        return current.state

    async def claim_sync(db, user_id, settings, now, *, force):
        current.claims.append(force)
        await db.commit()
        return current.claim_result

    async def mark_link_pending(db, user_id):
        current.link = _link(SyncStatus.pending, customer_id=current.link.iiko_customer_id if current.link else None)
        await db.commit()
        return current.link

    async def run_loyalty_sync(user_id, iiko, *, relink=False):
        current.scheduled.append((user_id, iiko, relink, current.session.in_transaction))

    async def load_tiers(db, *, active_only):
        db.begin_work()
        return []

    async def build_history_out(db, user_id, state, *, limit, offset):
        db.begin_work()
        return PurchaseHistoryOut(items=[], total=0, updated_at=state.synced_at if state else None)

    monkeypatch.setattr(service, "_load_tiers", load_tiers)
    monkeypatch.setattr(loyalty_routes, "load_link", load_link)
    monkeypatch.setattr(loyalty_routes, "load_state", load_state)
    monkeypatch.setattr(loyalty_routes, "claim_sync", claim_sync)
    monkeypatch.setattr(service, "claim_sync", claim_sync)
    monkeypatch.setattr(loyalty_routes, "mark_link_pending", mark_link_pending)
    monkeypatch.setattr(loyalty_routes, "run_loyalty_sync", run_loyalty_sync)
    monkeypatch.setattr(loyalty_routes, "build_history_out", build_history_out)

    app.dependency_overrides[get_current_user] = lambda: User(id=USER_ID, phone="+79990000001", name=current.name)
    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_iiko_client] = lambda: IIKO
    app.dependency_overrides[get_effective_settings] = lambda: settings
    yield current
    app.dependency_overrides.clear()


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


class TestGetLoyalty:
    async def test_first_open_answers_from_link_and_syncs_in_background(self, env, client):
        env.link = _link()
        response = await client.get("/loyalty")
        assert response.status_code == 200
        body = response.json()
        assert body["card_number"] == CARD
        assert body["sync_status"] == "synced"
        assert body["stale"] is True
        assert body["total_spend"] == 0.0
        assert env.claims == [False]
        assert env.scheduled == [(USER_ID, IIKO, False, False)]

    async def test_fresh_state_schedules_nothing(self, env, client):
        env.link = _link()
        env.state = _state(synced_ago=timedelta(minutes=1), updated_ago=timedelta(minutes=1))
        body = (await client.get("/loyalty")).json()
        assert body["stale"] is False
        assert body["total_spend"] == 7500.0
        assert env.claims == []
        assert env.scheduled == []

    async def test_expired_state_is_refreshed_in_background(self, env, client):
        env.link = _link()
        env.state = _state(synced_ago=timedelta(minutes=15), updated_ago=timedelta(minutes=15))
        body = (await client.get("/loyalty")).json()
        assert body["stale"] is True
        assert body["total_spend"] == 7500.0
        assert env.scheduled == [(USER_ID, IIKO, False, False)]

    async def test_sync_already_running_is_not_scheduled_again(self, env, client):
        env.link = _link()
        env.state = _state(synced_ago=timedelta(minutes=15), updated_ago=timedelta(seconds=10))
        body = (await client.get("/loyalty")).json()
        assert body["stale"] is True
        assert env.claims == []
        assert env.scheduled == []

    async def test_lost_claim_does_not_schedule(self, env, client):
        env.link = _link()
        env.state = _state(synced_ago=timedelta(minutes=15), updated_ago=timedelta(minutes=15))
        env.claim_result = False
        body = (await client.get("/loyalty")).json()
        assert body["stale"] is True
        assert env.claims == [False]
        assert env.scheduled == []

    async def test_failed_relink_still_serves_cached_data(self, env, client):
        env.link = _link(SyncStatus.failed)
        env.state = _state(synced_ago=timedelta(minutes=1), updated_ago=timedelta(minutes=1))
        body = (await client.get("/loyalty")).json()
        assert body["sync_status"] == "failed"
        assert body["card_number"] == CARD
        assert body["total_spend"] == 7500.0
        assert body["bonus_balance"] == 40.0
        assert body["stale"] is True

    async def test_unlinked_user_never_syncs(self, env, client):
        body = (await client.get("/loyalty")).json()
        assert body["card_number"] is None
        assert body["stale"] is False
        assert env.claims == []
        assert env.scheduled == []


class TestHistory:
    @pytest.mark.parametrize("offset", [1_000_001, 10**20])
    async def test_out_of_range_offset_is_rejected(self, env, client, offset):
        response = await client.get("/loyalty/history", params={"offset": offset})
        assert response.status_code == 422

    async def test_largest_offset_is_accepted(self, env, client):
        assert (await client.get("/loyalty/history", params={"offset": 1_000_000})).status_code == 200

    async def test_first_load_syncs_in_background(self, env, client):
        env.link = _link()
        response = await client.get("/loyalty/history")
        assert response.json() == {"items": [], "total": 0, "updated_at": None}
        assert env.scheduled == [(USER_ID, IIKO, False, False)]


class TestRefresh:
    async def test_cooldown_is_rejected(self, env, client):
        env.link = _link()
        env.claim_result = False
        response = await client.post("/loyalty/refresh")
        assert response.status_code == 429
        assert response.json() == {"detail": "Обновлять можно не чаще раза в минуту"}
        assert env.scheduled == []

    async def test_linked_user_gets_forced_background_sync(self, env, client):
        env.link = _link()
        env.state = _state(synced_ago=timedelta(minutes=2), updated_ago=timedelta(minutes=2))
        response = await client.post("/loyalty/refresh")
        assert response.status_code == 200
        body = response.json()
        assert body["stale"] is True
        assert body["total_spend"] == 7500.0
        assert env.claims == [True]
        assert env.scheduled == [(USER_ID, IIKO, False, False)]

    @pytest.mark.parametrize(
        "link",
        [
            None,
            _link(SyncStatus.failed, customer_id=None),
            _link(SyncStatus.pending, customer_id=None, updated_ago=timedelta(minutes=30)),
        ],
    )
    async def test_missing_or_failed_link_is_retried(self, env, client, link):
        env.link = link
        body = (await client.post("/loyalty/refresh")).json()
        assert body["sync_status"] == "pending"
        assert body["card_number"] is None
        assert body["stale"] is True
        assert env.claims == [True]
        assert env.scheduled == [(USER_ID, IIKO, True, False)]

    async def test_failed_relink_with_customer_keeps_card_while_retrying(self, env, client):
        env.link = _link(SyncStatus.failed)
        env.state = _state(synced_ago=timedelta(minutes=2), updated_ago=timedelta(minutes=2))
        body = (await client.post("/loyalty/refresh")).json()
        assert body["card_number"] == CARD
        assert body["total_spend"] == 7500.0
        assert env.scheduled == [(USER_ID, IIKO, True, False)]

    async def test_link_in_progress_is_left_alone(self, env, client):
        env.link = _link(SyncStatus.pending, customer_id=None, updated_ago=timedelta(minutes=1))
        body = (await client.post("/loyalty/refresh")).json()
        assert body["sync_status"] == "pending"
        assert env.claims == []
        assert env.scheduled == []

    async def test_incomplete_profile_is_not_linked(self, env, client):
        env.name = "  "
        response = await client.post("/loyalty/refresh")
        assert response.status_code == 200
        assert response.json()["sync_status"] is None
        assert env.claims == []
        assert env.scheduled == []
