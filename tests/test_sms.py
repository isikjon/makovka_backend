import httpx

from app.sms.client import SmsRuClient

PHONE = "+79990000001"


async def test_login_code_text_uses_the_new_brand(make_settings):
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.params["msg"])
        return httpx.Response(200, json={"status": "OK", "sms": {PHONE: {"status": "OK"}}})

    client = SmsRuClient(make_settings(sms_ru_api_id="api-id"))
    client._http = httpx.AsyncClient(base_url="https://sms.invalid", transport=httpx.MockTransport(handler))
    try:
        await client.send_otp(PHONE, "123456")
    finally:
        await client.aclose()
    assert sent == ["Код для входа в приложение Пекарня: 123456"]
    assert "Маковк" not in sent[0]
