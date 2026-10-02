import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.iiko import sync as iiko_sync
from app.iiko.client import IikoClientError, IikoMutationBlocked
from app.loyalty import service
from app.models import IikoLink, LoyaltyState, LoyaltyTier, SyncStatus, User

from .conftest import TEST_GUEST_PHONE

USER_ID = uuid.uuid4()
CUSTOMER_ID = "guest-1"
HISTORY_FROM = datetime(2026, 9, 20, tzinfo=timezone.utc)
SYNCED_BEFORE = datetime(2026, 9, 29, tzinfo=timezone.utc)


class FakeSession:
    def __init__(self, user: User | None = None, results: list | None = None):
        self.user = user
        self.results = list(results or [])
        self.in_transaction = False
        self.added: list = []

    def begin_work(self) -> None:
        self.in_transaction = True

    async def get(self, entity, key, **kwargs):
        self.begin_work()
        return self.user if entity is User else None

    async def execute(self, statement):
        self.begin_work()
        value = self.results.pop(0)
        return type("Result", (), {"scalar_one_or_none": lambda self: value})()

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.in_transaction = False


def _tier(code: str, min_spend: str, category_id: str | None, *, active: bool = True) -> LoyaltyTier:
    return LoyaltyTier(
        code=code,
        name=code,
        min_spend=Decimal(min_spend),
        discount_percent=Decimal("10"),
        cashback_percent=Decimal("0"),
        perks=[],
        iiko_category_id=category_id,
        sort_order=0,
        is_active=active,
    )


TIERS = [
    _tier("bronze", "0", None),
    _tier("silver", "5000", "cat-silver"),
    _tier("gold", "10000", "cat-gold"),
]


def _tx(type_name: str, pos_order_id: str, tx_id: str, **extra) -> dict:
    row = {
        "id": tx_id,
        "typeName": type_name,
        "posOrderId": pos_order_id,
        "orderNumber": 7,
        "terminalGroupId": "tg-1",
        "whenCreated": "2026-09-25 10:00:00.000",
        "whenCreatedOrder": "2026-09-25T10:00:00Z",
        "walletId": "bonus-wallet",
        "isIgnored": False,
        "sum": 0,
    }
    row.update(extra)
    return row


class FakeStore:
    def __init__(self, link: IikoLink | None, state: LoyaltyState | None, tiers=TIERS, stored=None):
        self.link = link
        self.state = state
        self.tiers = tiers
        self.stored: dict[str, dict] = dict(stored or {})

    def install(self, monkeypatch) -> None:
        async def load_link(db, user_id):
            db.begin_work()
            return self.link

        async def load_state(db, user_id):
            db.begin_work()
            return self.state

        async def history_start(db, user_id, settings, now):
            db.begin_work()
            return HISTORY_FROM

        async def earliest_purchase(db, user_id, pos_order_ids):
            db.begin_work()
            return min(
                (self.stored[order_id]["ordered_at"] for order_id in pos_order_ids if order_id in self.stored),
                default=None,
            )

        async def store_purchases(db, user_id, purchases):
            db.begin_work()
            self.stored.update({purchase["pos_order_id"]: purchase for purchase in purchases})

        async def load_tiers(db, *, active_only):
            db.begin_work()
            return [tier for tier in self.tiers if tier.is_active or not active_only]

        async def total_savings(db, user_id):
            db.begin_work()
            return sum((purchase["discount_sum"] for purchase in self.stored.values()), Decimal(0))

        monkeypatch.setattr(service, "load_link", load_link)
        monkeypatch.setattr(service, "load_state", load_state)
        monkeypatch.setattr(service, "_history_start", history_start)
        monkeypatch.setattr(service, "_earliest_purchase", earliest_purchase)
        monkeypatch.setattr(service, "_store_purchases", store_purchases)
        monkeypatch.setattr(service, "_load_tiers", load_tiers)
        monkeypatch.setattr(service, "_total_savings", total_savings)


