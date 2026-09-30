import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator

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
