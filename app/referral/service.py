from __future__ import annotations

import enum
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import ColumnElement, and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from ..config import Settings, is_review_demo
from ..iiko.client import IikoClientError, IikoCloudClient, IikoMutationBlocked
from ..models import IikoLink, Purchase, ReferralReward, User
from ..schemas import ReferralOut

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
REWARD_MAX_ATTEMPTS = 5
REWARD_CLAIM_TIMEOUT = timedelta(minutes=15)

_CODE_ALLOCATION_ATTEMPTS = 8
_INPUT_CODE = re.compile(r"[A-Z0-9]{4,16}")
_CYRILLIC_LOOKALIKES = str.maketrans("АВСЕНКМРТХУавсенкмртху", "ABCEHKMPTXYABCEHKMPTXY")
_ERROR_MAX_LENGTH = 1000
_CENT = Decimal("0.01")
_SAFE_TRANSPORT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

CODE_NOT_FOUND = "Промокод не найден"
OWN_CODE = "Нельзя использовать свой промокод"
MUTUAL_CODE = "Нельзя использовать промокод приглашённого вами друга"
ALREADY_APPLIED = "Промокод уже применён"

REFERRED_COMMENT = "Бонус за регистрацию по приглашению друга"
REFERRER_COMMENT = "Бонус за приглашённого друга"

UNKNOWN_OUTCOME = "Результат начисления неизвестен, проверьте кошелёк гостя в iiko"
INTERRUPTED = f"{UNKNOWN_OUTCOME}: начисление было прервано"
NO_BONUS_WALLET = "Не настроен бонусный кошелёк iiko (LOYALTY_BONUS_WALLET_ID)"
NOT_LINKED = "Оба гостя должны быть привязаны к iiko"
DEMO_REFERRER = "Пригласивший гость — демо-аккаунт для проверки в магазинах приложений, бонусы не начисляются"


class ReferralError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def generate_referral_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(raw: str) -> str | None:
    code = raw.strip().translate(_CYRILLIC_LOOKALIKES).upper()
    return code if _INPUT_CODE.fullmatch(code) else None


def share_text(settings: Settings, code: str) -> str:
    return (
        f"Мой промокод в приложении пекарни: {code}. "
        f"Скачайте приложение и введите промокод при регистрации: {settings.referral_share_url}"
    )


async def ensure_referral_code(db: AsyncSession, user: User) -> str:
    if user.referral_code:
        return user.referral_code
    user_id = user.id
    for _ in range(_CODE_ALLOCATION_ATTEMPTS):
        code = generate_referral_code()
        try:
            async with db.begin_nested():
                assigned = await db.scalar(
                    update(User)
                    .where(User.id == user_id, User.referral_code.is_(None))
                    .values(referral_code=code)
                    .returning(User.referral_code)
                    .execution_options(synchronize_session=False)
                )
        except IntegrityError:
            continue
        await db.commit()
        if assigned is None:
            await db.refresh(user, ["referral_code"])
        else:
            set_committed_value(user, "referral_code", assigned)
        if user.referral_code:
            return user.referral_code
    raise RuntimeError("could not allocate a unique referral code")