class FakeIiko:
    def __init__(
        self,
        *,
        total_spend: str = "0",
        wallets: list | None = None,
        categories: list[str] | None = None,
        transactions=None,
        fail_on: str | None = None,
        blocked: bool = False,
    ):
        self.total_spend = Decimal(total_spend)
        self.customer = {
            "id": CUSTOMER_ID,
            "walletBalances": wallets or [],
            "categories": [{"id": category_id} for category_id in categories or []],
        }
        self.transactions = transactions or (lambda date_from: [])
        self.fail_on = fail_on
        self.blocked = blocked
        self.calls: list[tuple] = []

    async def _call(self, db, name: str, *args):
        assert not db.in_transaction, f"{name} waited on iiko inside an open DB transaction"
        self.calls.append((name, *args))
        if name == self.fail_on:
            raise IikoClientError(f"iiko {name} responded 503", 503)
        await db.commit()

    async def get_customer_by_id(self, customer_id, db=None):
        await self._call(db, "customer", customer_id)
        return self.customer

    async def get_orders_sum_all_time(self, customer_id, db=None):
        await self._call(db, "orders_sum", customer_id)
        return self.total_spend

    async def list_terminal_groups(self, db=None):
        await self._call(db, "terminal_groups")
        return {"tg-1": "Сходненская 35"}

    async def get_transactions(self, customer_id, date_from, date_to, db=None):
        await self._call(db, "transactions", date_from)
        return self.transactions(date_from)

    async def add_customer_category(self, *, customer_id, category_id, guard_phone, db=None):
        self._guard()
        await self._call(db, "add_category", category_id)

    async def remove_customer_category(self, *, customer_id, category_id, guard_phone, db=None):
        self._guard()
        await self._call(db, "remove_category", category_id)

    def _guard(self) -> None:
        if self.blocked:
            raise IikoMutationBlocked("iiko mutations disabled via kill switch")

    def names(self) -> list[str]:
        return [name for name, *_ in self.calls]


def _link(status: SyncStatus = SyncStatus.synced, customer_id: str | None = CUSTOMER_ID) -> IikoLink:
    return IikoLink(user_id=USER_ID, sync_status=status, iiko_customer_id=customer_id, card_number="1234")


def _state() -> LoyaltyState:
    return LoyaltyState(
        user_id=USER_ID,
        total_spend=Decimal("6000"),
        bonus_balance=Decimal("0"),
        total_savings=Decimal("0"),
        tier_code="silver",
        synced_category_id="cat-silver",
        synced_at=SYNCED_BEFORE,
        last_error=None,
    )


@pytest.fixture
def sync_env(monkeypatch, make_settings):
    def setup(store: FakeStore, **settings_overrides) -> FakeSession:
        store.install(monkeypatch)
        settings = make_settings(**settings_overrides)

        async def effective_settings():
            return settings

        monkeypatch.setattr(service, "get_effective_settings", effective_settings)
        return FakeSession(User(id=USER_ID, phone=TEST_GUEST_PHONE, name="Анна"))

    return setup


TIER_SYNC = {"loyalty_tiers_enabled": True, "loyalty_tier_sync_enabled": True}


