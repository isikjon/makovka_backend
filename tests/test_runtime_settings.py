import pytest

from app import runtime_settings
from app.runtime_settings import (
    CACHE_TTL_SECONDS,
    OVERRIDABLE_KEYS,
    OverrideCache,
    SettingValueError,
    apply_overrides,
    get_effective_settings,
    invalidate_settings_cache,
    load_overrides,
    validate_override,
    validate_setting,
)

WALLET = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


class TestValidation:
    @pytest.mark.parametrize(
        ("key", "raw", "expected"),
        [
            ("loyalty_tiers_enabled", True, True),
            ("loyalty_tiers_enabled", "true", True),
            ("loyalty_tier_sync_enabled", "false", False),
            ("referral_rewards_enabled", "on", True),
            ("loyalty_flat_discount_percent", "12,5", 12.5),
            ("loyalty_flat_discount_percent", 0, 0.0),
            ("loyalty_flat_discount_percent", 100, 100.0),
            ("coffee_stamp_goal", "7", 7),
            ("coffee_stamp_goal", 50, 50),
            ("coffee_stamp_goal", 1.0, 1),
            ("referral_bonus_amount", "500", 500.0),
            ("referral_bonus_amount", 100000, 100000.0),
            ("coffee_stamp_wallet_id", "", ""),
            ("coffee_stamp_wallet_id", f" {WALLET} ", WALLET),
            ("coffee_stamp_wallet_id", "3FA85F6457174562B3FC2C963F66AFA6", WALLET),
            ("coffee_stamp_wallet_id", "3FA85F64-5717-4562-B3FC-2C963F66AFA6", WALLET),
        ],
    )
    def test_accepts(self, key, raw, expected):
        value = validate_setting(key, raw)
        assert value == expected
        assert type(value) is type(expected)

    @pytest.mark.parametrize(
        ("key", "raw"),
        [
            ("loyalty_tiers_enabled", "maybe"),
            ("loyalty_tiers_enabled", 1),
            ("loyalty_flat_discount_percent", "-1"),
            ("loyalty_flat_discount_percent", "100.01"),
            ("loyalty_flat_discount_percent", "nan"),
            ("loyalty_flat_discount_percent", "inf"),
            ("loyalty_flat_discount_percent", True),
            ("loyalty_flat_discount_percent", ""),
            ("coffee_stamp_goal", "0"),
            ("coffee_stamp_goal", "51"),
            ("coffee_stamp_goal", "7.5"),
            ("referral_bonus_amount", "100000.01"),
            ("referral_bonus_amount", "abc"),
            ("coffee_stamp_wallet_id", "not-a-wallet"),
            ("coffee_stamp_wallet_id", f"{WALLET}; drop"),
            ("coffee_stamp_wallet_id", 5),
            ("coffee_stamp_wallet_id", "{" + WALLET + "}"),
            ("coffee_stamp_wallet_id", WALLET[:-1]),
        ],
    )
    def test_rejects(self, key, raw):
        with pytest.raises(SettingValueError):
            validate_setting(key, raw)

    def test_coffee_wallet_must_differ_from_bonus_wallet(self, make_settings):
        base = make_settings(loyalty_bonus_wallet_id=WALLET.upper())
        with pytest.raises(SettingValueError):
            validate_override(base, "coffee_stamp_wallet_id", WALLET.replace("-", ""))
        other = "11111111-2222-3333-4444-555555555555"
        assert validate_override(base, "coffee_stamp_wallet_id", other) == other
        assert validate_override(base, "coffee_stamp_wallet_id", "") == ""
        assert validate_override(make_settings(), "coffee_stamp_wallet_id", WALLET) == WALLET

    @pytest.mark.parametrize(
        "key",
        ["iiko_env", "iiko_mutations_enabled", "iiko_test_guest_phone", "jwt_secret", "loyalty_bonus_wallet_id"],
    )
    def test_env_only_keys_are_never_overridable(self, key):
        assert key not in OVERRIDABLE_KEYS
        with pytest.raises(SettingValueError):
            validate_setting(key, "prod")

    def test_overridable_keys(self):
        assert OVERRIDABLE_KEYS == {
            "loyalty_tiers_enabled",
            "loyalty_tier_sync_enabled",
            "loyalty_flat_discount_percent",
            "coffee_stamp_wallet_id",
            "coffee_stamp_goal",
            "referral_rewards_enabled",
            "referral_bonus_amount",
        }


