from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, TypeVar

_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_CASH_REGISTER_SUFFIX = re.compile(r"\s*\(\s*касса[^)]*\)\s*$", re.IGNORECASE)
_UUID_INPUT = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")

_CLOSE_ORDER = "CloseOrder"
_DISCOUNT = "DiscountSum"
_PAY_FROM_WALLET = "PayFromWallet"
_CANCEL_PAY_FROM_WALLET = "CancelPayFromWallet"
_REFILL_PREFIX = "Refill"
_CANCEL_REFILL_PREFIX = "CancelRefill"


class SupportsMinSpend(Protocol):
    min_spend: Decimal


class SupportsTierCode(SupportsMinSpend, Protocol):
    code: str


TierT = TypeVar("TierT", bound=SupportsMinSpend)


def canonical_uuid(raw: str) -> str | None:
    value = raw.strip()
    return str(uuid.UUID(value)) if _UUID_INPUT.fullmatch(value) else None


def to_money(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        return _ZERO
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return _ZERO
    if not amount.is_finite():
        return _ZERO
    return amount.quantize(_CENT)


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_iiko_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_iiko_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.000")


def resolve_tier(
    tiers: Iterable[TierT], spend: Decimal
) -> tuple[TierT | None, TierT | None, Decimal | None, float]:
    current: TierT | None = None
    upcoming: TierT | None = None
    for tier in sorted(tiers, key=lambda item: item.min_spend):
        if tier.min_spend <= spend:
            current = tier
        else:
            upcoming = tier
            break
    if upcoming is None:
        return current, None, None, 1.0
    floor = current.min_spend if current is not None else Decimal(0)
    span = upcoming.min_spend - floor
    progress = float((spend - floor) / span) if span > 0 else 1.0
    return current, upcoming, upcoming.min_spend - spend, min(max(progress, 0.0), 1.0)


def tier_spend_ranges(
    tiers: Iterable[SupportsTierCode], codes: Iterable[str]
) -> list[tuple[Decimal, Decimal | None]]:
    ordered = sorted(tiers, key=lambda item: item.min_spend)
    wanted = set(codes)
    return [
        (tier.min_spend, ordered[index + 1].min_spend if index + 1 < len(ordered) else None)
        for index, tier in enumerate(ordered)
        if tier.code in wanted
    ]


def bakery_name(terminal_group_name: str) -> str:
    return _CASH_REGISTER_SUFFIX.sub("", terminal_group_name).strip()


def parse_terminal_groups(response: Mapping[str, Any]) -> dict[str, str]:
    bakeries: dict[str, str] = {}
    for key in ("terminalGroups", "terminalGroupsInSleep"):
        for group in response.get(key) or []:
            if not isinstance(group, Mapping):
                continue
            for item in group.get("items") or []:
                if isinstance(item, Mapping) and item.get("id"):
                    bakeries[str(item["id"])] = bakery_name(str(item.get("name") or ""))
    return bakeries


def _is_bonus_wallet_row(row: Mapping[str, Any], bonus_wallet_id: str | None) -> bool:
    if not bonus_wallet_id:
        return False
    wallet_id = row.get("walletId")
    return not wallet_id or str(wallet_id).lower() == bonus_wallet_id.lower()


def _build_purchase(
    pos_order_id: str,
    rows: Sequence[Mapping[str, Any]],
    bakery_map: Mapping[str, str],
    bonus_wallet_id: str | None,
) -> dict[str, Any] | None:
    closes = [row for row in rows if row.get("typeName") == _CLOSE_ORDER]
    if not closes:
        return None
    close = max(closes, key=lambda row: parse_iiko_datetime(row.get("whenCreated")) or _EPOCH)
    ordered_at = parse_iiko_datetime(close.get("whenCreatedOrder")) or parse_iiko_datetime(
        close.get("whenCreated")
    )
    if ordered_at is None:
        return None

    discount = accrued = spent = _ZERO
    for row in rows:
        kind = str(row.get("typeName") or "")
        amount = to_money(row.get("sum"))
        if kind == _DISCOUNT:
            discount += amount
        elif not _is_bonus_wallet_row(row, bonus_wallet_id):
            continue
        elif kind.startswith(_CANCEL_REFILL_PREFIX):
            accrued -= abs(amount)
        elif kind.startswith(_REFILL_PREFIX):
            accrued += abs(amount)
        elif kind == _PAY_FROM_WALLET:
            spent += abs(amount)
        elif kind == _CANCEL_PAY_FROM_WALLET:
            spent -= abs(amount)

    terminal_group_id = next(
        (str(row["terminalGroupId"]) for row in (close, *rows) if row.get("terminalGroupId")), None
    )
    return {
        "pos_order_id": pos_order_id,
        "order_number": _to_int(close.get("orderNumber")),
        "ordered_at": ordered_at,
        "order_sum": to_money(close.get("orderSum")),
        "paid_sum": to_money(close.get("sum")),
        "discount_sum": discount,
        "bonus_accrued": max(accrued, _ZERO),
        "bonus_spent": max(spent, _ZERO),
        "terminal_group_id": terminal_group_id,
        "bakery": (bakery_map.get(terminal_group_id) or None) if terminal_group_id else None,
    }


def _group_by_order(transactions: Iterable[Any]) -> dict[str, list[Mapping[str, Any]]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    seen_ids: set[str] = set()
    for row in transactions:
        if not isinstance(row, Mapping) or row.get("isIgnored"):
            continue
        row_id = row.get("id")
        if row_id:
            if str(row_id) in seen_ids:
                continue
            seen_ids.add(str(row_id))
        pos_order_id = row.get("posOrderId")
        if not pos_order_id:
            continue
        groups.setdefault(str(pos_order_id), []).append(row)
    return groups


def build_purchases(
    transactions: Iterable[Any],
    bakery_map: Mapping[str, str],
    *,
    bonus_wallet_id: str | None = None,
) -> list[dict[str, Any]]:
    purchases = [
        purchase
        for pos_order_id, rows in _group_by_order(transactions).items()
        if (purchase := _build_purchase(pos_order_id, rows, bakery_map, bonus_wallet_id)) is not None
    ]
    purchases.sort(key=lambda purchase: purchase["ordered_at"])
    return purchases


def orphan_order_ids(transactions: Iterable[Any]) -> list[str]:
    return [
        pos_order_id
        for pos_order_id, rows in _group_by_order(transactions).items()
        if not any(row.get("typeName") == _CLOSE_ORDER for row in rows)
    ]


def extract_category_ids(customer: Mapping[str, Any]) -> list[str]:
    return [
        str(category["id"])
        for category in customer.get("categories") or []
        if isinstance(category, Mapping) and category.get("id")
    ]


def plan_category_changes(
    target_category_id: str | None,
    guest_category_ids: Iterable[str],
    all_tier_category_ids: Iterable[str | None],
) -> tuple[list[str], list[str]]:
    target = target_category_id.lower() if target_category_id else None
    tier_ids = {category_id.lower() for category_id in all_tier_category_ids if category_id}
    guest = list(dict.fromkeys(category_id for category_id in guest_category_ids if category_id))
    guest_normalized = {category_id.lower() for category_id in guest}

    to_add = [target_category_id] if target and target not in guest_normalized else []
    to_remove = [
        category_id
        for category_id in guest
        if category_id.lower() in tier_ids and category_id.lower() != target
    ]
    return to_add, to_remove


def parse_counters(response: Mapping[str, Any], customer_id: str) -> Decimal:
    wanted = customer_id.lower()
    for counter in response.get("counters") or []:
        if isinstance(counter, Mapping) and str(counter.get("guestId") or "").lower() == wanted:
            return to_money(counter.get("value"))
    return _ZERO


def pick_wallet(
    wallet_balances: Iterable[Any] | None,
    wallet_id: str,
    type_fallback: int | None = 1,
    *,
    exclude_ids: Iterable[str] = (),
) -> Mapping[str, Any] | None:
    wallets = [wallet for wallet in wallet_balances or [] if isinstance(wallet, Mapping)]
    if wallet_id:
        wanted = wallet_id.lower()
        return next((wallet for wallet in wallets if str(wallet.get("id") or "").lower() == wanted), None)
    if type_fallback is None:
        return None
    excluded = {excluded_id.lower() for excluded_id in exclude_ids if excluded_id}
    return next(
        (
            wallet
            for wallet in wallets
            if str(wallet.get("type")) == str(type_fallback)
            and str(wallet.get("id") or "").lower() not in excluded
        ),
        None,
    )


def resolve_bonus_wallet_id(
    wallet_balances: Iterable[Any] | None, configured_id: str, coffee_wallet_ids: Iterable[str]
) -> str | None:
    if configured_id:
        return configured_id
    wallet = pick_wallet(wallet_balances, "", exclude_ids=coffee_wallet_ids)
    return str(wallet["id"]) if wallet and wallet.get("id") else None


def wallet_balance(wallet: Mapping[str, Any] | None) -> Decimal:
    return to_money(wallet.get("balance")) if wallet else _ZERO


def pick_wallet_balance(
    wallet_balances: Iterable[Any] | None, wallet_id: str, type_fallback: int | None = 1
) -> Decimal:
    return wallet_balance(pick_wallet(wallet_balances, wallet_id, type_fallback))


def count_coffee_stamps(wallet_balances: Iterable[Any] | None, wallet_id: str, goal: int) -> int | None:
    if not wallet_id:
        return None
    balance = pick_wallet_balance(wallet_balances, wallet_id, type_fallback=None)
    return max(0, min(int(balance), goal))
