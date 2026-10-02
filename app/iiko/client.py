from __future__ import annotations

import asyncio
import copy
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..loyalty.rules import format_iiko_datetime, parse_counters, parse_terminal_groups
from ..models import IikoApiCall

_SENSITIVE_KEYS = {"phone", "cardnumber", "cardtrack", "card", "track", "number", "apilogin"}
_RETRY_DELAYS_SECONDS = (5.0, 15.0, 30.0)
_TRANSIENT_STATUS_CODES = {502, 503, 504}
_TRANSACTIONS_PAGE_SIZE = 100
_TRANSACTIONS_MAX_PAGES = 50
_TERMINAL_GROUPS_TTL_SECONDS = 24 * 60 * 60


def _mask(value: Any) -> Any:
    if value is None:
        return None
    clone = copy.deepcopy(value)
    _mask_in_place(clone)
    return clone


def _mask_in_place(node: Any) -> None:
    if isinstance(node, dict):
        for key, val in node.items():
            if key.lower() in _SENSITIVE_KEYS and isinstance(val, str):
                node[key] = _mask_str(val)
            else:
                _mask_in_place(val)
    elif isinstance(node, list):
        for item in node:
            _mask_in_place(item)


def _mask_str(s: str) -> str:
    return f"***{s[-4:]}" if len(s) > 4 else "***"


def generate_card_number() -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(16))


