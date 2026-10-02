from __future__ import annotations

import asyncio
import uuid
import weakref
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings, is_review_demo
from ..db import async_session_factory
from ..iiko.client import IikoClientError, IikoCloudClient
from ..iiko.sync import sync_user_with_iiko
from ..models import IikoLink, LoyaltyState, LoyaltyTier, Purchase, SyncStatus, User
from ..referral.service import process_referral_reward
from ..runtime_settings import get_effective_settings
from ..schemas import CoffeeOut, LoyaltyOut, PurchaseHistoryOut, PurchaseOut, TierOut
from .rules import (
    build_purchases,
    count_coffee_stamps,
    extract_category_ids,
    orphan_order_ids,
    pick_wallet_balance,
    plan_category_changes,
    resolve_bonus_wallet_id,
    resolve_tier,
)

_PURCHASE_FIELDS = (
    "order_number",
    "ordered_at",
    "order_sum",
    "paid_sum",
    "discount_sum",
    "bonus_accrued",
    "bonus_spent",
    "terminal_group_id",
    "bakery",
)
_UPSERT_CHUNK_SIZE = 500
_ERROR_MAX_LENGTH = 1000
_LINK_PENDING_TIMEOUT = timedelta(minutes=10)

_user_locks: weakref.WeakValueDictionary[uuid.UUID, asyncio.Lock] = weakref.WeakValueDictionary()


def _user_lock(user_id: uuid.UUID) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


def is_linked(link: IikoLink | None) -> bool:
    return link is not None and bool(link.iiko_customer_id)


def tier_sync_enabled(settings: Settings) -> bool:
    return settings.loyalty_tiers_enabled and settings.loyalty_tier_sync_enabled


def effective_spend(link: IikoLink | None, state: LoyaltyState | None) -> Decimal:
    return state.total_spend if state is not None and is_linked(link) else Decimal(0)


def needs_relink(link: IikoLink | None, now: datetime) -> bool:
    if link is None or link.sync_status == SyncStatus.failed:
        return True
    return link.sync_status == SyncStatus.pending and now - link.updated_at >= _LINK_PENDING_TIMEOUT


def needs_refresh(state: LoyaltyState, settings: Settings, now: datetime) -> bool:
    if state.synced_at is None:
        return True
    return now - state.synced_at >= timedelta(seconds=settings.loyalty_cache_ttl_seconds)


def is_stale(state: LoyaltyState | None, settings: Settings, now: datetime) -> bool:
    return state is None or state.last_error is not None or needs_refresh(state, settings, now)


def in_cooldown(state: LoyaltyState, settings: Settings, now: datetime) -> bool:
    return now - state.updated_at < timedelta(seconds=settings.loyalty_refresh_cooldown_seconds)


def sync_due(state: LoyaltyState | None, settings: Settings, now: datetime) -> bool:
    return state is None or (needs_refresh(state, settings, now) and not in_cooldown(state, settings, now))


async def load_link(db: AsyncSession, user_id: uuid.UUID) -> IikoLink | None:
    result = await db.execute(
        select(IikoLink).where(IikoLink.user_id == user_id).execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def load_state(db: AsyncSession, user_id: uuid.UUID) -> LoyaltyState | None:
    return await db.get(LoyaltyState, user_id, populate_existing=True)


async def _load_tiers(db: AsyncSession, *, active_only: bool) -> list[LoyaltyTier]:
    query = select(LoyaltyTier).order_by(LoyaltyTier.min_spend, LoyaltyTier.sort_order)
    if active_only:
        query = query.where(LoyaltyTier.is_active.is_(True))
    return list((await db.scalars(query)).all())


async def claim_sync(
    db: AsyncSession, user_id: uuid.UUID, settings: Settings, now: datetime, *, force: bool
) -> bool:
    claimable = LoyaltyState.updated_at <= now - timedelta(seconds=settings.loyalty_refresh_cooldown_seconds)
    if not force:
        claimable = and_(
            claimable,
            or_(
                LoyaltyState.synced_at.is_(None),
                LoyaltyState.synced_at <= now - timedelta(seconds=settings.loyalty_cache_ttl_seconds),
                LoyaltyState.last_error.is_not(None),
            ),
        )
    stmt = pg_insert(LoyaltyState).values(user_id=user_id, updated_at=now)
    claimed = await db.scalar(
        stmt.on_conflict_do_update(
            index_elements=[LoyaltyState.user_id], set_={"updated_at": now}, where=claimable
        ).returning(LoyaltyState.user_id)
    )
    await db.commit()
    return claimed is not None


async def claim_due_sync(
    db: AsyncSession,
    user_id: uuid.UUID,
    link: IikoLink | None,
    state: LoyaltyState | None,
    settings: Settings,
    now: datetime,
) -> bool:
    if not (is_linked(link) and sync_due(state, settings, now)):
        return False
    return await claim_sync(db, user_id, settings, now, force=False)


async def mark_link_pending(db: AsyncSession, user_id: uuid.UUID) -> IikoLink | None:
    stmt = pg_insert(IikoLink).values(id=uuid.uuid4(), user_id=user_id, sync_status=SyncStatus.pending)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[IikoLink.user_id],
            set_={"sync_status": SyncStatus.pending, "updated_at": func.now()},
        )
    )
    await db.commit()
    return await load_link(db, user_id)


