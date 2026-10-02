from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .db import async_session_factory
from .loyalty.rules import canonical_uuid
from .models import AppSetting

logger = logging.getLogger("app.runtime_settings")

CACHE_TTL_SECONDS = 15.0

_TRUE_TOKENS = {"1", "true", "on", "yes", "да"}
_FALSE_TOKENS = {"", "0", "false", "off", "no", "нет"}


class SettingValueError(ValueError):
    pass


@dataclass(frozen=True)
class OverridableSetting:
    key: str
    label: str
    kind: Literal["bool", "int", "float", "wallet"]
    minimum: float | None = None
    maximum: float | None = None
    hint: str = ""


OVERRIDABLE_SETTINGS: tuple[OverridableSetting, ...] = (
    OverridableSetting(
        "loyalty_tiers_enabled",
        "Уровни лояльности",
        "bool",
        hint="Выключено — у всех гостей единая скидка, категории уровней снимаются с гостей в iiko "
        "при следующей синхронизации",
    ),
    OverridableSetting(
        "loyalty_tier_sync_enabled",
        "Синхронизация категорий уровней в iiko",
        "bool",
        hint="Работает только вместе с включёнными уровнями. Выключено — выданные категории уровней "
        "снимаются с гостей",
    ),
    OverridableSetting("loyalty_flat_discount_percent", "Единая скидка, %", "float", 0, 100),
    OverridableSetting(
        "coffee_stamp_wallet_id",
        "ID кошелька кофейных штампов в iiko",
        "wallet",
        hint="Пусто — карточка кофе показывается без счётчика",
    ),
    OverridableSetting("coffee_stamp_goal", "Сколько чашек нужно собрать", "int", 1, 50),
    OverridableSetting("referral_rewards_enabled", "Начислять бонусы за приглашённых друзей", "bool"),
    OverridableSetting("referral_bonus_amount", "Бонусов за каждого друга", "float", 0, 100000),
)

OVERRIDABLE_KEYS = frozenset(setting.key for setting in OVERRIDABLE_SETTINGS)
_BY_KEY = {setting.key: setting for setting in OVERRIDABLE_SETTINGS}


def _parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    raise SettingValueError("Ожидается «да» или «нет»")


def _parse_number(raw: Any, setting: OverridableSetting) -> float:
    if isinstance(raw, bool):
        raise SettingValueError("Ожидается число")
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str) and raw.strip():
        try:
            value = float(raw.strip().replace(",", "."))
        except ValueError as exc:
            raise SettingValueError("Ожидается число") from exc
    else:
        raise SettingValueError("Ожидается число")
    if not math.isfinite(value):
        raise SettingValueError("Ожидается число")
    if setting.minimum is not None and value < setting.minimum:
        raise SettingValueError(f"Значение должно быть не меньше {setting.minimum:g}")
    if setting.maximum is not None and value > setting.maximum:
        raise SettingValueError(f"Значение должно быть не больше {setting.maximum:g}")
    return value


def _parse_int(raw: Any, setting: OverridableSetting) -> int:
    value = _parse_number(raw, setting)
    if not value.is_integer():
        raise SettingValueError("Ожидается целое число")
    return int(value)


def _parse_wallet(raw: Any) -> str:
    if not isinstance(raw, str):
        raise SettingValueError("Ожидается идентификатор кошелька")
    if not raw.strip():
        return ""
    value = canonical_uuid(raw)
    if value is None:
        raise SettingValueError("Идентификатор кошелька должен быть UUID из iiko или пустым")
    return value


def validate_setting(key: str, raw: Any) -> Any:
    setting = _BY_KEY.get(key)
    if setting is None:
        raise SettingValueError("Эту настройку нельзя изменить из админки")
    if setting.kind == "bool":
        return _parse_bool(raw)
    if setting.kind == "int":
        return _parse_int(raw, setting)
    if setting.kind == "float":
        return round(_parse_number(raw, setting), 2)
    return _parse_wallet(raw)


def validate_override(base: Settings, key: str, raw: Any) -> Any:
    value = validate_setting(key, raw)
    if key == "coffee_stamp_wallet_id" and value and value == canonical_uuid(base.loyalty_bonus_wallet_id):
        raise SettingValueError(
            "Это бонусный кошелёк (LOYALTY_BONUS_WALLET_ID), укажите кошелёк кофейных штампов"
        )
    return value


def apply_overrides(base: Settings, overrides: Mapping[str, Any]) -> Settings:
    update: dict[str, Any] = {}
    for key, raw in overrides.items():
        try:
            update[key] = validate_setting(key, raw)
        except SettingValueError:
            logger.warning("ignoring invalid runtime setting %s", key)
    return base.model_copy(update=update) if update else base


class OverrideCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.values: dict[str, Any] | None = None
        self.expires_at = 0.0
        self.generation = 0


_cache = OverrideCache()


def invalidate_settings_cache() -> None:
    _cache.values = None
    _cache.expires_at = 0.0
    _cache.generation += 1


async def fetch_overrides() -> dict[str, Any]:
    async with async_session_factory() as db:
        rows = await db.execute(select(AppSetting.key, AppSetting.value))
        return {key: value for key, value in rows}


async def load_overrides() -> dict[str, Any]:
    if _cache.values is not None and _cache.clock() < _cache.expires_at:
        return _cache.values
    generation = _cache.generation
    values = await fetch_overrides()
    if generation == _cache.generation:
        _cache.values = values
        _cache.expires_at = _cache.clock() + CACHE_TTL_SECONDS
    return values


async def get_effective_settings() -> Settings:
    return apply_overrides(get_settings(), await load_overrides())


async def load_override_rows(db: AsyncSession) -> dict[str, AppSetting]:
    rows = await db.scalars(select(AppSetting).where(AppSetting.key.in_(OVERRIDABLE_KEYS)))
    return {row.key: row for row in rows}


async def save_overrides(
    db: AsyncSession, updates: Mapping[str, Any], removals: Iterable[str], updated_by: str
) -> None:
    for key, value in updates.items():
        stmt = pg_insert(AppSetting).values(key=key, value=validate_setting(key, value), updated_by=updated_by)
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=[AppSetting.key],
                set_={"value": stmt.excluded.value, "updated_by": stmt.excluded.updated_by, "updated_at": func.now()},
            )
        )
    removed = [key for key in removals if key in OVERRIDABLE_KEYS]
    if removed:
        await db.execute(delete(AppSetting).where(AppSetting.key.in_(removed)))
    await db.commit()
    invalidate_settings_cache()