class TestTierCategorySync:
    async def test_unmapped_tier_removes_previous_tier_category(self, sync_env):
        state = _state()
        db = sync_env(FakeStore(_link(), state), **TIER_SYNC)
        iiko = FakeIiko(total_spend="1000", categories=["cat-bonus-program", "cat-silver"])
        await service._sync(db, USER_ID, iiko)
        assert ("remove_category", "cat-silver") in iiko.calls
        assert "add_category" not in iiko.names()
        assert state.tier_code == "bronze"
        assert state.synced_category_id is None
        assert state.last_error is None

    async def test_no_tier_removes_every_tier_category(self, sync_env):
        tiers = [_tier("silver", "5000", "cat-silver"), _tier("gold", "10000", "cat-gold", active=False)]
        state = _state()
        db = sync_env(FakeStore(_link(), state, tiers=tiers), **TIER_SYNC)
        iiko = FakeIiko(total_spend="100", categories=["cat-gold", "cat-bonus-program"])
        await service._sync(db, USER_ID, iiko)
        assert [call for call in iiko.calls if "category" in call[0]] == [("remove_category", "cat-gold")]
        assert state.tier_code is None
        assert state.synced_category_id is None

    async def test_mapped_tier_is_applied(self, sync_env):
        state = _state()
        db = sync_env(FakeStore(_link(), state), **TIER_SYNC)
        iiko = FakeIiko(total_spend="12000", categories=["cat-silver"])
        await service._sync(db, USER_ID, iiko)
        assert [call for call in iiko.calls if "category" in call[0]] == [
            ("add_category", "cat-gold"),
            ("remove_category", "cat-silver"),
        ]
        assert state.tier_code == "gold"
        assert state.synced_category_id == "cat-gold"

    @pytest.mark.parametrize("flags", [{"loyalty_tiers_enabled": True}, {"loyalty_tier_sync_enabled": True}, {}])
    async def test_turning_tiers_or_sync_off_removes_applied_tier_categories(self, sync_env, flags):
        state = _state()
        db = sync_env(FakeStore(_link(), state), **flags)
        iiko = FakeIiko(total_spend="12000", categories=["cat-bonus-program", "cat-silver", "cat-gold"])
        await service._sync(db, USER_ID, iiko)
        assert [call for call in iiko.calls if "category" in call[0]] == [
            ("remove_category", "cat-silver"),
            ("remove_category", "cat-gold"),
        ]
        assert state.synced_category_id is None
        assert state.last_error is None

    async def test_sync_off_without_applied_category_touches_nothing(self, sync_env):
        state = _state()
        state.synced_category_id = None
        db = sync_env(FakeStore(_link(), state), loyalty_tiers_enabled=True)
        iiko = FakeIiko(total_spend="12000", categories=["cat-silver"])
        await service._sync(db, USER_ID, iiko)
        assert not [name for name in iiko.names() if "category" in name]
        assert state.synced_category_id is None

    async def test_changed_tier_category_replaces_the_old_one_add_first(self, sync_env):
        tiers = [_tier("bronze", "0", None), _tier("silver", "5000", "cat-silver-new"), _tier("gold", "10000", "cat-gold")]
        state = _state()
        db = sync_env(FakeStore(_link(), state, tiers=tiers), **TIER_SYNC)
        iiko = FakeIiko(total_spend="6000", categories=["cat-bonus-program", "CAT-SILVER"])
        await service._sync(db, USER_ID, iiko)
        assert [call for call in iiko.calls if "category" in call[0]] == [
            ("add_category", "cat-silver-new"),
            ("remove_category", "CAT-SILVER"),
        ]
        assert state.synced_category_id == "cat-silver-new"

    async def test_retired_category_is_removed_once_tier_sync_is_off(self, sync_env):
        state = _state()
        db = sync_env(FakeStore(_link(), state, tiers=[_tier("bronze", "0", None)]))
        iiko = FakeIiko(total_spend="6000", categories=["cat-silver", "cat-bonus-program"])
        await service._sync(db, USER_ID, iiko)
        assert [call for call in iiko.calls if "category" in call[0]] == [("remove_category", "cat-silver")]
        assert state.synced_category_id is None

    async def test_blocked_cleanup_keeps_the_applied_category_for_later(self, sync_env):
        state = _state()
        db = sync_env(FakeStore(_link(), state))
        iiko = FakeIiko(total_spend="6000", categories=["cat-silver"], blocked=True)
        await service._sync(db, USER_ID, iiko)
        assert not [name for name in iiko.names() if "category" in name]
        assert state.synced_category_id == "cat-silver"
        assert "kill switch" in state.last_error
        assert db.in_transaction is False

    async def test_category_failure_is_recorded_after_snapshot(self, sync_env):
        state = _state()
        db = sync_env(FakeStore(_link(), state), **TIER_SYNC)
        iiko = FakeIiko(total_spend="1000", categories=["cat-silver"], fail_on="remove_category")
        await service._sync(db, USER_ID, iiko)
        assert state.synced_at > SYNCED_BEFORE
        assert state.total_spend == Decimal("1000")
        assert state.synced_category_id == "cat-silver"
        assert "remove_category" in state.last_error
        assert db.in_transaction is False


