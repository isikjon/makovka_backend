import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.loyalty.service import (
    assemble_loyalty_out,
    in_cooldown,
    is_linked,
    is_stale,
    needs_refresh,
    needs_relink,
    sync_due,
)
from app.models import IikoLink, LoyaltyState, LoyaltyTier, SyncStatus

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
CARD = "1234567890123456"
PERK = "Бесплатный десерт на День рождения"

CONTRACT_KEYS = {
    "mode",
    "sync_status",
    "card_number",
    "current_tier",
    "next_tier",
    "total_spend",
    "remaining_to_next",
    "progress",
    "bonus_balance",
    "total_savings",
    "coffee",
    "tiers",
    "updated_at",
    "stale",
}
TIER_KEYS = {"code", "name", "min_spend", "discount_percent", "cashback_percent", "perks"}


def _tiers() -> list[LoyaltyTier]:
    seed = [
        ("bronze", "Бронза", "0", "10", "0", []),
        ("silver", "Серебро", "5000", "15", "5", [PERK]),
        ("gold", "Золото", "10000", "17", "7", [PERK]),
        ("platinum", "Платина", "15000", "19", "9", [PERK]),
        ("diamond", "Бриллиант", "20000", "21", "11", [PERK]),
    ]
    return [
        LoyaltyTier(
            code=code,
            name=name,
            min_spend=Decimal(min_spend),
            discount_percent=Decimal(discount),
            cashback_percent=Decimal(cashback),
            perks=perks,
            sort_order=index,
            is_active=True,
        )
        for index, (code, name, min_spend, discount, cashback, perks) in enumerate(seed)
    ]


def _link(
    status: SyncStatus = SyncStatus.synced,
    customer_id: str | None = "guest-1",
    updated_ago: timedelta = timedelta(minutes=1),
) -> IikoLink:
    return IikoLink(
        sync_status=status, iiko_customer_id=customer_id, card_number=CARD, updated_at=NOW - updated_ago
    )


def _state(
    total_spend: str = "7500",
    synced_ago: timedelta | None = timedelta(minutes=1),
    updated_ago: timedelta = timedelta(minutes=1),
    last_error: str | None = None,
    coffee: int | None = 3,
) -> LoyaltyState:
    return LoyaltyState(
        user_id=uuid.uuid4(),
        total_spend=Decimal(total_spend),
        bonus_balance=Decimal("120.50"),
        total_savings=Decimal("830.25"),
        coffee_collected=coffee,
        tier_code=None,
        synced_at=NOW - synced_ago if synced_ago is not None else None,
        updated_at=NOW - updated_ago,
        last_error=last_error,
    )


class TestFlatMode:
    def test_flat_shape(self, make_settings):
        out = assemble_loyalty_out(make_settings(), _link(), _state(), _tiers(), NOW)
        data = out.model_dump()
        assert set(data) == CONTRACT_KEYS
        assert data["mode"] == "flat"
        assert data["sync_status"] == "synced"
        assert data["card_number"] == CARD
        assert data["current_tier"] == {
            "code": "base",
            "name": "Скидка",
            "min_spend": 0.0,
            "discount_percent": 10.0,
            "cashback_percent": 0.0,
            "perks": [],
        }
        assert data["next_tier"] is None
        assert data["remaining_to_next"] is None
        assert data["progress"] == 1.0
        assert data["tiers"] == []
        assert data["total_spend"] == 7500.0
        assert data["bonus_balance"] == 120.5
        assert data["total_savings"] == 830.25
        assert data["coffee"] is None
        assert data["updated_at"] == NOW - timedelta(minutes=1)
        assert data["stale"] is False

    def test_flat_discount_is_configurable(self, make_settings):
        out = assemble_loyalty_out(make_settings(loyalty_flat_discount_percent=12.5), _link(), _state(), [], NOW)
        assert out.current_tier.discount_percent == 12.5

    def test_json_is_utc_iso(self, make_settings):
        out = assemble_loyalty_out(make_settings(), _link(), _state(), [], NOW)
        assert '"updated_at":"2026-09-30T11:59:00Z"' in out.model_dump_json()


