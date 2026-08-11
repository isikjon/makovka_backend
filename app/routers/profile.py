from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user, get_iiko_client
from ..iiko.client import IikoCloudClient
from ..iiko.sync import sync_user_with_iiko
from ..models import IikoLink, User
from ..schemas import IikoLinkOut, ProfileIn, ProfileOut

router = APIRouter(prefix="/profile", tags=["profile"])


async def _load_out(user: User, db: AsyncSession) -> ProfileOut:
    result = await db.execute(select(IikoLink).where(IikoLink.user_id == user.id))
    link = result.scalar_one_or_none()
    return ProfileOut(
        phone=user.phone,
        name=user.name,
        surname=user.surname,
        email=user.email,
        birthdate=user.birthdate,
        gender=user.gender,
        promo_code=user.promo_code,
        iiko=IikoLinkOut(
            sync_status=link.sync_status.value, card_number=link.card_number, last_error=link.last_error
        )
        if link
        else None,
    )


@router.get("", response_model=ProfileOut)
async def get_profile(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _load_out(user, db)


@router.put("", response_model=ProfileOut)
async def update_profile(
    body: ProfileIn,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    iiko: IikoCloudClient = Depends(get_iiko_client),
):
    user.name = body.name
    user.surname = body.surname
    user.email = body.email
    user.birthdate = body.birthdate
    user.gender = body.gender
    user.promo_code = body.promo_code
    await db.commit()

    if user.name.strip():
        background_tasks.add_task(sync_user_with_iiko, user.id, iiko)

    return await _load_out(user, db)
