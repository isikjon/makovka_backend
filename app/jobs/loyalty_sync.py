import asyncio
import logging

from sqlalchemy import select

from ..config import get_settings
from ..db import async_session_factory, engine
from ..iiko.client import IikoCloudClient
from ..loyalty.service import sync_user_loyalty
from ..models import IikoLink

logger = logging.getLogger("app.jobs.loyalty_sync")


async def run() -> int:
    settings = get_settings()
    if not (settings.loyalty_tiers_enabled and settings.loyalty_tier_sync_enabled):
        logger.info("loyalty tier sync is disabled, nothing to do")
        return 0

    iiko = IikoCloudClient(settings)
    failed = 0
    try:
        async with async_session_factory() as db:
            user_ids = list(
                await db.scalars(
                    select(IikoLink.user_id)
                    .where(IikoLink.iiko_customer_id.is_not(None))
                    .order_by(IikoLink.linked_at)
                )
            )
        for user_id in user_ids:
            try:
                await sync_user_loyalty(user_id, iiko)
            except Exception:
                failed += 1
                logger.exception("loyalty sync crashed for user %s", user_id)
    finally:
        await iiko.aclose()
        await engine.dispose()

    logger.info("loyalty sync finished: %d users, %d crashed", len(user_ids), failed)
    return 1 if failed else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
