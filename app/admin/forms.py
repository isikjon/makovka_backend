from __future__ import annotations

import math
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from ..loyalty.rules import canonical_uuid
from ..models import PROMOTION_AUDIENCES, PROMOTION_KINDS
from ..schemas import normalize_phone

MOSCOW = timezone(timedelta(hours=3), "MSK")

PROMOTION_KIND_LABELS = {
    "generic": "Обычная акция",
    "coffee_stamps": "Кофейные штампы",
    "referral": "Приглашение друзей",
    "happy_hours": "Счастливые часы",
    "combo": "Комбо",
}
AUDIENCE_LABELS = {
    "all": "Всем гостям",
    "phones": "По номерам телефонов",
    "tiers": "По уровням лояльности",
}

REQUIRED = "Обязательное поле"
_PHONE_SEPARATORS = re.compile(r"[\n,;]+")
_MAX_MONEY = Decimal("9999999999.99")
_MAX_PERCENT = Decimal("100")
_MAX_AUDIENCE_PHONES = 5000
_MAX_PERKS = 20
_MAX_PERK_LENGTH = 200
_DATETIME_INPUT = "%Y-%m-%dT%H:%M"


class TierLike(Protocol):
    id: uuid.UUID
    min_spend: Decimal
    iiko_category_id: str | None
    is_active: bool