class TestMerge:
    def test_overrides_win_over_env(self, make_settings):
        base = make_settings(loyalty_tiers_enabled=False, coffee_stamp_goal=7, referral_bonus_amount=500)
        merged = apply_overrides(
            base, {"loyalty_tiers_enabled": True, "coffee_stamp_goal": 9, "referral_bonus_amount": 250}
        )
        assert merged.loyalty_tiers_enabled is True
        assert merged.coffee_stamp_goal == 9
        assert merged.referral_bonus_amount == 250.0
        assert base.loyalty_tiers_enabled is False
        assert base.coffee_stamp_goal == 7

    def test_env_only_and_invalid_rows_are_ignored(self, make_settings):
        base = make_settings(iiko_env="staging", iiko_mutations_enabled=True, coffee_stamp_goal=7)
        merged = apply_overrides(
            base,
            {
                "iiko_env": "prod",
                "iiko_mutations_enabled": False,
                "iiko_test_guest_phone": "+79991112233",
                "coffee_stamp_goal": 500,
                "unknown": 1,
            },
        )
        assert merged.iiko_env == "staging"
        assert merged.iiko_mutations_enabled is True
        assert merged.iiko_test_guest_phone == base.iiko_test_guest_phone
        assert merged.coffee_stamp_goal == 7

    def test_no_overrides_returns_env_settings(self, make_settings):
        base = make_settings()
        assert apply_overrides(base, {}) is base


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def cache(monkeypatch):
    clock = Clock()
    rows: dict = {}
    fetches: list[int] = []

    async def fetch():
        fetches.append(1)
        return dict(rows)

    monkeypatch.setattr(runtime_settings, "_cache", OverrideCache(clock=clock))
    monkeypatch.setattr(runtime_settings, "fetch_overrides", fetch)
    return clock, rows, fetches


class TestCache:
    async def test_cached_for_ttl(self, cache):
        clock, rows, fetches = cache
        rows["coffee_stamp_goal"] = 5
        assert await load_overrides() == {"coffee_stamp_goal": 5}
        rows["coffee_stamp_goal"] = 6
        clock.now += CACHE_TTL_SECONDS - 0.1
        assert await load_overrides() == {"coffee_stamp_goal": 5}
        assert len(fetches) == 1
        clock.now += 0.2
        assert await load_overrides() == {"coffee_stamp_goal": 6}
        assert len(fetches) == 2

    async def test_admin_save_invalidates_immediately(self, cache):
        clock, rows, fetches = cache
        assert await load_overrides() == {}
        rows["loyalty_tiers_enabled"] = True
        invalidate_settings_cache()
        assert await load_overrides() == {"loyalty_tiers_enabled": True}
        assert len(fetches) == 2

    async def test_fetch_racing_an_invalidation_is_not_cached(self, cache, monkeypatch):
        clock, rows, fetches = cache

        async def stale_fetch():
            fetches.append(1)
            invalidate_settings_cache()
            return {"coffee_stamp_goal": 3}

        monkeypatch.setattr(runtime_settings, "fetch_overrides", stale_fetch)
        assert await load_overrides() == {"coffee_stamp_goal": 3}
        assert runtime_settings._cache.values is None

    async def test_effective_settings_merge_env_and_db(self, cache, monkeypatch, make_settings):
        clock, rows, fetches = cache
        monkeypatch.setattr(runtime_settings, "get_settings", lambda: make_settings(loyalty_flat_discount_percent=10))
        rows["loyalty_flat_discount_percent"] = 7.5
        effective = await get_effective_settings()
        assert effective.loyalty_flat_discount_percent == 7.5
        assert effective.iiko_organization_id == "org-1"


class FakeSession:
    def __init__(self):
        self.statements: list = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)

    async def commit(self):
        self.commits += 1


class TestSave:
    async def test_upserts_removes_and_invalidates(self, cache):
        clock, rows, fetches = cache
        await load_overrides()
        db = FakeSession()
        await runtime_settings.save_overrides(
            db, {"coffee_stamp_goal": "9"}, ["loyalty_tiers_enabled", "iiko_env"], "admin"
        )
        assert db.commits == 1
        assert len(db.statements) == 2
        insert, delete = db.statements
        assert insert.compile().params["value"] == 9
        assert insert.compile().params["updated_by"] == "admin"
        assert "iiko_env" not in str(delete.compile(compile_kwargs={"literal_binds": True}))
        assert runtime_settings._cache.values is None

    async def test_invalid_value_is_never_written(self, cache):
        db = FakeSession()
        with pytest.raises(SettingValueError):
            await runtime_settings.save_overrides(db, {"coffee_stamp_goal": "0"}, [], "admin")
        with pytest.raises(SettingValueError):
            await runtime_settings.save_overrides(db, {"iiko_env": "prod"}, [], "admin")
        assert db.statements == []
        assert db.commits == 0
