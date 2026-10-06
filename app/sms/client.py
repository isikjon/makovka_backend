from __future__ import annotations

import re

import httpx

from ..config import Settings

_CALL_CODE_RE = re.compile(r"[0-9]{1,4}")


class SmsSendError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SmsRuClient:
    _BASE_URL = "https://sms.ru"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._http = httpx.AsyncClient(base_url=self._BASE_URL, timeout=15.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send_otp(self, phone: str, code: str) -> None:
        text = f"Код для входа в приложение Пекарня: {code}"
        params = {
            "api_id": self._settings.sms_ru_api_id,
            "to": phone,
            "msg": text,
            "json": 1,
        }
        data = await self._get_json("/sms/send", params)

        if data.get("status") != "OK":
            raise SmsSendError(data.get("status_text", "unknown sms.ru error"), status_code=data.get("status_code"))

        results = data.get("sms") or {}
        result = results.get(phone) or results.get(phone.lstrip("+")) or {}
        if result.get("status") != "OK":
            raise SmsSendError(
                result.get("status_text", "unknown sms.ru delivery error"),
                status_code=result.get("status_code"),
            )

    async def call_otp(self, phone: str, ip: str) -> str:
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

        return _normalize_call_code(data.get("code"), sorted(data))

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
