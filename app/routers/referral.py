from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..referral.service import ReferralError, apply_referral_code, build_referral_out
from ..runtime_settings import get_effective_settings
from ..schemas import ReferralApplyIn, ReferralApplyOut, ReferralOut

router = APIRouter(prefix="/referral", tags=["referral"])


@router.get("", response_model=ReferralOut)
async def get_referral(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    out = await build_referral_out(db, user, settings)
    await db.commit()
    return out


@router.post("/apply", response_model=ReferralApplyOut)
async def apply_referral(
    body: ReferralApplyIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    try:
        await apply_referral_code(db, user, body.code, settings)
    except ReferralError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    return ReferralApplyOut(applied=True)