async def link_user(user_id: uuid.UUID, iiko: IikoCloudClient) -> None:
    async with _user_lock(user_id):
        await sync_user_with_iiko(user_id, iiko)


async def run_loyalty_sync(user_id: uuid.UUID, iiko: IikoCloudClient, *, relink: bool = False) -> None:
    async with _user_lock(user_id):
        if relink:
            await sync_user_with_iiko(user_id, iiko)
        async with async_session_factory() as db:
            await _sync(db, user_id, iiko)


async def sync_user_loyalty(user_id: uuid.UUID, iiko: IikoCloudClient) -> None:
    async with async_session_factory() as db:
        claimed = await claim_sync(db, user_id, get_settings(), datetime.now(timezone.utc), force=False)
    if claimed:
        await run_loyalty_sync(user_id, iiko)


async def _sync(db: AsyncSession, user_id: uuid.UUID, iiko: IikoCloudClient) -> None:
    settings = await get_effective_settings()
    now = datetime.now(timezone.utc)
    user = await db.get(User, user_id)
    link = await load_link(db, user_id)
    state = await load_state(db, user_id)
    if user is None or state is None or not is_linked(link) or is_review_demo(settings, user.phone):
        return
    history_from = await _history_start(db, user_id, settings, now)
    await db.commit()

    customer_id = link.iiko_customer_id
    until = now + timedelta(days=1)
    try:
        customer = await iiko.get_customer_by_id(customer_id, db=db)
        total_spend = await iiko.get_orders_sum_all_time(customer_id, db=db)
        bakeries = await iiko.list_terminal_groups(db=db)
        transactions = await iiko.get_transactions(customer_id, history_from, until, db=db)
        rescan_from = await _rescan_start(db, user_id, transactions, history_from, settings)
        if rescan_from is not None:
            await db.commit()
            transactions = await iiko.get_transactions(customer_id, rescan_from, until, db=db)
    except IikoClientError as exc:
        state.last_error = str(exc)[:_ERROR_MAX_LENGTH]
        await db.commit()
        return

    wallets = customer.get("walletBalances") or []
    bonus_wallet_id = resolve_bonus_wallet_id(
        wallets,
        settings.loyalty_bonus_wallet_id,
        (get_settings().coffee_stamp_wallet_id, settings.coffee_stamp_wallet_id),
    )
    await _store_purchases(
        db, user_id, build_purchases(transactions, bakeries, bonus_wallet_id=bonus_wallet_id)
    )

    tiers = await _load_tiers(db, active_only=False)
    current, _, _, _ = resolve_tier([tier for tier in tiers if tier.is_active], total_spend)

    state.total_spend = total_spend
    state.bonus_balance = pick_wallet_balance(wallets, bonus_wallet_id or "", type_fallback=None)
    state.total_savings = await _total_savings(db, user_id)
    state.coffee_collected = count_coffee_stamps(
        wallets, settings.coffee_stamp_wallet_id, settings.coffee_stamp_goal
    )
    state.tier_code = current.code if current else None
    state.synced_at = now
    state.last_error = None
    await db.commit()

    tier_sync = tier_sync_enabled(settings)
    if tier_sync or state.synced_category_id:
        target_category_id = current.iiko_category_id if tier_sync and current else None
        removable = [*(tier.iiko_category_id for tier in tiers), state.synced_category_id]
        try:
            await _sync_category(db, iiko, user, customer_id, customer, target_category_id, removable)
            state.synced_category_id = target_category_id
        except IikoClientError as exc:
            state.last_error = str(exc)[:_ERROR_MAX_LENGTH]
        await db.commit()

    await process_referral_reward(db, iiko, user, settings)


