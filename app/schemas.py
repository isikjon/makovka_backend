import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_PHONE_RE = re.compile(r"^\+7\d{10}$")


def normalize_phone(raw: str) -> str:
    """Collapses any of the app's input formats down to E.164 (+7XXXXXXXXXX)."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    normalized = f"+{digits}"
    if not _PHONE_RE.match(normalized):
        raise ValueError("Ожидается российский номер телефона")
    return normalized


class OtpRequestIn(BaseModel):
    phone: str

    @field_validator("phone")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return normalize_phone(v)


class OtpRequestOut(BaseModel):
    phone: str
    expires_in_seconds: int
    dev_code: str | None = None


class OtpVerifyIn(BaseModel):
    phone: str
    code: str

    @field_validator("phone")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return normalize_phone(v)


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    is_new_user: bool = False


class RefreshIn(BaseModel):
    refresh_token: str


class ProfileIn(BaseModel):
    name: str = ""
    surname: str = ""
    email: str = ""
    birthdate: str = ""
    gender: str = ""
    promo_code: str = ""


class IikoLinkOut(BaseModel):
    sync_status: str
    card_number: str | None = None
    last_error: str | None = None


class ProfileOut(BaseModel):
    phone: str
    name: str
    surname: str
    email: str
    birthdate: str
    gender: str
    promo_code: str
    iiko: IikoLinkOut | None = None


class TierOut(BaseModel):
    code: str
    name: str
    min_spend: float
    discount_percent: float
    cashback_percent: float
    perks: list[str]


class CoffeeOut(BaseModel):
    collected: int
    goal: int


class LoyaltyOut(BaseModel):
    mode: Literal["flat", "tiers"]
    sync_status: Literal["pending", "synced", "failed"] | None
    card_number: str | None
    current_tier: TierOut
    next_tier: TierOut | None
    total_spend: float
    remaining_to_next: float | None
    progress: float
    bonus_balance: float
    total_savings: float
    coffee: CoffeeOut | None
    tiers: list[TierOut]
    updated_at: datetime | None
    stale: bool


class PurchaseOut(BaseModel):
    id: str
    ordered_at: datetime
    order_number: int | None
    order_sum: float
    paid_sum: float
    discount_sum: float
    bonus_accrued: float
    bonus_spent: float
    bakery: str | None


class PurchaseHistoryOut(BaseModel):
    items: list[PurchaseOut]
    total: int
    updated_at: datetime | None


class NoveltyOut(BaseModel):
    id: str
    title: str
    image_url: str | None


class PromotionOut(BaseModel):
    id: str
    kind: Literal["generic", "coffee_stamps", "referral", "happy_hours", "combo"]
    title: str
    subtitle: str | None
    description: str | None
    badge: str | None
    price: float | None
    old_price: float | None
    time_text: str | None
    image_url: str | None
    show_on_home: bool
    personal: bool
    ends_at: datetime | None


class BakeryOut(BaseModel):
    id: str
    name: str
    address: str
    metro: str | None
    hours_text: str | None
    latitude: float
    longitude: float


class ContentOut(BaseModel):
    novelties: list[NoveltyOut]
    promotions: list[PromotionOut]
    bakeries: list[BakeryOut]


class ReferralOut(BaseModel):
    code: str
    invited_count: int
    rewarded_count: int
    bonus_per_friend: float
    rewards_enabled: bool
    share_text: str


class ReferralApplyIn(BaseModel):
    code: str = Field(max_length=64)


class ReferralApplyOut(BaseModel):
    applied: bool