class TestBonusWallet:
    async def test_configured_wallet_absent_from_guest_filters_coffee_rows(self, sync_env):
        store = FakeStore(_link(), _state())
        db = sync_env(store, loyalty_bonus_wallet_id="bonus-wallet", coffee_stamp_wallet_id="coffee-wallet")
        rows = [
            _tx("CloseOrder", "order-a", "t1", orderSum=300, sum=270),
            _tx("DiscountSum", "order-a", "t2", sum=30),
            _tx("RefillWalletFromOrder", "order-a", "t3", sum=1, walletId="coffee-wallet"),
            _tx("PayFromWallet", "order-a", "t4", sum=7, walletId="coffee-wallet"),
        ]
        iiko = FakeIiko(
            wallets=[{"id": "coffee-wallet", "type": 1, "balance": 3}], transactions=lambda date_from: rows
        )
        await service._sync(db, USER_ID, iiko)
        purchase = store.stored["order-a"]
        assert purchase["bonus_accrued"] == Decimal("0.00")
        assert purchase["bonus_spent"] == Decimal("0.00")
        assert purchase["discount_sum"] == Decimal("30.00")
        assert store.state.bonus_balance == Decimal("0.00")
        assert store.state.coffee_collected == 3

    async def test_env_coffee_wallet_stays_excluded_when_override_is_cleared(self, sync_env, monkeypatch, make_settings):
        store = FakeStore(_link(), _state())
        db = sync_env(store, coffee_stamp_wallet_id="")
        monkeypatch.setattr(service, "get_settings", lambda: make_settings(coffee_stamp_wallet_id="COFFEE-WALLET"))
        wallets = [
            {"id": "coffee-wallet", "type": 1, "balance": 3},
            {"id": "bonus-2", "type": 1, "balance": 50},
        ]
        await service._sync(db, USER_ID, FakeIiko(wallets=wallets))
        assert store.state.bonus_balance == Decimal("50.00")
        assert store.state.coffee_collected is None

    async def test_type_fallback_skips_coffee_wallet(self, sync_env):
        store = FakeStore(_link(), _state())
        db = sync_env(store, coffee_stamp_wallet_id="coffee-wallet")
        rows = [
            _tx("CloseOrder", "order-a", "t1", orderSum=300, sum=300),
            _tx("RefillWalletFromOrder", "order-a", "t2", sum=1, walletId="coffee-wallet"),
            _tx("RefillWalletFromOrder", "order-a", "t3", sum=20, walletId="bonus-2"),
        ]
        wallets = [
            {"id": "coffee-wallet", "type": 1, "balance": 3},
            {"id": "bonus-2", "type": 1, "balance": 50},
        ]
        iiko = FakeIiko(wallets=wallets, transactions=lambda date_from: rows)
        await service._sync(db, USER_ID, iiko)
        assert store.stored["order-a"]["bonus_accrued"] == Decimal("20.00")
        assert store.state.bonus_balance == Decimal("50.00")


STORED_AT = HISTORY_FROM - timedelta(days=5)


def _stored_before_window() -> dict:
    return {
        "order-a": {
            "pos_order_id": "order-a",
            "ordered_at": STORED_AT,
            "discount_sum": Decimal("15.00"),
            "bonus_accrued": Decimal("0.00"),
        }
    }


