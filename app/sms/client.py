from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from ..config import Settings

_CALL_CODE_RE = re.compile(r"[0-9]{1,4}")


class SmsSendError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SmsCallResult:
    code: str
    call_id: str | None
    status_code: int | None


class SmsRuClient:
    _BASE_URL = "https://sms.ru"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._http = httpx.AsyncClient(base_url=self._BASE_URL, timeout=15.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call_otp(self, phone: str, ip: str) -> SmsCallResult:
        params = {
            "phone": phone.lstrip("+"),
            "ip": ip,
            "api_id": self._settings.sms_ru_api_id,
        }
        data = await self._get_json("/code/call", params)

        if data.get("status") != "OK":
            raise SmsSendError(
                data.get("status_text", "unknown sms.ru call error"),
                status_code=data.get("status_code"),
            )

        return SmsCallResult(
            code=_normalize_call_code(data.get("code"), sorted(data)),
            call_id=_optional_text(data.get("call_id")),
            status_code=_optional_int(data.get("status_code")),
        )

    async def _get_json(self, path: str, params: dict) -> dict:
        try:
            resp = await self._http.get(path, params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise SmsSendError(f"sms.ru request failed: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise SmsSendError("sms.ru returned a non-JSON response") from exc
        if not isinstance(data, dict):
            raise SmsSendError("sms.ru returned an unexpected response")
        return data


def _normalize_call_code(code: object, fields: list[str]) -> str:
    if isinstance(code, int) and not isinstance(code, bool):
        text = str(code)
    elif isinstance(code, str):
        text = code.strip()
    else:
        text = ""
    if not _CALL_CODE_RE.fullmatch(text):
        raise SmsSendError(
            f"sms.ru returned an invalid call code ({type(code).__name__}; fields: {', '.join(fields)})"
        )
    return text.zfill(4)


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
