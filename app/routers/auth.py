from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db import get_db
from ..deps import get_sms_client
from ..models import IikoLink, OtpCode, RefreshToken, SyncStatus, User
from ..schemas import (
    OtpRequestIn,
    OtpRequestOut,
    OtpVerifyIn,
    RefreshIn,
    TokenPairOut,
)
from ..security import (
    create_access_token,
    generate_otp_code,
    generate_refresh_token,
    hash_otp_code,
    hash_refresh_token,
    verify_otp_code,
)
from ..sms.client import SmsRuClient, SmsSendError

router = APIRouter(prefix="/auth", tags=["auth"])

_MAX_OTP_ATTEMPTS = 5


@router.post("/otp/request", response_model=OtpRequestOut)
async def request_otp(
    body: OtpRequestIn,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    sms: SmsRuClient = Depends(get_sms_client),
):
    now = datetime.now(timezone.utc)
    existing = await db.execute(
        select(OtpCode)
        .where(OtpCode.phone == body.phone, OtpCode.consumed_at.is_(None), OtpCode.expires_at > now)
        .order_by(OtpCode.created_at.desc())
    )
    if existing.scalars().first() is not None:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Код уже отправлен, дождитесь окончания срока действия")

    is_review_bypass = bool(settings.review_bypass_phone) and body.phone == settings.review_bypass_phone
    if is_review_bypass:
        code = settings.review_bypass_code
    elif settings.otp_mock_mode:
        code = settings.otp_dev_code
    else:
        code = generate_otp_code()

    if not settings.otp_mock_mode and not is_review_bypass:
        try:
            await sms.send_otp(body.phone, code)
        except SmsSendError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Не удалось отправить SMS: {exc}",
            ) from exc

    otp = OtpCode(
        phone=body.phone,
        code_hash=hash_otp_code(code),
        expires_at=now + timedelta(minutes=settings.otp_ttl_minutes),
    )
    db.add(otp)
    await db.commit()

    return OtpRequestOut(
        phone=body.phone,
        expires_in_seconds=settings.otp_ttl_minutes * 60,
        dev_code=code if settings.otp_mock_mode else None,
    )


@router.post("/otp/verify", response_model=TokenPairOut)
async def verify_otp(body: OtpVerifyIn, db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(OtpCode)
        .where(OtpCode.phone == body.phone, OtpCode.consumed_at.is_(None), OtpCode.expires_at > now)
        .order_by(OtpCode.created_at.desc())
    )
    otp = result.scalars().first()
    if otp is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Код не найден или истёк, запросите новый")
    if otp.attempts >= _MAX_OTP_ATTEMPTS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Слишком много попыток, запросите новый код")
    if not verify_otp_code(body.code, otp.code_hash):
        otp.attempts += 1
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный код")

    otp.consumed_at = now
    await db.commit()

    result = await db.execute(select(User).where(User.phone == body.phone))
    user = result.scalar_one_or_none()
    is_new_user = user is None
    if user is None:
        user = User(phone=body.phone)
        db.add(user)
        await db.flush()
        db.add(IikoLink(user_id=user.id, sync_status=SyncStatus.pending))
        await db.commit()

    access_token = create_access_token(user.id)
    refresh_token = generate_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(refresh_token),
            expires_at=now + timedelta(days=get_settings().refresh_token_ttl_days),
        )
    )
    await db.commit()

    return TokenPairOut(access_token=access_token, refresh_token=refresh_token, is_new_user=is_new_user)


@router.post("/refresh", response_model=TokenPairOut)
async def refresh(body: RefreshIn, db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    token_hash = hash_refresh_token(body.refresh_token)
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    stored = result.scalar_one_or_none()
    if stored is None or stored.revoked_at is not None or stored.expires_at <= now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Недействительный refresh-токен")

    stored.revoked_at = now
    new_refresh = generate_refresh_token()
    db.add(
        RefreshToken(
            user_id=stored.user_id,
            token_hash=hash_refresh_token(new_refresh),
            expires_at=now + timedelta(days=get_settings().refresh_token_ttl_days),
        )
    )
    await db.commit()

    return TokenPairOut(access_token=create_access_token(stored.user_id), refresh_token=new_refresh)