async def apply_referral_code(db: AsyncSession, user: User, raw_code: str, settings: Settings) -> None:
    if user.referred_by_id is not None:
        raise ReferralError(409, ALREADY_APPLIED)
    code = normalize_code(raw_code)
    referrer = await db.scalar(select(User).where(User.referral_code == code)) if code else None
    if referrer is None or is_review_demo(settings, referrer.phone):
        raise ReferralError(400, CODE_NOT_FOUND)
    if referrer.id == user.id:
        raise ReferralError(400, OWN_CODE)
    user_id, referrer_id = user.id, referrer.id
    locked = await db.execute(
        select(User.id, User.referred_by_id)
        .where(User.id.in_([user_id, referrer_id]))
        .order_by(User.id)
        .with_for_update()
    )
    referred_by = {row_id: referred_by_id for row_id, referred_by_id in locked}
    if referred_by.get(user_id) is not None:
        await db.rollback()
        raise ReferralError(409, ALREADY_APPLIED)
    if referred_by.get(referrer_id) == user_id:
        await db.rollback()
        raise ReferralError(400, MUTUAL_CODE)
    referred_at = await db.scalar(
        update(User)
        .where(User.id == user_id)
        .values(referred_by_id=referrer_id, referred_at=func.now())
        .returning(User.referred_at)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    set_committed_value(user, "referred_by_id", referrer_id)
    set_committed_value(user, "referred_at", referred_at)


async def build_referral_out(db: AsyncSession, user: User, settings: Settings) -> ReferralOut:
    code = await ensure_referral_code(db, user)
    invited = await db.scalar(select(func.count()).select_from(User).where(User.referred_by_id == user.id))
    rewarded = await db.scalar(
        select(func.count())
        .select_from(ReferralReward)
        .where(ReferralReward.referrer_user_id == user.id, ReferralReward.referrer_done.is_(True))
    )
    return ReferralOut(
        code=code,
        invited_count=invited or 0,
        rewarded_count=rewarded or 0,
        bonus_per_friend=settings.referral_bonus_amount,
        rewards_enabled=settings.referral_rewards_enabled,
        share_text=share_text(settings, code),
    )


def awaiting_reward_clause() -> ColumnElement[bool]:
    settled = select(ReferralReward.id).where(
        ReferralReward.referred_user_id == User.id,
        or_(
            and_(ReferralReward.referred_done.is_(True), ReferralReward.referrer_done.is_(True)),
            ReferralReward.attempts >= REWARD_MAX_ATTEMPTS,
        ),
    )
    return and_(User.referred_by_id.is_not(None), ~settled.exists())


def outcome_unknown(exc: IikoClientError) -> bool:
    if isinstance(exc, IikoMutationBlocked):
        return False
    if exc.status_code is not None:
        return exc.status_code >= 500
    return not isinstance(exc.__cause__, _SAFE_TRANSPORT_ERRORS)


@dataclass(frozen=True)
class RewardParties:
    referred_customer_id: str | None
    referrer_phone: str | None
    referrer_customer_id: str | None


@dataclass(frozen=True)
class RewardSide:
    done: str
    started_at: str
    comment: str


REFERRED_SIDE = RewardSide("referred_done", "referred_started_at", REFERRED_COMMENT)
REFERRER_SIDE = RewardSide("referrer_done", "referrer_started_at", REFERRER_COMMENT)


class SideStatus(enum.Enum):
    paid = "paid"
    skipped = "skipped"
    failed = "failed"
    unknown = "unknown"


@dataclass(frozen=True)
class SideOutcome:
    status: SideStatus
    error: str | None = None
    counted: bool = False


async def _load_reward(db: AsyncSession, referred_user_id: uuid.UUID) -> ReferralReward | None:
    return await db.scalar(
        select(ReferralReward)
        .where(ReferralReward.referred_user_id == referred_user_id)
        .execution_options(populate_existing=True)
    )


async def _has_qualifying_purchase(db: AsyncSession, user: User) -> bool:
    return bool(
        await db.scalar(
            select(exists().where(Purchase.user_id == user.id, Purchase.ordered_at >= user.referred_at))
        )
    )


async def _create_reward(db: AsyncSession, user: User, amount: Decimal) -> ReferralReward | None:
    stmt = pg_insert(ReferralReward).values(
        id=uuid.uuid4(), referred_user_id=user.id, referrer_user_id=user.referred_by_id, amount=amount
    )
    await db.execute(stmt.on_conflict_do_nothing(index_elements=[ReferralReward.referred_user_id]))
    await db.commit()
    return await _load_reward(db, user.id)


async def _load_parties(db: AsyncSession, user: User, reward: ReferralReward) -> RewardParties:
    links = {
        link.user_id: link.iiko_customer_id
        for link in await db.scalars(
            select(IikoLink).where(IikoLink.user_id.in_([user.id, reward.referrer_user_id]))
        )
    }
    referrer_phone = await db.scalar(select(User.phone).where(User.id == reward.referrer_user_id))
    return RewardParties(links.get(user.id), referrer_phone, links.get(reward.referrer_user_id))


async def _update_reward(db: AsyncSession, reward_id: uuid.UUID, *conditions: Any, **values: Any) -> bool:
    updated = await db.scalar(
        update(ReferralReward)
        .where(ReferralReward.id == reward_id, *conditions)
        .values(**values)
        .returning(ReferralReward.id)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return updated is not None


async def _claim_side(db: AsyncSession, reward_id: uuid.UUID, side: RewardSide) -> bool:
    return await _update_reward(
        db,
        reward_id,
        getattr(ReferralReward, side.done).is_(False),
        getattr(ReferralReward, side.started_at).is_(None),
        **{side.started_at: func.now()},
    )


async def _release_side(db: AsyncSession, reward_id: uuid.UUID, side: RewardSide) -> None:
    await _update_reward(db, reward_id, **{side.started_at: None})


async def _finish_side(db: AsyncSession, reward_id: uuid.UUID, side: RewardSide) -> None:
    await _update_reward(db, reward_id, **{side.done: True})


async def _record_result(
    db: AsyncSession, reward_id: uuid.UUID, error: str | None, *, counted: bool = False, final: bool = False
) -> None:
    values: dict[str, Any] = {"last_error": error[:_ERROR_MAX_LENGTH] if error else None}
    if final:
        values["attempts"] = REWARD_MAX_ATTEMPTS
    elif counted:
        values["attempts"] = ReferralReward.attempts + 1
    await _update_reward(db, reward_id, **values)


async def _pay_side(
    db: AsyncSession,
    iiko: IikoCloudClient,
    reward: ReferralReward,
    side: RewardSide,
    customer_id: str,
    phone: str,
    wallet_id: str,
) -> SideOutcome:
    if getattr(reward, side.done):
        return SideOutcome(SideStatus.skipped)
    started_at = getattr(reward, side.started_at)
    if started_at is not None:
        if datetime.now(timezone.utc) - started_at < REWARD_CLAIM_TIMEOUT:
            return SideOutcome(SideStatus.skipped)
        return SideOutcome(SideStatus.unknown, INTERRUPTED)
    try:
        iiko.guard_mutation(phone)
    except IikoMutationBlocked as exc:
        return SideOutcome(SideStatus.failed, str(exc))
    if not await _claim_side(db, reward.id, side):
        return SideOutcome(SideStatus.skipped)
    try:
        await iiko.topup_wallet(
            customer_id=customer_id,
            wallet_id=wallet_id,
            amount=reward.amount,
            comment=side.comment,
            guard_phone=phone,
        )
    except IikoClientError as exc:
        if outcome_unknown(exc):
            return SideOutcome(SideStatus.unknown, f"{UNKNOWN_OUTCOME}: {exc}")
        await _release_side(db, reward.id, side)
        return SideOutcome(SideStatus.failed, str(exc), counted=not isinstance(exc, IikoMutationBlocked))
    await _finish_side(db, reward.id, side)
    return SideOutcome(SideStatus.paid)


async def process_referral_reward(
    db: AsyncSession, iiko: IikoCloudClient, user: User, settings: Settings
) -> None:
    if not settings.referral_rewards_enabled or user.referred_by_id is None:
        return
    amount = Decimal(str(settings.referral_bonus_amount)).quantize(_CENT)
    reward = await _load_reward(db, user.id)
    if reward is None:
        if amount <= 0 or not await _has_qualifying_purchase(db, user):
            await db.commit()
            return
        reward = await _create_reward(db, user, amount)
    if reward is None or (reward.referred_done and reward.referrer_done):
        await db.commit()
        return
    if reward.attempts >= REWARD_MAX_ATTEMPTS:
        await db.commit()
        return

    parties = await _load_parties(db, user, reward)
    await db.commit()
    if parties.referrer_phone and is_review_demo(settings, parties.referrer_phone):
        await _record_result(db, reward.id, DEMO_REFERRER, final=True)
        return
    wallet_id = settings.loyalty_bonus_wallet_id
    if not wallet_id:
        await _record_result(db, reward.id, NO_BONUS_WALLET)
        return
    if not (parties.referred_customer_id and parties.referrer_customer_id and parties.referrer_phone):
        await _record_result(db, reward.id, NOT_LINKED)
        return

    targets = (
        (REFERRED_SIDE, parties.referred_customer_id, user.phone),
        (REFERRER_SIDE, parties.referrer_customer_id, parties.referrer_phone),
    )
    outcomes: list[SideOutcome] = []
    for side, customer_id, phone in targets:
        outcome = await _pay_side(db, iiko, reward, side, customer_id, phone, wallet_id)
        if outcome.status is SideStatus.unknown:
            await _record_result(db, reward.id, outcome.error, final=True)
            return
        outcomes.append(outcome)

    errors = [outcome.error for outcome in outcomes if outcome.error]
    if errors or any(outcome.status is SideStatus.paid for outcome in outcomes):
        await _record_result(
            db, reward.id, "; ".join(errors) or None, counted=any(outcome.counted for outcome in outcomes)
        )
