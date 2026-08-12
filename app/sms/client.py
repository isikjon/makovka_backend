from __future__ import annotations

import httpx

from ..config import Settings


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
        text = f"Код для входа в Маковку: {code}"
        params = {
            "api_id": self._settings.sms_ru_api_id,
            "to": phone,
            "msg": text,
            "json": 1,
        }
        try:
            resp = await self._http.get("/sms/send", params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise SmsSendError(f"sms.ru request failed: {type(exc).__name__}") from exc

        if data.get("status") != "OK":
            raise SmsSendError(data.get("status_text", "unknown sms.ru error"))

        result = data.get("sms", {}).get(phone, {})
        if result.get("status") != "OK":
            raise SmsSendError(
                result.get("status_text", "unknown sms.ru delivery error"),
                status_code=result.get("status_code"),
            )
