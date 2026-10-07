import ipaddress
import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db import get_db
from ..deps import get_sms_client
from ..models import IikoLink, OtpCode, RefreshToken, SyncStatus, User
from ..rate_limit import SlidingWindowLimiter
from ..schemas import (
    OtpChannel,
    OtpRequestIn,
    OtpRequestOut,
    OtpVerifyIn,
    RefreshIn,
    TokenPairOut,
)
from ..security import (
    create_access_token,
    generate_refresh_token,
    hash_otp_code,
    hash_refresh_token,
    verify_otp_code,
)
from ..sms.client import SmsCallResult, SmsRuClient, SmsSendError

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("app.auth")

_MAX_OTP_ATTEMPTS: dict[str, int] = {"sms": 5, "call": 3}
_RATE_WINDOW = timedelta(hours=1)
_FAILED_ATTEMPTS_WINDOW = timedelta(days=1)
_UNKNOWN_IP = "-1"
_PHONE_LOCK = text("SELECT pg_try_advisory_xact_lock(hashtext(:key))")

call_ip_limiter = SlidingWindowLimiter(_RATE_WINDOW.total_seconds())


def _client_ip(request: Request) -> str:
    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return _UNKNOWN_IP
    return _UNKNOWN_IP if address.is_loopback else host


def _fixed_code(settings: Settings, phone: str) -> str | None:
    if settings.review_bypass_phone and phone == settings.review_bypass_phone:
        return settings.review_bypass_code
    if settings.otp_mock_mode:
        return settings.otp_dev_code
    return None


def _fixed_call_code(settings: Settings, phone: str) -> str | None:
    raw = _fixed_code(settings, phone)
    if raw is None:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        raise RuntimeError("Configured OTP code must contain digits")
    # Legacy review/dev settings sometimes contained a six-digit SMS code.
    # The only supported live flow is a four-digit call code, so keep the
    # compatibility value deterministic while never returning six digits.
    return digits[-4:].zfill(4)


def _mask_phone(phone: str) -> str:
    return f"{phone[:3]}***{phone[-2:]}"


def _active_code_filter(phone: str, now: datetime) -> tuple:
    return OtpCode.phone == phone, OtpCode.consumed_at.is_(None), OtpCode.expires_at > now


async def _count_recent(db: AsyncSession, phone: str, now: datetime, channel: OtpChannel | None = None) -> int:
    query = select(func.count()).select_from(OtpCode).where(
        OtpCode.phone == phone, OtpCode.created_at > now - _RATE_WINDOW
    )
    if channel is not None:
        query = query.where(OtpCode.channel == channel)
    return await db.scalar(query)


def _too_many(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail)


def _delivery_failed(exc: SmsSendError) -> HTTPException:
    return HTTPException(status.HTTP_502_BAD_GATEWAY, f"Не удалось отправить код: {exc}")


async def _lock_phone(db: AsyncSession, phone: str) -> None:
    if not await db.scalar(_PHONE_LOCK, {"key": f"otp:{phone}"}):
        raise _too_many("Запрос по этому номеру уже обрабатывается, попробуйте позже")


async def _ensure_attempts_left(db: AsyncSession, settings: Settings, phone: str, now: datetime) -> None:
    failed = await db.scalar(
        select(func.coalesce(func.sum(OtpCode.attempts), 0)).where(
            OtpCode.phone == phone, OtpCode.created_at > now - _FAILED_ATTEMPTS_WINDOW
        )
    )
    if failed >= settings.otp_failed_attempts_per_day_per_phone:
        raise _too_many("Слишком много неверных попыток, попробуйте позже")


async def _request_call(
    db: AsyncSession, sms: SmsRuClient, settings: Settings, phone: str, ip: str, now: datetime
) -> SmsCallResult:
    if await _count_recent(db, phone, now, "call") >= settings.otp_calls_per_hour_per_phone:
        raise _too_many("Слишком много звонков на этот номер, попробуйте позже")
    if ip != _UNKNOWN_IP and not call_ip_limiter.try_acquire(ip, settings.otp_calls_per_hour_per_ip):
        raise _too_many("Слишком много запросов звонка, попробуйте позже")
    logger.warning(
        "otp_delivery_started channel=call endpoint=/code/call phone=%s ip=%s",
        _mask_phone(phone),
        ip,
    )
    try:
        result = await sms.call_otp(phone, ip)
    except SmsSendError as exc:
        logger.warning(
            "otp_delivery_result channel=call endpoint=/code/call phone=%s ip=%s status_code=%s error=%s",
            _mask_phone(phone),
            ip,
            exc.status_code,
            exc,
        )
        raise _delivery_failed(exc) from exc
    logger.warning(
        "otp_delivery_result channel=call endpoint=/code/call phone=%s ip=%s status_code=%s call_id=%s",
        _mask_phone(phone),
        ip,
        result.status_code,
        result.call_id or "-",
    )
    return result


@router.post("/otp/request", response_model=OtpRequestOut)
async def request_otp(
    body: OtpRequestIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    sms: SmsRuClient = Depends(get_sms_client),
):
    now = datetime.now(timezone.utc)
    await _lock_phone(db, body.phone)
    await _ensure_attempts_left(db, settings, body.phone, now)

    # `channel=sms` is accepted for old app builds, but is deliberately
    # normalized to the only supported production flow: SMS.ru code call.
    if body.channel == "sms":
        logger.info("otp_legacy_channel_normalized requested=sms effective=call")

    fixed_code = _fixed_call_code(settings, body.phone)
    if fixed_code is None:
        if await _count_recent(db, body.phone, now) >= settings.otp_requests_per_hour_per_phone:
            raise _too_many("Слишком много запросов кода, попробуйте позже")
        call = await _request_call(db, sms, settings, body.phone, _client_ip(request), now)
        code, channel = call.code, "call"
    else:
        code, channel = fixed_code, "call"

    await db.execute(update(OtpCode).where(*_active_code_filter(body.phone, now)).values(consumed_at=now))

    db.add(
        OtpCode(
            phone=body.phone,
            code_hash=hash_otp_code(code),
            channel=channel,
            expires_at=now + timedelta(minutes=settings.otp_ttl_minutes),
        )
    )
    await db.commit()

    return OtpRequestOut(
        phone=body.phone,
        expires_in_seconds=settings.otp_ttl_minutes * 60,
        dev_code=code if settings.otp_mock_mode else None,
        channel=channel,
        code_length=len(code),
    )


@router.post("/otp/verify", response_model=TokenPairOut)
async def verify_otp(
    body: OtpVerifyIn,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    now = datetime.now(timezone.utc)
    await _lock_phone(db, body.phone)
    result = await db.execute(
        select(OtpCode).where(*_active_code_filter(body.phone, now)).order_by(OtpCode.created_at.desc())
    )
    otp = result.scalars().first()
    if otp is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Код не найден или истёк, запросите новый")
    await _ensure_attempts_left(db, settings, body.phone, now)
    if otp.attempts >= _MAX_OTP_ATTEMPTS[otp.channel]:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Слишком много попыток, запросите новый код")
    if otp.channel == "call" and re.fullmatch(r"[0-9]{4}", body.code) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ожидается четырехзначный код")
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
            expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
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
