from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TypeVar

from sqlalchemy import ColumnElement, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..loyalty.rules import tier_spend_ranges
from ..models import Bakery, IikoLink, LoyaltyState, LoyaltyTier, Novelty, Promotion, SyncStatus, User

GUESTS_PAGE_SIZE = 50

Orderable = TypeVar("Orderable", Novelty, Promotion, Bakery)

_LINK_STATUS_LABELS = {
    None: "Не привязан",
    SyncStatus.pending: "Привязывается",
    SyncStatus.synced: "Привязан",
    SyncStatus.failed: "Ошибка привязки",
}


async def ordered_items(db: AsyncSession, model: type[Orderable]) -> list[Orderable]:
    return list(await db.scalars(select(model).order_by(model.sort_order, model.created_at, model.id)))


async def next_sort_order(db: AsyncSession, model: type[Orderable]) -> int:
    current = await db.scalar(select(func.max(model.sort_order)))
    return 0 if current is None else current + 1


def reorder(items: Sequence[Orderable], item_id: uuid.UUID, direction: int) -> bool:
    ordered = list(items)
    index = next((position for position, item in enumerate(ordered) if item.id == item_id), None)
    if index is None:
        return False
    target = index + direction
    if 0 <= target < len(ordered):
        ordered[index], ordered[target] = ordered[target], ordered[index]
    for position, item in enumerate(ordered):
        if item.sort_order != position:
            item.sort_order = position
    return True


async def move_item(db: AsyncSession, model: type[Orderable], item_id: uuid.UUID, direction: int) -> bool:
    moved = reorder(await ordered_items(db, model), item_id, direction)
    await db.commit()
    return moved


async def load_tiers(db: AsyncSession) -> list[LoyaltyTier]:
    return list(await db.scalars(select(LoyaltyTier).order_by(LoyaltyTier.min_spend, LoyaltyTier.sort_order)))


async def count_audience(
    db: AsyncSession,
    audience: str,
    phones: Sequence[str],
    tiers: Sequence[str],
    *,
    tiers_enabled: bool,
) -> int:
    if audience == "all":
        return await db.scalar(select(func.count()).select_from(User)) or 0
    if audience == "phones":
        if not phones:
            return 0
        return await db.scalar(select(func.count()).select_from(User).where(User.phone.in_(list(phones)))) or 0
    if audience == "tiers" and tiers_enabled and tiers:
        active = [tier for tier in await load_tiers(db) if tier.is_active]
        ranges = tier_spend_ranges(active, tiers)
        if not ranges:
            return 0
        spend = case(
            (IikoLink.iiko_customer_id.is_not(None), func.coalesce(LoyaltyState.total_spend, 0)), else_=0
        )
        query = (
            select(func.count())
            .select_from(User)
            .outerjoin(IikoLink, IikoLink.user_id == User.id)
            .outerjoin(LoyaltyState, LoyaltyState.user_id == User.id)
            .where(or_(*(_in_range(spend, low, high) for low, high in ranges)))
        )
        return await db.scalar(query) or 0
    return 0


def _in_range(spend: ColumnElement[Decimal], low: Decimal, high: Decimal | None) -> ColumnElement[bool]:
    return spend >= low if high is None else and_(spend >= low, spend < high)


def mask_phone(phone: str) -> str:
    if len(phone) <= 4:
        return "•" * len(phone)
    prefix = phone[:2] if phone.startswith("+") and len(phone) > 6 else ""
    return prefix + "•" * (len(phone) - len(prefix) - 4) + phone[-4:]


def mask_card(card: str | None) -> str:
    if not card:
        return "—"
    return f"•••• {card[-4:]}" if len(card) > 4 else "••••"


def search_digits(raw: str | None) -> str:
    return "".join(character for character in (raw or "") if character.isdigit())[:10]


@dataclass(frozen=True)
class GuestRow:
    phone: str
    name: str
    link_status: str
    card: str
    tier: str
    total_spend: Decimal
    bonus_balance: Decimal
    created_at: datetime


async def list_guests(db: AsyncSession, digits: str, page: int) -> tuple[list[GuestRow], int]:
    query = (
        select(User, IikoLink, LoyaltyState)
        .outerjoin(IikoLink, IikoLink.user_id == User.id)
        .outerjoin(LoyaltyState, LoyaltyState.user_id == User.id)
    )
    count = select(func.count()).select_from(User)
    if digits:
        query = query.where(User.phone.endswith(digits))
        count = count.where(User.phone.endswith(digits))
    total = await db.scalar(count) or 0
    tier_names = {tier.code: tier.name for tier in await db.scalars(select(LoyaltyTier))}
    result = await db.execute(
        query.order_by(User.created_at.desc(), User.id)
        .limit(GUESTS_PAGE_SIZE)
        .offset((page - 1) * GUESTS_PAGE_SIZE)
    )
    rows = [
        GuestRow(
            phone=mask_phone(user.phone),
            name=" ".join(part for part in (user.name, user.surname) if part).strip() or "—",
            link_status=_LINK_STATUS_LABELS[link.sync_status if link else None],
            card=mask_card(link.card_number if link else None),
            tier=tier_names.get(state.tier_code, state.tier_code) if state and state.tier_code else "—",
            total_spend=state.total_spend if state else Decimal(0),
            bonus_balance=state.bonus_balance if state else Decimal(0),
            created_at=user.created_at,
        )
        for user, link, state in result.all()
    ]
    return rows, total
