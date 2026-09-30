from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db import get_db
from ..deps import get_current_user, get_iiko_client
from ..iiko.client import IikoCloudClient
from ..loyalty.service import (
    build_history_out,
    build_loyalty_out,
    claim_due_sync,
    claim_sync,
    is_linked,
    load_link,
    load_state,
    mark_link_pending,
    needs_relink,
    run_loyalty_sync,
)
from ..models import User
from ..schemas import LoyaltyOut, PurchaseHistoryOut

router = APIRouter(prefix="/loyalty", tags=["loyalty"])


@router.get("", response_model=LoyaltyOut)
async def get_loyalty(
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    iiko: IikoCloudClient = Depends(get_iiko_client),
    settings: Settings = Depends(get_settings),
):
    now = datetime.now(timezone.utc)
    link = await load_link(db, user.id)
    state = await load_state(db, user.id)
    refreshing = await claim_due_sync(db, user.id, link, state, settings, now)
    if refreshing:
        background_tasks.add_task(run_loyalty_sync, user.id, iiko)
    out = await build_loyalty_out(db, settings, link, state, now, refreshing=refreshing)
    await db.commit()
    return out


@router.get("/history", response_model=PurchaseHistoryOut)
async def get_history(
    background_tasks: BackgroundTasks,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    iiko: IikoCloudClient = Depends(get_iiko_client),
    settings: Settings = Depends(get_settings),
):
    now = datetime.now(timezone.utc)
    link = await load_link(db, user.id)
    state = await load_state(db, user.id)
    if await claim_due_sync(db, user.id, link, state, settings, now):
        background_tasks.add_task(run_loyalty_sync, user.id, iiko)
    out = await build_history_out(db, user.id, state, limit=limit, offset=offset)
    await db.commit()
    return out


@router.post("/refresh", response_model=LoyaltyOut)
async def refresh_loyalty(
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    iiko: IikoCloudClient = Depends(get_iiko_client),
    settings: Settings = Depends(get_settings),
):
    now = datetime.now(timezone.utc)
    link = await load_link(db, user.id)
    relink = bool(user.name.strip()) and needs_relink(link, now)
    refreshing = relink or is_linked(link)
    if refreshing:
        if not await claim_sync(db, user.id, settings, now, force=True):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Обновлять можно не чаще раза в минуту")
        if relink:
            link = await mark_link_pending(db, user.id)
        background_tasks.add_task(run_loyalty_sync, user.id, iiko, relink=relink)
    state = await load_state(db, user.id)
    out = await build_loyalty_out(db, settings, link, state, now, refreshing=refreshing)
    await db.commit()
    return out
