import re

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