async def _history_start(
    db: AsyncSession, user_id: uuid.UUID, settings: Settings, now: datetime
) -> datetime:
    latest = await db.scalar(select(func.max(Purchase.ordered_at)).where(Purchase.user_id == user_id))
    if latest is None:
        return now - timedelta(days=settings.loyalty_history_days_initial)
    return latest - timedelta(days=settings.loyalty_history_overlap_days)


async def _earliest_purchase(
    db: AsyncSession, user_id: uuid.UUID, pos_order_ids: Sequence[str]
) -> datetime | None:
    return await db.scalar(
        select(func.min(Purchase.ordered_at)).where(
            Purchase.user_id == user_id, Purchase.pos_order_id.in_(pos_order_ids)
        )
    )


async def _rescan_start(
    db: AsyncSession,
    user_id: uuid.UUID,
    transactions: Sequence[Any],
    history_from: datetime,
    settings: Settings,
) -> datetime | None:
    orphans = orphan_order_ids(transactions)
    if not orphans:
        return None
    earliest = await _earliest_purchase(db, user_id, orphans)
    if earliest is None:
        return None
    start = earliest - timedelta(days=settings.loyalty_history_overlap_days)
    return start if start < history_from else None


async def _store_purchases(
    db: AsyncSession, user_id: uuid.UUID, purchases: Sequence[Mapping[str, Any]]
) -> None:
    for start in range(0, len(purchases), _UPSERT_CHUNK_SIZE):
        rows = [
            {"id": uuid.uuid4(), "user_id": user_id, **purchase}
            for purchase in purchases[start : start + _UPSERT_CHUNK_SIZE]
        ]
        stmt = pg_insert(Purchase).values(rows)
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=[Purchase.user_id, Purchase.pos_order_id],
                set_={field: stmt.excluded[field] for field in _PURCHASE_FIELDS},
            )
        )


async def _total_savings(db: AsyncSession, user_id: uuid.UUID) -> Decimal:
    total = await db.scalar(
        select(func.coalesce(func.sum(Purchase.discount_sum), 0)).where(Purchase.user_id == user_id)
    )
    return Decimal(total)


async def _sync_category(
    db: AsyncSession,
    iiko: IikoCloudClient,
    user: User,
    customer_id: str,
    customer: Mapping[str, Any],
    target_category_id: str | None,
    removable_category_ids: Sequence[str | None],
) -> None:
    to_add, to_remove = plan_category_changes(
        target_category_id, extract_category_ids(customer), removable_category_ids
    )
    for category_id in to_add:
        await iiko.add_customer_category(
            customer_id=customer_id, category_id=category_id, guard_phone=user.phone, db=db
        )
    for category_id in to_remove:
        await iiko.remove_customer_category(
            customer_id=customer_id, category_id=category_id, guard_phone=user.phone, db=db
        )


def _tier_out(tier: LoyaltyTier) -> TierOut:
    return TierOut(
        code=tier.code,
        name=tier.name,
        min_spend=float(tier.min_spend),
        discount_percent=float(tier.discount_percent),
        cashback_percent=float(tier.cashback_percent or 0),
        perks=list(tier.perks or []),
    )


def review_demo_link(settings: Settings) -> IikoLink:
    return IikoLink(sync_status=SyncStatus.synced, card_number=settings.review_demo_card_number)


def _base_tier(settings: Settings) -> TierOut:
    return TierOut(
        code="base",
        name="Скидка",
        min_spend=0,
        discount_percent=settings.loyalty_flat_discount_percent,
        cashback_percent=0,
        perks=[],
    )