class TestTiersMode:
    def test_tier_progress(self, make_settings):
        out = assemble_loyalty_out(make_settings(loyalty_tiers_enabled=True), _link(), _state(), _tiers(), NOW)
        assert out.mode == "tiers"
        assert out.current_tier.code == "silver"
        assert out.current_tier.perks == [PERK]
        assert out.current_tier.discount_percent == 15.0
        assert out.current_tier.cashback_percent == 5.0
        assert out.next_tier.code == "gold"
        assert out.remaining_to_next == 2500.0
        assert out.progress == 0.5
        assert [tier.code for tier in out.tiers] == ["bronze", "silver", "gold", "platinum", "diamond"]
        assert all(set(tier.model_dump()) == TIER_KEYS for tier in out.tiers)

    def test_tiers_are_ordered_by_min_spend(self, make_settings):
        tiers = list(reversed(_tiers()))
        out = assemble_loyalty_out(make_settings(loyalty_tiers_enabled=True), _link(), _state("0"), tiers, NOW)
        assert [tier.code for tier in out.tiers] == ["bronze", "silver", "gold", "platinum", "diamond"]
        assert out.current_tier.code == "bronze"
        assert out.progress == 0.0

    def test_top_tier(self, make_settings):
        out = assemble_loyalty_out(
            make_settings(loyalty_tiers_enabled=True), _link(), _state("25000"), _tiers(), NOW
        )
        assert out.current_tier.code == "diamond"
        assert out.next_tier is None
        assert out.remaining_to_next is None
        assert out.progress == 1.0

    def test_no_tiers_configured_falls_back_to_base(self, make_settings):
        out = assemble_loyalty_out(make_settings(loyalty_tiers_enabled=True), _link(), _state(), [], NOW)
        assert out.current_tier.code == "base"
        assert out.next_tier is None
        assert out.tiers == []
        assert out.progress == 1.0

    def test_unlinked_user_starts_at_entry_tier(self, make_settings):
        out = assemble_loyalty_out(make_settings(loyalty_tiers_enabled=True), None, None, _tiers(), NOW)
        assert out.current_tier.code == "bronze"
        assert out.next_tier.code == "silver"
        assert out.remaining_to_next == 5000.0
        assert out.total_spend == 0.0


class TestLinkStates:
    def test_no_link(self, make_settings):
        out = assemble_loyalty_out(make_settings(), None, None, [], NOW)
        assert out.sync_status is None
        assert out.card_number is None
        assert out.total_spend == 0.0
        assert out.bonus_balance == 0.0
        assert out.total_savings == 0.0
        assert out.updated_at is None
        assert out.stale is False

    @pytest.mark.parametrize("status", [SyncStatus.pending, SyncStatus.failed])
    def test_failed_relink_keeps_serving_cached_state(self, make_settings, status):
        link = _link(status)
        assert is_linked(link) is True
        out = assemble_loyalty_out(make_settings(loyalty_tiers_enabled=True), link, _state(), _tiers(), NOW)
        assert out.sync_status == status.value
        assert out.card_number == CARD
        assert out.total_spend == 7500.0
        assert out.current_tier.code == "silver"
        assert out.bonus_balance == 120.5
        assert out.total_savings == 830.25
        assert out.updated_at == NOW - timedelta(minutes=1)
        assert out.stale is True

    @pytest.mark.parametrize("status", [SyncStatus.pending, SyncStatus.failed, SyncStatus.synced])
    def test_link_without_customer_id_is_not_linked(self, make_settings, status):
        link = _link(status, customer_id=None)
        assert is_linked(link) is False
        out = assemble_loyalty_out(make_settings(), link, _state(), [], NOW)
        assert out.total_spend == 0.0
        assert out.bonus_balance == 0.0
        assert out.updated_at is None
        assert out.stale is False


class TestRelink:
    def test_missing_link(self):
        assert needs_relink(None, NOW) is True

    def test_failed_link(self):
        assert needs_relink(_link(SyncStatus.failed), NOW) is True
        assert needs_relink(_link(SyncStatus.failed, customer_id=None), NOW) is True

    def test_synced_link(self):
        assert needs_relink(_link(SyncStatus.synced, updated_ago=timedelta(days=30)), NOW) is False

    def test_recent_pending_link_is_in_progress(self):
        assert needs_relink(_link(SyncStatus.pending, customer_id=None, updated_ago=timedelta(minutes=9)), NOW) is False

    def test_abandoned_pending_link(self):
        link = _link(SyncStatus.pending, customer_id=None, updated_ago=timedelta(minutes=10))
        assert needs_relink(link, NOW) is True


