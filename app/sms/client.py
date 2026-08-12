from __future__ import annotations

import httpx

from ..config import Settings


class SmsSendError(Exception):
    def __init__(self, message: str, err_code: str | None = None):
        super().__init__(message)
        self.err_code = err_code


class SmsProstoClient:
    _BASE_URL = "https://api.sms-prosto.ru"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._http = httpx.AsyncClient(base_url=self._BASE_URL, timeout=15.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send_otp(self, phone: str, code: str) -> None:
        text = f"Код для входа в Маковку: {code}"
        params = {
            "method": "push_msg",
            "key": self._settings.sms_prosto_api_key,
            "phone": phone,
            "text": text,
            "sender_name": self._settings.sms_prosto_sender_name,
            "format": "json",
        }
        try:
            resp = await self._http.get("/", params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise SmsSendError(f"sms-prosto request failed: {type(exc).__name__}") from exc

        msg = data.get("response", {}).get("msg", {})
        err_code = msg.get("err_code")
        if err_code != "0":
            raise SmsSendError(msg.get("text", "unknown sms-prosto error"), err_code=err_code)
