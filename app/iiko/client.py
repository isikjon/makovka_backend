from __future__ import annotations

import asyncio
import copy
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models import IikoApiCall

_SENSITIVE_KEYS = {"phone", "cardnumber", "cardtrack", "card", "track", "apilogin"}


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


class IikoCloudClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._http = httpx.AsyncClient(base_url=settings.iiko_base_url, timeout=15.0)
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and self._token_expires_at and now < self._token_expires_at:
            return self._token
        async with self._token_lock:
            now = datetime.now(timezone.utc)
            if self._token and self._token_expires_at and now < self._token_expires_at:
                return self._token
            resp = await self._http.post(
                "/api/1/access_token", json={"apiLogin": self._settings.iiko_api_login}
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["token"]
            self._token_expires_at = now + timedelta(minutes=50)
            return self._token

    def _guard_mutation(self, payload: dict) -> None:
        if not self._settings.iiko_mutations_enabled:
            raise IikoMutationBlocked("iiko mutations disabled via kill switch")
        if self._settings.iiko_env == "staging":
            phone = _find_phone(payload)
            test_phone = self._settings.iiko_test_guest_phone
            if not test_phone:
                raise IikoMutationBlocked("staging environment has no test guest configured")
            if phone and phone != test_phone:
                raise IikoMutationBlocked(f"staging only allows mutations for {test_phone}, got {phone!r}")

    async def _post(
        self, db: AsyncSession | None, endpoint: str, payload: dict, *, mutating: bool
    ) -> dict:
        if mutating:
            self._guard_mutation(payload)

        token = await self._get_token()
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


def _find_phone(payload: dict) -> str | None:
    for key in ("phone", "Phone"):
        if key in payload and isinstance(payload[key], str):
            return payload[key]
    customer = payload.get("customer")
    if isinstance(customer, dict):
        return _find_phone(customer)
    return None