def _coffee_out(settings: Settings, state: LoyaltyState | None) -> CoffeeOut | None:
    if not settings.coffee_stamp_wallet_id:
        return None
    collected = state.coffee_collected if state and state.coffee_collected is not None else 0
    return CoffeeOut(collected=collected, goal=settings.coffee_stamp_goal)


def assemble_loyalty_out(
    settings: Settings,
    link: IikoLink | None,
    state: LoyaltyState | None,
    tiers: Sequence[LoyaltyTier],
    now: datetime,
    *,
    refreshing: bool = False,
) -> LoyaltyOut:
    linked = is_linked(link)
    snapshot = state if linked else None
    total_spend = effective_spend(link, state)
    base_tier = _base_tier(settings)

    if settings.loyalty_tiers_enabled:
        ordered = sorted(tiers, key=lambda tier: tier.min_spend)
        current, upcoming, remaining, progress = resolve_tier(ordered, total_spend)
        mode = "tiers"
        current_out = _tier_out(current) if current else base_tier
        next_out = _tier_out(upcoming) if upcoming else None
        tiers_out = [_tier_out(tier) for tier in ordered]
    else:
        mode = "flat"
        current_out, next_out, remaining, progress, tiers_out = base_tier, None, None, 1.0, []

    return LoyaltyOut(
        mode=mode,
        sync_status=link.sync_status.value if link else None,
        card_number=link.card_number if link else None,
        current_tier=current_out,
        next_tier=next_out,
        total_spend=float(total_spend),
        remaining_to_next=float(remaining) if remaining is not None else None,
        progress=progress,
        bonus_balance=float(snapshot.bonus_balance) if snapshot else 0.0,
        total_savings=float(snapshot.total_savings) if snapshot else 0.0,
        coffee=_coffee_out(settings, snapshot),
        tiers=tiers_out,
        updated_at=snapshot.synced_at if snapshot else None,
        stale=refreshing
        or (linked and (link.sync_status != SyncStatus.synced or is_stale(state, settings, now))),
    )


async def build_loyalty_out(
    db: AsyncSession,
    settings: Settings,
    link: IikoLink | None,
    state: LoyaltyState | None,
    now: datetime,
    *,
    refreshing: bool = False,
) -> LoyaltyOut:
    tiers = await _load_tiers(db, active_only=True) if settings.loyalty_tiers_enabled else []
    return assemble_loyalty_out(settings, link, state, tiers, now, refreshing=refreshing)


async def load_current_tier_code(db: AsyncSession, settings: Settings, user: User) -> str | None:
    tiers = await _load_tiers(db, active_only=True)
    if is_review_demo(settings, user.phone):
        link, state = review_demo_link(settings), None
    else:
        link, state = await load_link(db, user.id), await load_state(db, user.id)
    current, _, _, _ = resolve_tier(tiers, effective_spend(link, state))
    return current.code if current else None


def _purchase_out(purchase: Purchase) -> PurchaseOut:
    return PurchaseOut(
        id=str(purchase.id),
        ordered_at=purchase.ordered_at,
        order_number=purchase.order_number,
        order_sum=float(purchase.order_sum),
        paid_sum=float(purchase.paid_sum),
        discount_sum=float(purchase.discount_sum),
        bonus_accrued=float(purchase.bonus_accrued),
        bonus_spent=float(purchase.bonus_spent),
        bakery=purchase.bakery,
    )


async def build_history_out(
    db: AsyncSession, user_id: uuid.UUID, state: LoyaltyState | None, *, limit: int, offset: int
) -> PurchaseHistoryOut:
    total = await db.scalar(
        select(func.count()).select_from(Purchase).where(Purchase.user_id == user_id)
    )
    purchases = await db.scalars(
        select(Purchase)
        .where(Purchase.user_id == user_id)
        .order_by(Purchase.ordered_at.desc(), Purchase.id)
        .limit(limit)
        .offset(offset)
    )
    return PurchaseHistoryOut(
        items=[_purchase_out(purchase) for purchase in purchases],
        total=total or 0,
        updated_at=state.synced_at if state else None,
    )