class TestStaleness:
    def test_fresh(self, make_settings):
        assert assemble_loyalty_out(make_settings(), _link(), _state(), [], NOW).stale is False

    def test_older_than_ttl(self, make_settings):
        state = _state(synced_ago=timedelta(minutes=11))
        assert assemble_loyalty_out(make_settings(), _link(), state, [], NOW).stale is True

    def test_never_synced(self, make_settings):
        state = _state(synced_ago=None)
        out = assemble_loyalty_out(make_settings(), _link(), state, [], NOW)
        assert out.stale is True
        assert out.updated_at is None

    def test_last_error(self, make_settings):
        state = _state(last_error="iiko /api/1/loyalty/iiko/customer_category/add responded 429")
        assert assemble_loyalty_out(make_settings(), _link(), state, [], NOW).stale is True

    def test_linked_without_state(self, make_settings):
        assert assemble_loyalty_out(make_settings(), _link(), None, [], NOW).stale is True

    def test_predicates(self, make_settings):
        settings = make_settings()
        assert needs_refresh(_state(synced_ago=timedelta(seconds=599)), settings, NOW) is False
        assert needs_refresh(_state(synced_ago=timedelta(seconds=600)), settings, NOW) is True
        assert in_cooldown(_state(updated_ago=timedelta(seconds=59)), settings, NOW) is True
        assert in_cooldown(_state(updated_ago=timedelta(seconds=60)), settings, NOW) is False
        assert is_stale(None, settings, NOW) is True

    def test_refreshing_marks_fresh_data_stale(self, make_settings):
        out = assemble_loyalty_out(make_settings(), _link(), _state(), [], NOW, refreshing=True)
        assert out.stale is True
        assert out.total_spend == 7500.0

    def test_refreshing_unlinked_user_is_stale(self, make_settings):
        link = _link(SyncStatus.pending, customer_id=None)
        out = assemble_loyalty_out(make_settings(), link, None, [], NOW, refreshing=True)
        assert out.stale is True
        assert out.sync_status == "pending"
        assert out.total_spend == 0.0


class TestSyncDue:
    def test_no_state_yet(self, make_settings):
        assert sync_due(None, make_settings(), NOW) is True

    def test_fresh(self, make_settings):
        assert sync_due(_state(), make_settings(), NOW) is False

    def test_expired(self, make_settings):
        state = _state(synced_ago=timedelta(minutes=11), updated_ago=timedelta(minutes=11))
        assert sync_due(state, make_settings(), NOW) is True

    def test_expired_but_sync_just_claimed(self, make_settings):
        state = _state(synced_ago=timedelta(minutes=11), updated_ago=timedelta(seconds=5))
        assert sync_due(state, make_settings(), NOW) is False

    def test_never_synced(self, make_settings):
        state = _state(synced_ago=None, updated_ago=timedelta(minutes=2))
        assert sync_due(state, make_settings(), NOW) is True


class TestCoffee:
    def test_configured(self, make_settings):
        settings = make_settings(coffee_stamp_wallet_id="coffee-wallet", coffee_stamp_goal=7)
        out = assemble_loyalty_out(settings, _link(), _state(coffee=3), [], NOW)
        assert out.coffee.model_dump() == {"collected": 3, "goal": 7}

    def test_configured_but_not_counted_yet(self, make_settings):
        settings = make_settings(coffee_stamp_wallet_id="coffee-wallet")
        out = assemble_loyalty_out(settings, _link(), _state(coffee=None), [], NOW)
        assert out.coffee.model_dump() == {"collected": 0, "goal": 7}

    def test_configured_without_link(self, make_settings):
        settings = make_settings(coffee_stamp_wallet_id="coffee-wallet")
        assert assemble_loyalty_out(settings, None, None, [], NOW).coffee.collected == 0

    def test_not_configured(self, make_settings):
        assert assemble_loyalty_out(make_settings(), _link(), _state(coffee=5), [], NOW).coffee is None
