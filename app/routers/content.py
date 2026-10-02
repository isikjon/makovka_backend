from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..content import assemble_content_out, load_active_content
from ..db import get_db
from ..deps import get_current_user
from ..loyalty.service import load_current_tier_code
from ..models import User
from ..runtime_settings import get_effective_settings
from ..schemas import ContentOut

router = APIRouter(prefix="/content", tags=["content"])


@router.get("", response_model=ContentOut)
async def get_content(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    novelties, promotions, bakeries = await load_active_content(db)
    tier_code = await load_current_tier_code(db, settings, user) if settings.loyalty_tiers_enabled else None
    return assemble_content_out(
        settings,
        novelties,
        promotions,
        bakeries,
        now=datetime.now(timezone.utc),
        phone=user.phone,
        tier_code=tier_code,
    )
