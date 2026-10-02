import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.content import assemble_content_out, in_window, matches_audience, promotion_out, visible_promotions
from app.db import get_db
from app.deps import get_current_user
from app.loyalty import service as loyalty_service
from app.main import app
from app.models import Bakery, IikoLink, LoyaltyState, LoyaltyTier, Novelty, Promotion, SyncStatus, User
from app.routers import content as content_routes
from app.runtime_settings import get_effective_settings

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
PHONE = "+79990000001"
OTHER_PHONE = "+79990000002"
CREATED = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _promotion(title: str = "Акция", **fields) -> Promotion:
    values = {
        "id": uuid.uuid4(),
        "kind": "generic",
        "title": title,
        "subtitle": None,
        "description": None,
        "badge": None,
        "price": None,
        "old_price": None,
        "time_text": None,
        "image": None,
        "starts_at": None,
        "ends_at": None,
        "show_on_home": False,
        "sort_order": 0,
        "is_active": True,
        "audience": "all",
        "audience_phones": [],
        "audience_tiers": [],
        "created_at": CREATED,
    }
    values.update(fields)
    return Promotion(**values)


def _titles(promotions) -> list[str]:
    return [promotion.title for promotion in promotions]


class TestWindow:
    def test_open_ended(self):
        assert in_window(None, None, NOW) is True

    def test_not_started_yet(self):
        assert in_window(NOW + timedelta(seconds=1), None, NOW) is False
        assert in_window(NOW, None, NOW) is True

    def test_ended(self):
        assert in_window(None, NOW, NOW) is False
        assert in_window(None, NOW + timedelta(seconds=1), NOW) is True

    def test_inside(self):
        assert in_window(NOW - timedelta(days=1), NOW + timedelta(days=1), NOW) is True


class TestAudience:
    def test_everyone(self):
        assert matches_audience(_promotion(), PHONE, None, tiers_enabled=False) is True

    def test_phones(self):
        promotion = _promotion(audience="phones", audience_phones=[PHONE])
        assert matches_audience(promotion, PHONE, None, tiers_enabled=False) is True
        assert matches_audience(promotion, OTHER_PHONE, None, tiers_enabled=False) is False

    def test_tiers_only_in_tiers_mode(self):
        promotion = _promotion(audience="tiers", audience_tiers=["gold", "platinum"])
        assert matches_audience(promotion, PHONE, "gold", tiers_enabled=True) is True
        assert matches_audience(promotion, PHONE, "gold", tiers_enabled=False) is False
        assert matches_audience(promotion, PHONE, "silver", tiers_enabled=True) is False
        assert matches_audience(promotion, PHONE, None, tiers_enabled=True) is False

    def test_unknown_audience_is_hidden(self):
        assert matches_audience(_promotion(audience="vip"), PHONE, "gold", tiers_enabled=True) is False


class TestVisiblePromotions:
    def test_filters_inactive_window_and_audience_keeping_order(self):
        promotions = [
            _promotion("Всем"),
            _promotion("Скрыта", is_active=False),
            _promotion("Ещё не началась", starts_at=NOW + timedelta(hours=1)),
            _promotion("Закончилась", ends_at=NOW - timedelta(minutes=1)),
            _promotion("Идёт", starts_at=NOW - timedelta(days=1), ends_at=NOW + timedelta(days=1)),
            _promotion("Мне", audience="phones", audience_phones=[PHONE]),
            _promotion("Не мне", audience="phones", audience_phones=[OTHER_PHONE]),
            _promotion("Золото", audience="tiers", audience_tiers=["gold"]),
            _promotion("Моя закончилась", audience="phones", audience_phones=[PHONE], ends_at=NOW),
        ]
        shown = visible_promotions(promotions, now=NOW, phone=PHONE, tier_code="gold", tiers_enabled=True)
        assert _titles(shown) == ["Всем", "Идёт", "Мне", "Золото"]

    def test_tier_offers_disappear_when_tiers_mode_is_off(self):
        promotions = [_promotion("Всем"), _promotion("Золото", audience="tiers", audience_tiers=["gold"])]
        shown = visible_promotions(promotions, now=NOW, phone=PHONE, tier_code="gold", tiers_enabled=False)
        assert _titles(shown) == ["Всем"]


def _novelty(title: str, image: str | None, *, active: bool = True) -> Novelty:
    return Novelty(id=uuid.uuid4(), title=title, image=image, sort_order=0, is_active=active, created_at=CREATED)


def _bakery(name: str, *, active: bool = True) -> Bakery:
    return Bakery(
        id=uuid.uuid4(),
        name=name,
        address="Сходненская улица, 35 к1",
        metro="Метро Сходненская",
        hours_text="Открыто с 07:00 до 23:00",
        latitude=55.8583,
        longitude=37.4553,
        sort_order=0,
        is_active=active,
        created_at=CREATED,
    )


