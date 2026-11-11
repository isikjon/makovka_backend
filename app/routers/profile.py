from fastapi import APIRouter, BackgroundTasks, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings, is_review_demo
from ..db import get_db
from ..deps import get_current_user, get_iiko_client
from ..iiko.client import IikoCloudClient
from ..loyalty.service import link_user
from ..models import IikoLink, User
from ..schemas import IikoLinkOut, ProfileIn, ProfileOut

router = APIRouter(prefix="/profile", tags=["profile"])


async def _load_iiko_out(user: User, db: AsyncSession, settings: Settings) -> IikoLinkOut | None:
    if is_review_demo(settings, user.phone):
        return IikoLinkOut(sync_status="synced", card_number=settings.review_demo_card_number)
    result = await db.execute(select(IikoLink).where(IikoLink.user_id == user.id))
    link = result.scalar_one_or_none()
    if link is None:
        return None
    return IikoLinkOut(sync_status=link.sync_status.value, card_number=link.card_number, last_error=link.last_error)


async def _load_out(user: User, db: AsyncSession, settings: Settings) -> ProfileOut:
    return ProfileOut(
        phone=user.phone,
        name=user.name,
        surname=user.surname,
        email=user.email,
        birthdate=user.birthdate,
        gender=user.gender,
        promo_code=user.promo_code,
        iiko=await _load_iiko_out(user, db, settings),
    )


@router.get("", response_model=ProfileOut)
async def get_profile(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    return await _load_out(user, db, settings)


@router.put("", response_model=ProfileOut)
async def update_profile(
    body: ProfileIn,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    iiko: IikoCloudClient = Depends(get_iiko_client),
    settings: Settings = Depends(get_settings),
):
    user.name = body.name
    user.surname = body.surname
    user.email = body.email
    user.birthdate = body.birthdate
    user.gender = body.gender
    user.promo_code = body.promo_code
    await db.commit()

    if user.name.strip() and not is_review_demo(settings, user.phone):
        background_tasks.add_task(link_user, user.id, iiko)

    return await _load_out(user, db, settings)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await db.delete(user)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
