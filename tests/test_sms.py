import httpx
import pytest

from app.sms.client import SmsRuClient, SmsSendError

PHONE = "+79990000001"


def _client(make_settings, handler) -> SmsRuClient:
    client = SmsRuClient(make_settings(sms_ru_api_id="api-id"))
    client._http = httpx.AsyncClient(base_url="https://sms.invalid", transport=httpx.MockTransport(handler))
    return client


async def test_call_otp_requests_call_and_returns_code(make_settings):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"status": "OK", "code": "0427", "call_id": "000-1", "cost": 0.4, "balance": 100.0}
        )

    client = _client(make_settings, handler)
    result = await client.call_otp(PHONE, "203.0.113.7")
    await client.aclose()
    assert result.code == "0427"
    assert result.call_id == "000-1"
    assert result.status_code is None
    [request] = requests
    assert request.method == "GET"
    assert request.url.path == "/code/call"
    assert dict(request.url.params) == {"phone": "79990000001", "ip": "203.0.113.7", "api_id": "api-id"}


async def test_call_otp_raises_sms_ru_error(make_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ERROR", "status_code": 202, "status_text": "Неправильно указан номер"})

    client = _client(make_settings, handler)
    with pytest.raises(SmsSendError) as caught:
        await client.call_otp(PHONE, "-1")
    await client.aclose()
    assert caught.value.status_code == 202
    assert str(caught.value) == "Неправильно указан номер"


@pytest.mark.parametrize("code", ["12345", "12a4", "١٢٣٤", "", None, 12345, -1, True, 12.5, ["1234"]])
async def test_call_otp_rejects_malformed_code(make_settings, code):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "OK", "code": code, "call_id": "000-1"})

    client = _client(make_settings, handler)
    with pytest.raises(SmsSendError) as caught:
        await client.call_otp(PHONE, "-1")
    await client.aclose()
    assert str(caught.value).startswith("sms.ru returned an invalid call code")


@pytest.mark.parametrize(
    ("code", "expected"),
    [("1435", "1435"), (1435, "1435"), (123, "0123"), (7, "0007"), ("0123", "0123"), ("123", "0123"), (" 1435 ", "1435"), (0, "0000")],
)
async def test_call_otp_normalizes_code(make_settings, code, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "OK", "code": code, "call_id": "000-1", "cost": 0.4})

    client = _client(make_settings, handler)
    assert (await client.call_otp(PHONE, "-1")).code == expected
    await client.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["OK"]),
    ],
)
async def test_call_otp_wraps_transport_and_format_errors(make_settings, response):
    client = _client(make_settings, lambda request: response)
    with pytest.raises(SmsSendError):
        await client.call_otp(PHONE, "-1")
    await client.aclose()


async def test_call_otp_wraps_network_errors(make_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    client = _client(make_settings, handler)
    with pytest.raises(SmsSendError) as caught:
        await client.call_otp(PHONE, "-1")
    await client.aclose()
    assert str(caught.value) == "sms.ru request failed: ConnectTimeout"