class TestLateRows:
    async def test_late_accrual_for_stored_order_rescans_from_that_order(self, sync_env):
        store = FakeStore(_link(), _state(), stored=_stored_before_window())
        db = sync_env(store, loyalty_bonus_wallet_id="bonus-wallet")
        late_refill = _tx("RefillWalletFromOrder", "order-a", "t3", sum=25)
        window = [late_refill, _tx("CloseOrder", "order-b", "t4", orderSum=100, sum=100)]
        full = [
            _tx("CloseOrder", "order-a", "t1", orderSum=150, sum=135, whenCreatedOrder="2026-09-15T09:00:00Z"),
            _tx("DiscountSum", "order-a", "t2", sum=15),
            *window,
        ]
        iiko = FakeIiko(transactions=lambda date_from: window if date_from == HISTORY_FROM else full)
        await service._sync(db, USER_ID, iiko)
        rescan_from = STORED_AT - timedelta(days=3)
        assert [call for call in iiko.calls if call[0] == "transactions"] == [
            ("transactions", HISTORY_FROM),
            ("transactions", rescan_from),
        ]
        assert store.stored["order-a"]["bonus_accrued"] == Decimal("25.00")
        assert store.stored["order-a"]["discount_sum"] == Decimal("15.00")
        assert "order-b" in store.stored

    async def test_late_cancellation_reduces_stored_accrual(self, sync_env):
        store = FakeStore(_link(), _state(), stored=_stored_before_window())
        db = sync_env(store, loyalty_bonus_wallet_id="bonus-wallet")
        cancel = _tx("CancelRefillWalletFromOrder", "order-a", "t4", sum=-10)
        full = [
            _tx("CloseOrder", "order-a", "t1", orderSum=150, sum=150),
            _tx("RefillWalletFromOrder", "order-a", "t2", sum=25),
            cancel,
        ]
        iiko = FakeIiko(transactions=lambda date_from: [cancel] if date_from == HISTORY_FROM else full)
        await service._sync(db, USER_ID, iiko)
        assert store.stored["order-a"]["bonus_accrued"] == Decimal("15.00")

    async def test_orphans_of_unknown_orders_do_not_rescan(self, sync_env):
        store = FakeStore(_link(), _state(), stored=_stored_before_window())
        db = sync_env(store)
        iiko = FakeIiko(transactions=lambda date_from: [_tx("RefillWalletFromOrder", "order-x", "t1", sum=5)])
        await service._sync(db, USER_ID, iiko)
        assert iiko.names().count("transactions") == 1
        assert "order-x" not in store.stored

    async def test_orphan_inside_window_does_not_rescan(self, sync_env):
        stored = _stored_before_window()
        stored["order-a"]["ordered_at"] = HISTORY_FROM + timedelta(days=4)
        store = FakeStore(_link(), _state(), stored=stored)
        db = sync_env(store)
        iiko = FakeIiko(transactions=lambda date_from: [_tx("RefillWalletFromOrder", "order-a", "t1", sum=5)])
        await service._sync(db, USER_ID, iiko)
        assert iiko.names().count("transactions") == 1


class TestSyncEligibility:
    async def test_failed_relink_with_customer_keeps_syncing(self, sync_env):
        state = _state()
        db = sync_env(FakeStore(_link(SyncStatus.failed), state))
        iiko = FakeIiko(total_spend="7000")
        await service._sync(db, USER_ID, iiko)
        assert iiko.names()[:1] == ["customer"]
        assert state.total_spend == Decimal("7000")
        assert state.synced_at > SYNCED_BEFORE

    @pytest.mark.parametrize("link", [None, _link(SyncStatus.synced, customer_id=None)])
    async def test_without_customer_nothing_is_fetched(self, sync_env, link):
        db = sync_env(FakeStore(link, _state()))
        iiko = FakeIiko()
        await service._sync(db, USER_ID, iiko)
        assert iiko.calls == []

    async def test_iiko_failure_keeps_snapshot_and_records_error(self, sync_env):
        state = _state()
        store = FakeStore(_link(), state)
        db = sync_env(store)
        iiko = FakeIiko(total_spend="9000", fail_on="orders_sum")
        await service._sync(db, USER_ID, iiko)
        assert state.synced_at == SYNCED_BEFORE
        assert state.total_spend == Decimal("6000")
        assert "orders_sum" in state.last_error
        assert store.stored == {}
        assert db.in_transaction is False


class TestConnectionRelease:
    async def test_no_iiko_call_waits_inside_a_db_transaction(self, sync_env):
        store = FakeStore(_link(), _state(), stored=_stored_before_window())
        db = sync_env(store, loyalty_bonus_wallet_id="bonus-wallet", **TIER_SYNC)
        late = [_tx("RefillWalletFromOrder", "order-a", "t9", sum=5)]
        full = [_tx("CloseOrder", "order-a", "t1", orderSum=10, sum=10), *late]
        iiko = FakeIiko(
            total_spend="12000",
            categories=["cat-silver"],
            transactions=lambda date_from: late if date_from == HISTORY_FROM else full,
        )
        await service._sync(db, USER_ID, iiko)
        assert iiko.names() == [
            "customer",
            "orders_sum",
            "terminal_groups",
            "transactions",
            "transactions",
            "add_category",
            "remove_category",
        ]
        assert db.in_transaction is False

    async def test_relink_releases_connection_before_calling_iiko(self, monkeypatch):
        user = User(id=USER_ID, phone=TEST_GUEST_PHONE, name="Анна", surname="")
        link = _link(SyncStatus.failed)
        db = FakeSession(results=[user, link])

        @asynccontextmanager
        async def session_factory():
            yield db

        class RelinkIiko:
            async def find_guest_by_phone(self, phone, db=None):
                assert not db.in_transaction
                raise IikoClientError("iiko customer/info responded 503", 503)

        monkeypatch.setattr(iiko_sync, "async_session_factory", session_factory)
        await iiko_sync.sync_user_with_iiko(USER_ID, RelinkIiko())
        assert link.sync_status == SyncStatus.failed
        assert link.iiko_customer_id == CUSTOMER_ID
        assert db.in_transaction is False


