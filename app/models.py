import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class SyncStatus(str, enum.Enum):
    pending = "pending"
    synced = "synced"
    failed = "failed"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("referral_code", name="uq_users_referral_code"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    surname: Mapped[str] = mapped_column(String(100), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    birthdate: Mapped[str] = mapped_column(String(10), default="")
    gender: Mapped[str] = mapped_column(String(10), default="")
    promo_code: Mapped[str] = mapped_column(String(50), default="")
    referral_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    referred_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    referred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    iiko_link: Mapped["IikoLink | None"] = relationship(back_populates="user", uselist=False)
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(back_populates="user")


class IikoLink(Base):
    __tablename__ = "iiko_links"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    iiko_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    card_number: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    guest_category_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sync_status: Mapped[SyncStatus] = mapped_column(
        Enum(SyncStatus, name="sync_status"), default=SyncStatus.pending
    )
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="iiko_link")


class IikoApiCall(Base):
    __tablename__ = "iiko_api_calls"

    id: Mapped[uuid.UUID] = _uuid_pk()
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    endpoint: Mapped[str] = mapped_column(String(255))
    request_body: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status_code: Mapped[int | None] = mapped_column(nullable=True)
    ok: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OtpCode(Base):
    __tablename__ = "otp_codes"
    __table_args__ = (Index("ix_otp_codes_phone_created", "phone", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    phone: Mapped[str] = mapped_column(String(20), index=True)
    code_hash: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(default=0)
    channel: Mapped[str] = mapped_column(String(10), server_default=text("'sms'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")


class LoyaltyTier(Base):
    __tablename__ = "loyalty_tiers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    min_spend: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    discount_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    cashback_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), server_default=text("0"))
    perks: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    iiko_category_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sort_order: Mapped[int] = mapped_column(server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(server_default=true())


class LoyaltyState(Base):
    __tablename__ = "loyalty_states"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    total_spend: Mapped[Decimal] = mapped_column(Numeric(14, 2), server_default=text("0"))
    bonus_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), server_default=text("0"))
    total_savings: Mapped[Decimal] = mapped_column(Numeric(14, 2), server_default=text("0"))
    coffee_collected: Mapped[int | None] = mapped_column(nullable=True)
    tier_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    synced_category_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Purchase(Base):
    __tablename__ = "purchases"
    __table_args__ = (
        UniqueConstraint("user_id", "pos_order_id", name="uq_purchases_user_pos_order"),
        Index("ix_purchases_user_ordered_at", "user_id", "ordered_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    pos_order_id: Mapped[str] = mapped_column(String(64))
    order_number: Mapped[int | None] = mapped_column(nullable=True)
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    order_sum: Mapped[Decimal] = mapped_column(Numeric(14, 2), server_default=text("0"))
    paid_sum: Mapped[Decimal] = mapped_column(Numeric(14, 2), server_default=text("0"))
    discount_sum: Mapped[Decimal] = mapped_column(Numeric(14, 2), server_default=text("0"))
    bonus_accrued: Mapped[Decimal] = mapped_column(Numeric(14, 2), server_default=text("0"))
    bonus_spent: Mapped[Decimal] = mapped_column(Numeric(14, 2), server_default=text("0"))
    terminal_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bakery: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


PROMOTION_KINDS = ("generic", "coffee_stamps", "referral", "happy_hours", "combo")
PROMOTION_AUDIENCES = ("all", "phones", "tiers")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class Novelty(Base):
    __tablename__ = "novelties"

    id: Mapped[uuid.UUID] = _uuid_pk()
    title: Mapped[str] = mapped_column(String(120))
    image: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sort_order: Mapped[int] = mapped_column(server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Promotion(Base):
    __tablename__ = "promotions"
    __table_args__ = (
        CheckConstraint(_in_list("kind", PROMOTION_KINDS), name="ck_promotions_kind"),
        CheckConstraint(_in_list("audience", PROMOTION_AUDIENCES), name="ck_promotions_audience"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(20), server_default=text("'generic'"))
    title: Mapped[str] = mapped_column(String(120))
    subtitle: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    badge: Mapped[str | None] = mapped_column(String(20), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    time_text: Mapped[str | None] = mapped_column(String(60), nullable=True)
    image: Mapped[str | None] = mapped_column(String(64), nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    show_on_home: Mapped[bool] = mapped_column(server_default=false())
    sort_order: Mapped[int] = mapped_column(server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(server_default=true())
    audience: Mapped[str] = mapped_column(String(10), server_default=text("'all'"))
    audience_phones: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    audience_tiers: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Bakery(Base):
    __tablename__ = "bakeries"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(120))
    address: Mapped[str] = mapped_column(String(255))
    metro: Mapped[str | None] = mapped_column(String(120), nullable=True)
    hours_text: Mapped[str | None] = mapped_column(String(120), nullable=True)
    latitude: Mapped[float]
    longitude: Mapped[float]
    sort_order: Mapped[int] = mapped_column(server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ReferralReward(Base):
    __tablename__ = "referral_rewards"

    id: Mapped[uuid.UUID] = _uuid_pk()
    referred_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    referrer_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    referrer_done: Mapped[bool] = mapped_column(server_default=false())
    referred_done: Mapped[bool] = mapped_column(server_default=false())
    referrer_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    referred_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    login: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(server_default=true())
    session_version: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