class FormSource(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def getlist(self, key: str) -> list[Any]: ...

    def keys(self) -> Iterable[str]: ...


class FormReader:
    def __init__(self, form: FormSource):
        self.form = form
        self.errors: dict[str, str] = {}

    def raw(self, name: str) -> str:
        value = self.form.get(name)
        return value.strip() if isinstance(value, str) else ""

    def text(self, name: str, max_length: int, *, required: bool = False) -> str | None:
        value = self.raw(name)
        if not value:
            if required:
                self.errors[name] = REQUIRED
            return None
        if len(value) > max_length:
            self.errors[name] = f"Не длиннее {max_length} символов"
        return value

    def checkbox(self, name: str) -> bool:
        return self.raw(name) in {"on", "1", "true"}

    def integer(self, name: str, minimum: int, maximum: int, default: int = 0) -> int:
        value = self.raw(name)
        if not value:
            return default
        try:
            number = int(value)
        except ValueError:
            self.errors[name] = "Ожидается целое число"
            return default
        if not minimum <= number <= maximum:
            self.errors[name] = f"Допустимо от {minimum} до {maximum}"
        return number

    def decimal(
        self, name: str, minimum: Decimal, maximum: Decimal, *, required: bool = False
    ) -> Decimal | None:
        value = self.raw(name).replace(",", ".").replace(" ", "")
        if not value:
            if required:
                self.errors[name] = REQUIRED
            return None
        try:
            number = Decimal(value)
        except InvalidOperation:
            self.errors[name] = "Ожидается число"
            return None
        if not number.is_finite():
            self.errors[name] = "Ожидается число"
            return None
        if not minimum <= number <= maximum:
            self.errors[name] = f"Допустимо от {minimum:f} до {maximum:f}"
        return number.quantize(Decimal("0.01"))

    def coordinate(self, name: str, limit: float) -> float | None:
        value = self.raw(name).replace(",", ".")
        if not value:
            self.errors[name] = REQUIRED
            return None
        try:
            number = float(value)
        except ValueError:
            self.errors[name] = "Ожидается число"
            return None
        if not math.isfinite(number) or not -limit <= number <= limit:
            self.errors[name] = f"Допустимо от {-limit:g} до {limit:g}"
            return None
        return number

    def choice(self, name: str, options: Sequence[str]) -> str:
        value = self.raw(name)
        if value not in options:
            self.errors[name] = "Выберите значение из списка"
            return options[0]
        return value

    def moscow_datetime(self, name: str) -> datetime | None:
        value = self.raw(name)
        if not value:
            return None
        try:
            parsed = datetime.strptime(value, _DATETIME_INPUT)
        except ValueError:
            self.errors[name] = "Ожидается дата и время"
            return None
        return parsed.replace(tzinfo=MOSCOW).astimezone(timezone.utc)

    def strings(self, name: str) -> list[str]:
        return [value.strip() for value in self.form.getlist(name) if isinstance(value, str) and value.strip()]


def parse_phone_list(raw: str) -> tuple[list[str], list[str]]:
    valid: dict[str, None] = {}
    invalid: list[str] = []
    for chunk in _PHONE_SEPARATORS.split(raw):
        entry = chunk.strip()
        if not entry:
            continue
        try:
            valid[normalize_phone(entry)] = None
        except ValueError:
            invalid.append(entry)
    return list(valid), invalid


def format_moscow(value: datetime | None) -> str:
    return value.astimezone(MOSCOW).strftime(_DATETIME_INPUT) if value else ""


def format_decimal(value: Decimal | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if value == value.to_integral_value() else f"{value:f}"


def form_snapshot(form: FormSource, multi: Iterable[str] = ()) -> dict[str, Any]:
    multi_keys = set(multi)
    snapshot: dict[str, Any] = {}
    for key in form.keys():
        if key in multi_keys:
            snapshot[key] = [value for value in form.getlist(key) if isinstance(value, str)]
        else:
            value = form.get(key)
            if isinstance(value, str):
                snapshot[key] = value
    for key in multi_keys:
        snapshot.setdefault(key, [])
    return snapshot


def parse_novelty(form: FormSource) -> tuple[dict[str, Any], dict[str, str]]:
    reader = FormReader(form)
    values = {
        "title": reader.text("title", 120, required=True),
        "sort_order": reader.integer("sort_order", -100000, 100000),
        "is_active": reader.checkbox("is_active"),
    }
    return values, reader.errors


def parse_bakery(form: FormSource) -> tuple[dict[str, Any], dict[str, str]]:
    reader = FormReader(form)
    values = {
        "name": reader.text("name", 120, required=True),
        "address": reader.text("address", 255, required=True),
        "metro": reader.text("metro", 120),
        "hours_text": reader.text("hours_text", 120),
        "latitude": reader.coordinate("latitude", 90),
        "longitude": reader.coordinate("longitude", 180),
        "sort_order": reader.integer("sort_order", -100000, 100000),
        "is_active": reader.checkbox("is_active"),
    }
    return values, reader.errors


def parse_promotion(
    form: FormSource, tier_codes: Sequence[str]
) -> tuple[dict[str, Any], dict[str, str]]:
    reader = FormReader(form)
    values: dict[str, Any] = {
        "kind": reader.choice("kind", PROMOTION_KINDS),
        "title": reader.text("title", 120, required=True),
        "subtitle": reader.text("subtitle", 200),
        "description": reader.text("description", 1000),
        "badge": reader.text("badge", 20),
        "price": reader.decimal("price", Decimal(0), _MAX_MONEY),
        "old_price": reader.decimal("old_price", Decimal(0), _MAX_MONEY),
        "time_text": reader.text("time_text", 60),
        "starts_at": reader.moscow_datetime("starts_at"),
        "ends_at": reader.moscow_datetime("ends_at"),
        "show_on_home": reader.checkbox("show_on_home"),
        "sort_order": reader.integer("sort_order", -100000, 100000),
        "is_active": reader.checkbox("is_active"),
        "audience": reader.choice("audience", PROMOTION_AUDIENCES),
        "audience_phones": [],
        "audience_tiers": [],
    }
    starts_at, ends_at = values["starts_at"], values["ends_at"]
    if starts_at and ends_at and ends_at <= starts_at:
        reader.errors["ends_at"] = "Окончание должно быть позже начала"

    if values["audience"] == "phones":
        phones, invalid = parse_phone_list(str(form.get("audience_phones") or ""))
        if invalid:
            reader.errors["audience_phones"] = "Неверные номера: " + ", ".join(invalid[:10]) + (
                f" и ещё {len(invalid) - 10}" if len(invalid) > 10 else ""
            )
        elif not phones:
            reader.errors["audience_phones"] = "Добавьте хотя бы один номер"
        elif len(phones) > _MAX_AUDIENCE_PHONES:
            reader.errors["audience_phones"] = f"Не больше {_MAX_AUDIENCE_PHONES} номеров"
        values["audience_phones"] = phones
    elif values["audience"] == "tiers":
        selected = list(dict.fromkeys(reader.strings("audience_tiers")))
        unknown = [code for code in selected if code not in tier_codes]
        if unknown:
            reader.errors["audience_tiers"] = "Неизвестные уровни: " + ", ".join(unknown)
        elif not selected:
            reader.errors["audience_tiers"] = "Выберите хотя бы один уровень"
        values["audience_tiers"] = selected
    return values, reader.errors


def parse_perks(raw: str) -> tuple[list[str], str | None]:
    perks = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(perks) > _MAX_PERKS:
        return perks, f"Не больше {_MAX_PERKS} привилегий"
    if any(len(perk) > _MAX_PERK_LENGTH for perk in perks):
        return perks, f"Каждая привилегия не длиннее {_MAX_PERK_LENGTH} символов"
    return perks, None


def parse_tier(form: FormSource) -> tuple[dict[str, Any], dict[str, str]]:
    reader = FormReader(form)
    values: dict[str, Any] = {
        "name": reader.text("name", 100, required=True),
        "min_spend": reader.decimal("min_spend", Decimal(0), _MAX_MONEY, required=True),
        "discount_percent": reader.decimal("discount_percent", Decimal(0), _MAX_PERCENT, required=True),
        "cashback_percent": reader.decimal("cashback_percent", Decimal(0), _MAX_PERCENT, required=True),
        "is_active": reader.checkbox("is_active"),
    }
    perks, perks_error = parse_perks(str(form.get("perks") or ""))
    if perks_error:
        reader.errors["perks"] = perks_error
    values["perks"] = perks
    category = reader.raw("iiko_category_manual") or reader.raw("iiko_category_id")
    values["iiko_category_id"] = canonical_uuid(category) if category else None
    if category and values["iiko_category_id"] is None:
        reader.errors["iiko_category_manual"] = "Идентификатор категории должен быть UUID из iiko"
    return values, reader.errors


def tier_conflicts(tiers: Iterable[TierLike], tier: TierLike, values: Mapping[str, Any]) -> dict[str, str]:
    others = [other for other in tiers if other.id != tier.id]
    errors: dict[str, str] = {}
    category = values["iiko_category_id"]
    if category and any(canonical_uuid(other.iiko_category_id or "") == category for other in others):
        errors["iiko_category_manual"] = "Эта категория уже назначена другому уровню"
    if values["is_active"] and any(
        other.is_active and other.min_spend == values["min_spend"] for other in others
    ):
        errors["min_spend"] = "У другого активного уровня такая же сумма покупок — один из них будет недостижим"
    return errors


def model_snapshot(fields: Mapping[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, bool):
            snapshot[key] = "on" if value else ""
        elif isinstance(value, Decimal):
            snapshot[key] = format_decimal(value)
        elif isinstance(value, datetime):
            snapshot[key] = format_moscow(value)
        elif value is None:
            snapshot[key] = ""
        elif isinstance(value, list):
            snapshot[key] = list(value)
        else:
            snapshot[key] = str(value)
    return snapshot