class TestRunners:
    @pytest.fixture
    def recorded(self, monkeypatch):
        events: list[str] = []

        @asynccontextmanager
        async def session_factory():
            yield FakeSession()

        async def link(user_id, iiko):
            events.append("link")

        async def sync(db, user_id, iiko):
            events.append("sync")

        monkeypatch.setattr(service, "async_session_factory", session_factory)
        monkeypatch.setattr(service, "sync_user_with_iiko", link)
        monkeypatch.setattr(service, "_sync", sync)
        return events

    async def test_relink_runs_before_loyalty_sync(self, recorded):
        await service.run_loyalty_sync(USER_ID, object(), relink=True)
        assert recorded == ["link", "sync"]

    async def test_plain_sync_does_not_relink(self, recorded):
        await service.run_loyalty_sync(USER_ID, object())
        assert recorded == ["sync"]

    @pytest.mark.parametrize(("claimed", "expected"), [(True, ["sync"]), (False, [])])
    async def test_job_sync_requires_claim(self, recorded, monkeypatch, claimed, expected):
        async def claim(db, user_id, settings, now, *, force):
            assert force is False
            return claimed

        monkeypatch.setattr(service, "claim_sync", claim)
        await service.sync_user_loyalty(USER_ID, object())
        assert recorded == expected


class TestReviewDemo:
    async def test_demo_user_is_never_synced(self, sync_env):
        db = sync_env(
            FakeStore(_link(), _state()),
            review_bypass_phone=TEST_GUEST_PHONE,
            review_demo_card_number="7777000011112222",
        )
        iiko = FakeIiko(total_spend="9000")
        await service._sync(db, USER_ID, iiko)
        assert iiko.calls == []


class TestReferralHook:
    async def test_rewards_run_after_snapshot_with_effective_settings(self, sync_env, monkeypatch):
        state = _state()
        db = sync_env(
            FakeStore(_link(), state),
            loyalty_bonus_wallet_id="bonus-wallet",
            referral_rewards_enabled=True,
            referral_bonus_amount=300,
        )
        seen = []

        async def process(session, iiko, user, settings):
            assert not session.in_transaction
            seen.append(
                (user.id, settings.referral_rewards_enabled, settings.referral_bonus_amount, settings.loyalty_bonus_wallet_id)
            )
            assert state.synced_at > SYNCED_BEFORE

        monkeypatch.setattr(service, "process_referral_reward", process)
        await service._sync(db, USER_ID, FakeIiko(total_spend="1000"))
        assert seen == [(USER_ID, True, 300.0, "bonus-wallet")]

    async def test_rewards_never_get_a_guessed_wallet(self, sync_env, monkeypatch):
        state = _state()
        db = sync_env(FakeStore(_link(), state), referral_rewards_enabled=True, coffee_stamp_wallet_id="")
        seen = []

        async def process(session, iiko, user, settings):
            seen.append(settings.loyalty_bonus_wallet_id)

        monkeypatch.setattr(service, "process_referral_reward", process)
        wallets = [{"id": "coffee-wallet", "type": 1, "balance": 3}, {"id": "bonus-2", "type": 1, "balance": 50}]
        await service._sync(db, USER_ID, FakeIiko(wallets=wallets))
        assert seen == [""]

    async def test_rewards_are_skipped_when_iiko_read_fails(self, sync_env, monkeypatch):
        db = sync_env(FakeStore(_link(), _state()), referral_rewards_enabled=True)
        seen = []

        async def process(*args):
            seen.append(args)

        monkeypatch.setattr(service, "process_referral_reward", process)
        await service._sync(db, USER_ID, FakeIiko(fail_on="customer"))
        assert seen == []