class TestAssemble:
    def test_contract_shape(self, make_settings):
        combo = _promotion(
            "Комбо: Круассан + Американо",
            kind="combo",
            subtitle="Идеальное утреннее сочетание",
            price=Decimal("299.00"),
            old_price=Decimal("420.00"),
            show_on_home=True,
            image="0123456789abcdef0123456789abcdef.webp",
        )
        personal = _promotion(
            "Для вас",
            audience="phones",
            audience_phones=[PHONE],
            ends_at=datetime(2026, 10, 1, 3, 0, tzinfo=timezone(timedelta(hours=3))),
        )
        out = assemble_content_out(
            make_settings(),
            [_novelty("Пицца цыпленок барбекю", "seed_pizza_bbq.png"), _novelty("Скрыта", None, active=False)],
            [combo, personal],
            [_bakery("Сходненская"), _bakery("Закрыта", active=False)],
            now=NOW,
            phone=PHONE,
            tier_code=None,
        )
        data = out.model_dump(mode="json")
        assert set(data) == {"novelties", "promotions", "bakeries"}
        assert data["novelties"] == [
            {
                "id": data["novelties"][0]["id"],
                "title": "Пицца цыпленок барбекю",
                "image_url": "https://app.top1makovka.ru/api/media/seed_pizza_bbq.png",
            }
        ]
        assert data["promotions"][0] == {
            "id": str(combo.id),
            "kind": "combo",
            "title": "Комбо: Круассан + Американо",
            "subtitle": "Идеальное утреннее сочетание",
            "description": None,
            "badge": None,
            "price": 299.0,
            "old_price": 420.0,
            "time_text": None,
            "image_url": "https://app.top1makovka.ru/api/media/0123456789abcdef0123456789abcdef.webp",
            "show_on_home": True,
            "personal": False,
            "ends_at": None,
        }
        assert data["promotions"][1]["personal"] is True
        assert data["promotions"][1]["ends_at"] == "2026-10-01T00:00:00Z"
        assert data["bakeries"] == [
            {
                "id": data["bakeries"][0]["id"],
                "name": "Сходненская",
                "address": "Сходненская улица, 35 к1",
                "metro": "Метро Сходненская",
                "hours_text": "Открыто с 07:00 до 23:00",
                "latitude": 55.8583,
                "longitude": 37.4553,
            }
        ]

    def test_uses_effective_tiers_mode(self, make_settings):
        promotions = [_promotion("Золото", audience="tiers", audience_tiers=["gold"])]
        off = assemble_content_out(make_settings(), [], promotions, [], now=NOW, phone=PHONE, tier_code="gold")
        on = assemble_content_out(
            make_settings(loyalty_tiers_enabled=True), [], promotions, [], now=NOW, phone=PHONE, tier_code="gold"
        )
        assert off.promotions == []
        assert [promotion.title for promotion in on.promotions] == ["Золото"]


class ContentEnv:
    def __init__(self, make_settings):
        self.make_settings = make_settings
        self.tier_lookups: list[uuid.UUID] = []
        self.promotions = [
            _promotion("Всем"),
            _promotion("Мне", audience="phones", audience_phones=[PHONE]),
            _promotion("Золото", audience="tiers", audience_tiers=["gold"]),
        ]

    def use_settings(self, **overrides) -> None:
        app.dependency_overrides[get_effective_settings] = lambda: self.make_settings(**overrides)


@pytest.fixture
def content_env(monkeypatch, make_settings):
    env = ContentEnv(make_settings)
    user = User(id=uuid.uuid4(), phone=PHONE, name="Анна")

    async def load_active_content(db):
        novelties = [_novelty("Круассан с шоколадом", "seed_croissant_chocolate.png")]
        return novelties, env.promotions, [_bakery("Тушинская")]

    async def load_current_tier_code(db, settings, current_user):
        env.tier_lookups.append(current_user.id)
        return "gold"

    async def fake_db():
        yield object()

    monkeypatch.setattr(content_routes, "load_active_content", load_active_content)
    monkeypatch.setattr(content_routes, "load_current_tier_code", load_current_tier_code)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = fake_db
    env.use_settings()
    yield env
    app.dependency_overrides.clear()


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


class TestContentRoute:
    async def test_requires_auth(self, client):
        assert (await client.get("/content")).status_code == 401

    async def test_flat_mode_never_looks_up_tier(self, content_env, client):
        body = (await client.get("/content")).json()
        assert [promotion["title"] for promotion in body["promotions"]] == ["Всем", "Мне"]
        assert [promotion["personal"] for promotion in body["promotions"]] == [False, True]
        assert body["novelties"][0]["image_url"].endswith("/api/media/seed_croissant_chocolate.png")
        assert body["bakeries"][0]["name"] == "Тушинская"
        assert content_env.tier_lookups == []

    async def test_tiers_mode_matches_current_tier(self, content_env, client):
        content_env.use_settings(loyalty_tiers_enabled=True)
        body = (await client.get("/content")).json()
        assert [promotion["title"] for promotion in body["promotions"]] == ["Всем", "Мне", "Золото"]
        assert len(content_env.tier_lookups) == 1


