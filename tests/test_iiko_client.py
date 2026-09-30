import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.iiko.client import IikoClientError, IikoCloudClient, IikoMutationBlocked

from .conftest import TEST_GUEST_PHONE

Handler = Callable[[str, dict], httpx.Response]


class FakeIiko:
    def __init__(self, handler: Handler | None = None):
        self.calls: list[tuple[str, dict, float]] = []
        self.token_calls = 0
        self._handler = handler or (lambda path, body: httpx.Response(200, json={}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/api/1/access_token":
            self.token_calls += 1
            return httpx.Response(200, json={"token": "token-1"})
        assert request.headers["Authorization"] == "Bearer token-1"
        self.calls.append((request.url.path, body, time.monotonic()))
        return self._handler(request.url.path, body)

    @property
    def paths(self) -> list[str]:
        return [path for path, _, _ in self.calls]


@pytest.fixture
async def make_client(make_settings):
    clients: list[IikoCloudClient] = []

    def factory(fake: FakeIiko, **overrides) -> IikoCloudClient:
        client = IikoCloudClient(make_settings(**overrides))
        client._http = httpx.AsyncClient(base_url="https://iiko.invalid", transport=httpx.MockTransport(fake))
        client._retry_delays = (0.0, 0.0, 0.0)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        await client.aclose()


class TestStagingGuard:
    async def test_blocks_customer_id_only_mutation_without_phone(self, make_client):
        fake = FakeIiko()
        client = make_client(fake)
        with pytest.raises(IikoMutationBlocked):
            await client.add_customer_category(customer_id="guest-1", category_id="cat-gold", guard_phone="")
        with pytest.raises(IikoMutationBlocked):
            await client._post(
                None,
                "/api/1/loyalty/iiko/customer_category/remove",
                {"organizationId": "org-1", "customerId": "guest-1", "categoryId": "cat-gold"},
                mutating=True,
            )
        assert fake.calls == []
        assert fake.token_calls == 0

    async def test_blocks_other_guest_phone(self, make_client):
        fake = FakeIiko()
        client = make_client(fake)
        with pytest.raises(IikoMutationBlocked):
            await client.remove_customer_category(
                customer_id="guest-1", category_id="cat-gold", guard_phone="+79991112233"
            )
        assert fake.calls == []

    async def test_blocks_when_payload_phone_disagrees_with_guard_phone(self, make_client):
        fake = FakeIiko()
        client = make_client(fake)
        with pytest.raises(IikoMutationBlocked):
            await client._post(
                None,
                "/api/1/loyalty/iiko/customer/card/add",
                {"customerId": "guest-1", "phone": "+79991112233"},
                mutating=True,
                guard_phone=TEST_GUEST_PHONE,
            )
        assert fake.calls == []

    async def test_blocks_without_configured_test_guest(self, make_client):
        fake = FakeIiko()
        client = make_client(fake, iiko_test_guest_phone="")
        with pytest.raises(IikoMutationBlocked):
            await client.add_customer_category(
                customer_id="guest-1", category_id="cat-gold", guard_phone=TEST_GUEST_PHONE
            )
        assert fake.calls == []

    async def test_allows_test_guest_phone(self, make_client):
        fake = FakeIiko()
        client = make_client(fake)
        await client.add_customer_category(customer_id="guest-1", category_id="cat-gold", guard_phone=TEST_GUEST_PHONE)
        await client.remove_customer_category(
            customer_id="guest-1", category_id="cat-silver", guard_phone=TEST_GUEST_PHONE
        )
        assert [(path, body) for path, body, _ in fake.calls] == [
            (
                "/api/1/loyalty/iiko/customer_category/add",
                {"organizationId": "org-1", "customerId": "guest-1", "categoryId": "cat-gold"},
            ),
            (
                "/api/1/loyalty/iiko/customer_category/remove",
                {"organizationId": "org-1", "customerId": "guest-1", "categoryId": "cat-silver"},
            ),
        ]

    async def test_existing_phone_payload_mutations_still_allowed(self, make_client):
        fake = FakeIiko()
        client = make_client(fake)
        await client.add_card(customer_id="guest-1", card_number="1234567890123456", phone=TEST_GUEST_PHONE)
        assert fake.paths == ["/api/1/loyalty/iiko/customer/card/add"]

    async def test_kill_switch_blocks_test_guest_too(self, make_client):
        fake = FakeIiko()
        client = make_client(fake, iiko_mutations_enabled=False)
        with pytest.raises(IikoMutationBlocked):
            await client.add_customer_category(
                customer_id="guest-1", category_id="cat-gold", guard_phone=TEST_GUEST_PHONE
            )
        assert fake.calls == []

    async def test_production_does_not_restrict_guests(self, make_client):
        fake = FakeIiko()
        client = make_client(fake, iiko_env="production")
        await client.add_customer_category(customer_id="guest-1", category_id="cat-gold", guard_phone="+79991112233")
        assert fake.paths == ["/api/1/loyalty/iiko/customer_category/add"]


def _sequence(*responses: httpx.Response) -> Handler:
    queue = list(responses)

    def handler(path: str, body: dict) -> httpx.Response:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handler


class TestRetries:
    async def test_retries_rate_limit_then_succeeds(self, make_client):
        fake = FakeIiko(
            _sequence(
                httpx.Response(429, json={"errorDescription": "Too many requests"}),
                httpx.Response(429, json={"errorDescription": "Too many requests"}),
                httpx.Response(200, json={"guestCategories": [{"id": "cat-1", "name": "Бонусная программа"}]}),
            )
        )
        client = make_client(fake)
        categories = await client.list_customer_categories()
        assert categories == [{"id": "cat-1", "name": "Бонусная программа"}]
        assert len(fake.calls) == 3

    async def test_gives_up_after_three_retries(self, make_client):
        fake = FakeIiko(_sequence(httpx.Response(429, json={})))
        client = make_client(fake)
        with pytest.raises(IikoClientError) as exc_info:
            await client.list_customer_categories()
        assert exc_info.value.status_code == 429
        assert len(fake.calls) == 4

    async def test_rate_limited_mutation_is_retried(self, make_client):
        fake = FakeIiko(_sequence(httpx.Response(429, json={}), httpx.Response(200, json={})))
        client = make_client(fake)
        await client.add_customer_category(customer_id="guest-1", category_id="cat-gold", guard_phone=TEST_GUEST_PHONE)
        assert len(fake.calls) == 2

    async def test_client_errors_are_not_retried(self, make_client):
        fake = FakeIiko(_sequence(httpx.Response(400, json={"errorDescription": "bad"})))
        client = make_client(fake)
        with pytest.raises(IikoClientError) as exc_info:
            await client.get_customer_by_id("guest-1")
        assert exc_info.value.status_code == 400
        assert len(fake.calls) == 1

    async def test_transient_server_errors_retried_for_reads_only(self, make_client):
        read_fake = FakeIiko(_sequence(httpx.Response(503, json={}), httpx.Response(200, json={"id": "guest-1"})))
        assert await make_client(read_fake).get_customer_by_id("guest-1") == {"id": "guest-1"}
        assert len(read_fake.calls) == 2

        write_fake = FakeIiko(_sequence(httpx.Response(503, json={}), httpx.Response(200, json={})))
        with pytest.raises(IikoClientError):
            await make_client(write_fake).add_customer_category(
                customer_id="guest-1", category_id="cat-gold", guard_phone=TEST_GUEST_PHONE
            )
        assert len(write_fake.calls) == 1

    async def test_network_errors_are_retried(self, make_client):
        attempts = []

        def handler(path: str, body: dict) -> httpx.Response:
            attempts.append(path)
            if len(attempts) == 1:
                raise httpx.ConnectError("connection refused")
            if len(attempts) == 2:
                raise httpx.ReadTimeout("timed out")
            return httpx.Response(200, json={"id": "guest-1"})

        client = make_client(FakeIiko(handler))
        assert await client.get_customer_by_id("guest-1") == {"id": "guest-1"}
        assert len(attempts) == 3

    async def test_token_failure_is_wrapped(self, make_settings):
        def transport(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={})

        client = IikoCloudClient(make_settings())
        client._http = httpx.AsyncClient(base_url="https://iiko.invalid", transport=httpx.MockTransport(transport))
        client._retry_delays = (0.0, 0.0, 0.0)
        try:
            with pytest.raises(IikoClientError) as exc_info:
                await client.get_customer_by_id("guest-1")
        finally:
            await client.aclose()
        assert exc_info.value.status_code == 500


class TestPacing:
    async def test_requests_are_spaced_by_min_interval(self, make_client):
        fake = FakeIiko()
        client = make_client(fake, iiko_min_request_interval_ms=100)
        for _ in range(3):
            await client.list_customer_categories()
        moments = [moment for _, _, moment in fake.calls]
        gaps = [later - earlier for earlier, later in zip(moments, moments[1:])]
        assert len(gaps) == 2
        assert all(gap >= 0.09 for gap in gaps)


class TestReadMethods:
    async def test_customer_info_by_id_payload(self, make_client):
        fake = FakeIiko(lambda path, body: httpx.Response(200, json={"id": "guest-1", "cards": []}))
        client = make_client(fake)
        assert await client.get_customer_by_id("guest-1") == {"id": "guest-1", "cards": []}
        assert fake.calls[0][:2] == (
            "/api/1/loyalty/iiko/customer/info",
            {"organizationId": "org-1", "type": "id", "id": "guest-1"},
        )

    async def test_orders_sum_requests_only_all_time_orders_sum(self, make_client):
        fake = FakeIiko(
            lambda path, body: httpx.Response(
                200, json={"counters": [{"guestId": "guest-1", "period": 0, "metric": 1, "value": 407.0}]}
            )
        )
        client = make_client(fake)
        assert await client.get_orders_sum_all_time("guest-1") == Decimal("407.00")
        assert fake.calls[0][:2] == (
            "/api/1/loyalty/iiko/get_counters",
            {"organizationId": "org-1", "guestIds": ["guest-1"], "periods": ["AllTime"], "metrics": ["OrdersSum"]},
        )

    async def test_transactions_follow_pages(self, make_client):
        def handler(path: str, body: dict) -> httpx.Response:
            size = 100 if body["pageNumber"] == 0 else 3
            items = [{"id": f"{body['pageNumber']}-{index}"} for index in range(size)]
            return httpx.Response(
                200, json={"transactions": items, "pageNumber": body["pageNumber"], "pageSize": body["pageSize"]}
            )

        fake = FakeIiko(handler)
        client = make_client(fake)
        transactions = await client.get_transactions(
            "guest-1",
            datetime(2024, 9, 30, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        )
        assert len(transactions) == 103
        bodies = [body for _, body, _ in fake.calls]
        assert [body["pageNumber"] for body in bodies] == [0, 1]
        assert bodies[0] == {
            "organizationId": "org-1",
            "customerId": "guest-1",
            "dateFrom": "2024-09-30 12:00:00.000",
            "dateTo": "2026-10-01 12:00:00.000",
            "pageNumber": 0,
            "pageSize": 100,
        }

    async def test_transactions_single_empty_page(self, make_client):
        fake = FakeIiko(lambda path, body: httpx.Response(200, json={"transactions": []}))
        client = make_client(fake)
        now = datetime(2026, 9, 30, tzinfo=timezone.utc)
        assert await client.get_transactions("guest-1", now, now) == []
        assert len(fake.calls) == 1

    async def test_terminal_groups_are_cached(self, make_client):
        fake = FakeIiko(
            lambda path, body: httpx.Response(
                200,
                json={
                    "terminalGroups": [
                        {"organizationId": "org-1", "items": [{"id": "tg-1", "name": "Сходненская 35 (Касса 1)"}]}
                    ]
                },
            )
        )
        client = make_client(fake)
        assert await client.list_terminal_groups() == {"tg-1": "Сходненская 35"}
        assert await client.list_terminal_groups() == {"tg-1": "Сходненская 35"}
        assert fake.calls[0][:2] == (
            "/api/1/terminal_groups",
            {"organizationIds": ["org-1"], "includeDisabled": True},
        )
        assert len(fake.calls) == 1
