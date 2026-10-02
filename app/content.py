from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .media import media_url
from .models import Bakery, Novelty, Promotion
from .schemas import BakeryOut, ContentOut, NoveltyOut, PromotionOut

COFFEE_REWARD_PLACEHOLDER = "{n}"


def in_window(starts_at: datetime | None, ends_at: datetime | None, now: datetime) -> bool:
    if starts_at is not None and starts_at > now:
        return False
    return ends_at is None or ends_at > now


def matches_audience(
    promotion: Promotion, phone: str, tier_code: str | None, *, tiers_enabled: bool
) -> bool:
    if promotion.audience == "all":
        return True
    if promotion.audience == "phones":
        return phone in (promotion.audience_phones or [])
    if promotion.audience == "tiers":
        return tiers_enabled and tier_code is not None and tier_code in (promotion.audience_tiers or [])
    return False


def visible_promotions(
    promotions: Iterable[Promotion],
    *,
    now: datetime,
    phone: str,
    tier_code: str | None,
    tiers_enabled: bool,
) -> list[Promotion]:
    return [
        promotion
        for promotion in promotions
        if promotion.is_active
        and in_window(promotion.starts_at, promotion.ends_at, now)
        and matches_audience(promotion, phone, tier_code, tiers_enabled=tiers_enabled)
    ]


def novelty_out(settings: Settings, novelty: Novelty) -> NoveltyOut:
    return NoveltyOut(id=str(novelty.id), title=novelty.title, image_url=media_url(settings, novelty.image))


def promotion_text(settings: Settings, promotion: Promotion, text: str) -> str:
    if promotion.kind != "coffee_stamps":
        return text
    return text.replace(COFFEE_REWARD_PLACEHOLDER, str(settings.coffee_stamp_goal + 1))


def _optional_text(settings: Settings, promotion: Promotion, text: str | None) -> str | None:
    return promotion_text(settings, promotion, text) if text is not None else None


def promotion_out(settings: Settings, promotion: Promotion) -> PromotionOut:
    return PromotionOut(
        id=str(promotion.id),
        kind=promotion.kind,
        title=promotion_text(settings, promotion, promotion.title),
        subtitle=_optional_text(settings, promotion, promotion.subtitle),
        description=_optional_text(settings, promotion, promotion.description),
        badge=promotion.badge,
        price=float(promotion.price) if promotion.price is not None else None,
        old_price=float(promotion.old_price) if promotion.old_price is not None else None,
        time_text=promotion.time_text,
        image_url=media_url(settings, promotion.image),
        show_on_home=promotion.show_on_home,
        personal=promotion.audience != "all",
        ends_at=promotion.ends_at.astimezone(timezone.utc) if promotion.ends_at else None,
    )


def bakery_out(bakery: Bakery) -> BakeryOut:
    return BakeryOut(
        id=str(bakery.id),
        name=bakery.name,
        address=bakery.address,
        metro=bakery.metro,
        hours_text=bakery.hours_text,
        latitude=bakery.latitude,
        longitude=bakery.longitude,
    )


def assemble_content_out(
    settings: Settings,
    novelties: Sequence[Novelty],
    promotions: Sequence[Promotion],
    bakeries: Sequence[Bakery],
    *,
    now: datetime,
    phone: str,
    tier_code: str | None,
) -> ContentOut:
    shown = visible_promotions(
        promotions, now=now, phone=phone, tier_code=tier_code, tiers_enabled=settings.loyalty_tiers_enabled
    )
    return ContentOut(
        novelties=[novelty_out(settings, novelty) for novelty in novelties if novelty.is_active],
        promotions=[promotion_out(settings, promotion) for promotion in shown],
        bakeries=[bakery_out(bakery) for bakery in bakeries if bakery.is_active],
    )


async def load_active_content(
    db: AsyncSession,
) -> tuple[list[Novelty], list[Promotion], list[Bakery]]:
    novelties = await db.scalars(
        select(Novelty).where(Novelty.is_active.is_(True)).order_by(Novelty.sort_order, Novelty.created_at)
    )
    promotions = await db.scalars(
        select(Promotion)
        .where(Promotion.is_active.is_(True))
        .order_by(Promotion.sort_order, Promotion.created_at)
    )
    bakeries = await db.scalars(
        select(Bakery).where(Bakery.is_active.is_(True)).order_by(Bakery.sort_order, Bakery.created_at)
    )
    return list(novelties), list(promotions), list(bakeries)