def _tier(code: str, min_spend: str, *, active: bool = True) -> LoyaltyTier:
    return LoyaltyTier(
        code=code,
        name=code,
        min_spend=Decimal(min_spend),
        discount_percent=Decimal("10"),
        cashback_percent=Decimal("0"),
        perks=[],
        iiko_category_id=None,
        sort_order=0,
        is_active=active,
    )


TIERS = [_tier("bronze", "0"), _tier("silver", "5000"), _tier("gold", "10000", active=False)]


class TierLookup:
    def __init__(self, monkeypatch, link: IikoLink | None, state: LoyaltyState | None):
        self.calls: list[str] = []

        async def load_tiers(db, *, active_only):
            self.calls.append(f"tiers:{active_only}")
            return [tier for tier in TIERS if tier.is_active or not active_only]

        async def load_link(db, user_id):
            self.calls.append("link")
            return link

        async def load_state(db, user_id):
            self.calls.append("state")
            return state

        monkeypatch.setattr(loyalty_service, "_load_tiers", load_tiers)
        monkeypatch.setattr(loyalty_service, "load_link", load_link)
        monkeypatch.setattr(loyalty_service, "load_state", load_state)


def _state(total_spend: str) -> LoyaltyState:
    return LoyaltyState(
        user_id=uuid.uuid4(),
        total_spend=Decimal(total_spend),
        bonus_balance=Decimal(0),
        total_savings=Decimal(0),
        tier_code=None,
        synced_at=NOW,
        updated_at=NOW,
        last_error=None,
    )


def _link(customer_id: str | None = "guest-1") -> IikoLink:
    return IikoLink(sync_status=SyncStatus.synced, iiko_customer_id=customer_id, card_number="1")


class TestCurrentTierForContent:
    @pytest.mark.parametrize(
        ("link", "state", "expected"),
        [
            (None, None, "bronze"),
            (_link(customer_id=None), _state("7000"), "bronze"),
            (_link(), _state("7000"), "silver"),
            (_link(), _state("20000"), "silver"),
        ],
    )
    async def test_matches_the_tier_shown_by_loyalty(self, monkeypatch, make_settings, link, state, expected):
        settings = make_settings(loyalty_tiers_enabled=True)
        TierLookup(monkeypatch, link, state)
        user = User(id=uuid.uuid4(), phone=PHONE, name="Анна")
        code = await loyalty_service.load_current_tier_code(object(), settings, user)
        shown = loyalty_service.assemble_loyalty_out(
            settings, link, state, [tier for tier in TIERS if tier.is_active], NOW
        )
        assert code == expected == shown.current_tier.code

    async def test_stale_stored_tier_code_is_ignored(self, monkeypatch, make_settings):
        state = _state("100")
        state.tier_code = "gold"
        TierLookup(monkeypatch, _link(), state)
        user = User(id=uuid.uuid4(), phone=PHONE, name="Анна")
        assert await loyalty_service.load_current_tier_code(object(), make_settings(), user) == "bronze"

    async def test_review_demo_never_loads_guest_data(self, monkeypatch, make_settings):
        settings = make_settings(review_bypass_phone=PHONE, review_demo_card_number="7777000011112222")
        lookup = TierLookup(monkeypatch, _link(), _state("20000"))
        user = User(id=uuid.uuid4(), phone=PHONE, name="Ревьюер")
        assert await loyalty_service.load_current_tier_code(object(), settings, user) == "bronze"
        assert lookup.calls == ["tiers:True"]


class TestCoffeePromotionText:
    def test_reward_number_follows_the_stamp_goal(self, make_settings):
        promotion = _promotion(
            "Скидка 99% на каждый {n} кофе", kind="coffee_stamps", description="каждый {n}-й кофе", subtitle=None
        )
        out = promotion_out(make_settings(coffee_stamp_goal=9), promotion)
        assert (out.title, out.description, out.subtitle) == ("Скидка 99% на каждый 10 кофе", "каждый 10-й кофе", None)

    def test_other_kinds_are_left_alone(self, make_settings):
        promotion = _promotion("Код {n}", kind="generic")
        assert promotion_out(make_settings(), promotion).title == "Код {n}"


def _seed_migration():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "9d862365fd25_admin_content_referrals_settings.py"
    spec = importlib.util.spec_from_file_location("seed_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSeededPromotions:
    def test_home_order_matches_previous_app(self):
        kinds = [promotion["kind"] for promotion in _seed_migration()._SEED_PROMOTIONS]
        assert kinds == ["referral", "coffee_stamps", "happy_hours", "combo"]

    def test_coffee_text_follows_the_goal_like_before(self, make_settings):
        seed = next(item for item in _seed_migration()._SEED_PROMOTIONS if item["kind"] == "coffee_stamps")
        assert "8" not in seed["title"] + seed["description"]
        promotion = _promotion(seed["title"], kind="coffee_stamps", description=seed["description"])
        default = promotion_out(make_settings(), promotion)
        assert default.title == "Скидка 99% на каждый 8 кофе"
        assert default.description == "Копите чашки в приложении:\nкаждый 8-й кофе — за 1% стоимости!"
        assert promotion_out(make_settings(coffee_stamp_goal=5), promotion).title == "Скидка 99% на каждый 6 кофе"
