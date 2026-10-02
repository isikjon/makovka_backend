from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import jinja2
from fastapi.templating import Jinja2Templates

from .forms import MOSCOW, format_decimal

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def format_msk(value: datetime | None) -> str:
    return value.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M") if value else "—"


def format_money(value: Decimal | float | int | None) -> str:
    amount = Decimal(str(value or 0))
    text = f"{amount:,.0f}" if amount == amount.to_integral_value() else f"{amount:,.2f}"
    return text.replace(",", "\u00a0").replace(".", ",") + "\u00a0₽"


def format_percent(value: Decimal | None) -> str:
    return f"{format_decimal(value) or '0'}%"


def setting_input(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def setting_value(value: Any) -> str:
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if value == "":
        return "пусто"
    return setting_input(value)


_environment = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
    autoescape=True,
    undefined=jinja2.StrictUndefined,
)
_environment.filters.update(
    msk=format_msk,
    money=format_money,
    percent=format_percent,
    setting_input=setting_input,
    setting_value=setting_value,
)
templates = Jinja2Templates(env=_environment)