class IikoClientError(Exception):
    def __init__(self, message: str, status_code: int | None = None, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class IikoMutationBlocked(IikoClientError):
    pass


def _is_retryable(exc: IikoClientError, mutating: bool) -> bool:
    if exc.status_code == 429:
        return True
    if exc.status_code is not None:
        return exc.status_code in _TRANSIENT_STATUS_CODES and not mutating
    if isinstance(exc.__cause__, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return True
    return isinstance(exc.__cause__, httpx.TransportError) and not mutating


class IikoCloudClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._http = httpx.AsyncClient(base_url=settings.iiko_base_url, timeout=15.0)
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self._token_lock = asyncio.Lock()
        self._pace_lock = asyncio.Lock()
        self._last_request_at: float | None = None
        self._retry_delays: tuple[float, ...] = _RETRY_DELAYS_SECONDS
        self._terminal_groups: dict[str, str] | None = None
        self._terminal_groups_expires_at = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _pace(self) -> None:
        async with self._pace_lock:
            if self._last_request_at is not None:
                interval = self._settings.iiko_min_request_interval_ms / 1000
                wait = self._last_request_at + interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    async def _get_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and self._token_expires_at and now < self._token_expires_at:
            return self._token
        async with self._token_lock:
            now = datetime.now(timezone.utc)
            if self._token and self._token_expires_at and now < self._token_expires_at:
                return self._token
            await self._pace()
            try:
                resp = await self._http.post(
                    "/api/1/access_token", json={"apiLogin": self._settings.iiko_api_login}
                )
                resp.raise_for_status()
                token = resp.json()["token"]
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                raise IikoClientError(f"iiko access_token responded {status_code}", status_code) from exc
            except httpx.HTTPError as exc:
                raise IikoClientError(f"iiko access_token request failed: {exc}") from exc
            except (ValueError, KeyError) as exc:
                raise IikoClientError("iiko access_token returned an unexpected body") from exc
            self._token = token
            self._token_expires_at = now + timedelta(minutes=50)
            return self._token

    def guard_mutation(self, guard_phone: str | None, payload: dict | None = None) -> None:
        if not self._settings.iiko_mutations_enabled:
            raise IikoMutationBlocked("iiko mutations disabled via kill switch")
        if self._settings.iiko_env == "staging":
            test_phone = self._settings.iiko_test_guest_phone
            if not test_phone:
                raise IikoMutationBlocked("staging environment has no test guest configured")
            phones = {phone for phone in (guard_phone, _find_phone(payload or {})) if phone}
            if not phones:
                raise IikoMutationBlocked("staging mutation has no guest phone to check")
            for phone in phones:
                if phone != test_phone:
                    raise IikoMutationBlocked(f"staging only allows mutations for {test_phone}, got {phone!r}")

    async def _post(
        self,
        db: AsyncSession | None,
        endpoint: str,
        payload: dict,
        *,
        mutating: bool,
        guard_phone: str | None = None,
    ) -> dict:
        if mutating:
            self.guard_mutation(guard_phone, payload)

        delays = iter(self._retry_delays)
        while True:
            try:
                return await self._send(db, endpoint, payload)
            except IikoClientError as exc:
                delay = next(delays, None) if _is_retryable(exc, mutating) else None
                if delay is None:
                    raise
            await asyncio.sleep(delay)

    async def _send(self, db: AsyncSession | None, endpoint: str, payload: dict) -> dict:
        token = await self._get_token()
        await self._pace()
        correlation_id = str(uuid.uuid4())
        status_code: int | None = None
        response_body: dict | None = None
        ok = False
        try:
            resp = await self._http.post(
                endpoint, json=payload, headers={"Authorization": f"Bearer {token}"}
            )
            status_code = resp.status_code
            try:
                response_body = resp.json()
            except ValueError:
                response_body = {"raw": resp.text[:2000]}
            resp.raise_for_status()
            ok = True
            return response_body
        except httpx.HTTPStatusError as exc:
            raise IikoClientError(
                f"iiko {endpoint} responded {status_code}", status_code, response_body
            ) from exc
        except httpx.HTTPError as exc:
            raise IikoClientError(f"iiko {endpoint} request failed: {exc}") from exc
        finally:
            if db is not None:
                db.add(
                    IikoApiCall(
                        correlation_id=correlation_id,
                        endpoint=endpoint,
                        request_body=_mask(payload),
                        response_body=_mask(response_body),
                        status_code=status_code,
                        ok=ok,
                    )
                )
                await db.commit()

    async def list_organizations(self, db: AsyncSession | None = None) -> list[dict]:
        data = await self._post(db, "/api/1/organizations", {}, mutating=False)
        return data.get("organizations", [])

    async def list_loyalty_programs(self, db: AsyncSession | None = None) -> list[dict]:
        data = await self._post(
            db,
            "/api/1/loyalty/iiko/program",
            {"organizationId": self._settings.iiko_organization_id},
            mutating=False,
        )
        return data.get("Programs", data.get("programs", []))

    async def find_guest_by_phone(self, phone: str, db: AsyncSession | None = None) -> dict | None:
        payload = {
            "organizationId": self._settings.iiko_organization_id,
            "type": "phone",
            "phone": phone,
        }
        try:
            return await self._post(db, "/api/1/loyalty/iiko/customer/info", payload, mutating=False)
        except IikoClientError as exc:
            body = exc.response_body or {}
            if body.get("errorCode") == "Validation_IncorrectPhone":
                return None
            raise

    async def create_or_update_guest(
        self,
        *,
        phone: str,
        name: str = "",
        surname: str = "",
        birthdate: str = "",
        db: AsyncSession | None = None,
    ) -> dict:
        payload = {
            "organizationId": self._settings.iiko_organization_id,
            "phone": phone,
            "name": name,
            "surname": surname,
        }
        if birthdate:
            payload["birthday"] = birthdate
        return await self._post(
            db, "/api/1/loyalty/iiko/customer/create_or_update", payload, mutating=True
        )

    async def add_card(
        self, *, customer_id: str, card_number: str, phone: str, db: AsyncSession | None = None
    ) -> dict:
        payload = {
            "organizationId": self._settings.iiko_organization_id,
            "customerId": customer_id,
            "cardTrack": card_number,
            "phone": phone,
        }
        return await self._post(db, "/api/1/loyalty/iiko/customer/card/add", payload, mutating=True)

    async def get_customer_by_id(self, customer_id: str, db: AsyncSession | None = None) -> dict:
        payload = {
            "organizationId": self._settings.iiko_organization_id,
            "type": "id",
            "id": customer_id,
        }
        return await self._post(db, "/api/1/loyalty/iiko/customer/info", payload, mutating=False)

    async def list_customer_categories(self, db: AsyncSession | None = None) -> list[dict]:
        data = await self._post(
            db,
            "/api/1/loyalty/iiko/customer_category",
            {"organizationId": self._settings.iiko_organization_id},
            mutating=False,
        )
        return data.get("guestCategories", [])

    async def add_customer_category(
        self, *, customer_id: str, category_id: str, guard_phone: str, db: AsyncSession | None = None
    ) -> dict:
        return await self._change_customer_category(
            "/api/1/loyalty/iiko/customer_category/add", customer_id, category_id, guard_phone, db
        )

    async def remove_customer_category(
        self, *, customer_id: str, category_id: str, guard_phone: str, db: AsyncSession | None = None
    ) -> dict:
        return await self._change_customer_category(
            "/api/1/loyalty/iiko/customer_category/remove", customer_id, category_id, guard_phone, db
        )

    async def _change_customer_category(
        self,
        endpoint: str,
        customer_id: str,
        category_id: str,
        guard_phone: str,
        db: AsyncSession | None,
    ) -> dict:
        payload = {
            "organizationId": self._settings.iiko_organization_id,
            "customerId": customer_id,
            "categoryId": category_id,
        }
        return await self._post(db, endpoint, payload, mutating=True, guard_phone=guard_phone)

    async def topup_wallet(
        self,
        *,
        customer_id: str,
        wallet_id: str,
        amount: Decimal,
        comment: str,
        guard_phone: str,
        db: AsyncSession | None = None,
    ) -> dict:
        payload = {
            "organizationId": self._settings.iiko_organization_id,
            "customerId": customer_id,
            "walletId": wallet_id,
            "sum": float(amount),
            "comment": comment,
        }
        return await self._post(
            db, "/api/1/loyalty/iiko/customer/wallet/topup", payload, mutating=True, guard_phone=guard_phone
        )

    async def get_orders_sum_all_time(self, customer_id: str, db: AsyncSession | None = None) -> Decimal:
        payload = {
            "organizationId": self._settings.iiko_organization_id,
            "guestIds": [customer_id],
            "periods": ["AllTime"],
            "metrics": ["OrdersSum"],
        }
        data = await self._post(db, "/api/1/loyalty/iiko/get_counters", payload, mutating=False)
        return parse_counters(data, customer_id)

    async def get_transactions(
        self,
        customer_id: str,
        date_from: datetime,
        date_to: datetime,
        db: AsyncSession | None = None,
    ) -> list[dict]:
        transactions: list[dict] = []
        for page_number in range(_TRANSACTIONS_MAX_PAGES):
            payload = {
                "organizationId": self._settings.iiko_organization_id,
                "customerId": customer_id,
                "dateFrom": format_iiko_datetime(date_from),
                "dateTo": format_iiko_datetime(date_to),
                "pageNumber": page_number,
                "pageSize": _TRANSACTIONS_PAGE_SIZE,
            }
            data = await self._post(
                db, "/api/1/loyalty/iiko/customer/transactions/by_date", payload, mutating=False
            )
            page = data.get("transactions") or []
            transactions.extend(page)
            if len(page) < _TRANSACTIONS_PAGE_SIZE:
                break
        return transactions

    async def list_terminal_groups(self, db: AsyncSession | None = None) -> dict[str, str]:
        if self._terminal_groups is not None and time.monotonic() < self._terminal_groups_expires_at:
            return self._terminal_groups
        payload = {
            "organizationIds": [self._settings.iiko_organization_id],
            "includeDisabled": True,
        }
        data = await self._post(db, "/api/1/terminal_groups", payload, mutating=False)
        self._terminal_groups = parse_terminal_groups(data)
        self._terminal_groups_expires_at = time.monotonic() + _TERMINAL_GROUPS_TTL_SECONDS
        return self._terminal_groups


def _find_phone(payload: dict) -> str | None:
    for key in ("phone", "Phone"):
        if key in payload and isinstance(payload[key], str):
            return payload[key]
    customer = payload.get("customer")
    if isinstance(customer, dict):
        return _find_phone(customer)
    return None
