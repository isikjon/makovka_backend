import asyncio
import logging

from sqlalchemy import ColumnElement, Select, or_, select

from ..config import Settings
from ..db import async_session_factory, engine
from ..iiko.client import IikoCloudClient
from ..loyalty.service import sync_user_loyalty, tier_sync_enabled
from ..models import IikoLink, LoyaltyState, User
from ..referral.service import awaiting_reward_clause
from ..runtime_settings import get_effective_settings

logger = logging.getLogger("app.jobs.loyalty_sync")


def referral_rewards_active(settings: Settings) -> bool:
    return settings.referral_rewards_enabled and settings.referral_bonus_amount > 0


def tier_cleanup_clause() -> ColumnElement[bool]:
    return (
        select(LoyaltyState.user_id)
        .where(LoyaltyState.user_id == User.id, LoyaltyState.synced_category_id.is_not(None))
        .exists()
    )


def candidates_query(settings: Settings) -> Select:
    query = (
        select(IikoLink.user_id)
        .join(User, User.id == IikoLink.user_id)
        .where(IikoLink.iiko_customer_id.is_not(None))
        .order_by(IikoLink.linked_at)
    )
    if settings.review_demo_card_number and settings.review_bypass_phone:
        query = query.where(User.phone != settings.review_bypass_phone)
    if not tier_sync_enabled(settings):
        pending = [tier_cleanup_clause()]
        if referral_rewards_active(settings):
            pending.append(awaiting_reward_clause())
        query = query.where(or_(*pending))
    return query


async def run() -> int:
    try:
        return await _sync_candidates(await get_effective_settings())
    finally:
        await engine.dispose()


async def _sync_candidates(settings: Settings) -> int:
    async with async_session_factory() as db:
        user_ids = list(await db.scalars(candidates_query(settings)))
    if not user_ids:
        logger.info("loyalty sync: nothing to do")
        return 0
    iiko = IikoCloudClient(settings)
    failed = 0
    try:
        for user_id in user_ids:
            try:
                await sync_user_loyalty(user_id, iiko)
            except Exception:
                failed += 1
                logger.exception("loyalty sync crashed for user %s", user_id)
    finally:
        await iiko.aclose()

    logger.info("loyalty sync finished: %d users, %d crashed", len(user_ids), failed)
    return 1 if failed else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
