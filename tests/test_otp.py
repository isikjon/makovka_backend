import asyncio
import logging
import operator
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy.sql import operators
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.elements import BindParameter, BooleanClauseList, TextClause

from app.config import get_settings
from app.db import get_db
from app.deps import get_sms_client
from app.main import app
from app.models import OtpCode, User
from app.rate_limit import SlidingWindowLimiter
from app.routers import auth as auth_routes
from app.security import hash_otp_code, verify_otp_code
from app.sms.client import SmsCallResult, SmsSendError

PHONE = "+79990000001"
OTHER_PHONE = "+79990000002"
REVIEW_PHONE = "+79990000099"
PUBLIC_IP = "203.0.113.7"
BUSY = "Запрос по этому номеру уже обрабатывается, попробуйте позже"
OUT_OF_ATTEMPTS = "Слишком много неверных попыток, попробуйте позже"
CODE_ATTEMPTS_EXHAUSTED = "Слишком много попыток, запросите новый код"

_OPERATORS = {
    operators.eq: operator.eq,
    operators.gt: operator.gt,
    operators.is_: operator.is_,
}


def _matches(row, clause) -> bool:
    if isinstance(clause, BooleanClauseList):
        return all(_matches(row, part) for part in clause.clauses)
    other = clause.right.value if isinstance(clause.right, BindParameter) else None
    return _OPERATORS[clause.operator](getattr(row, clause.left.key), other)


class Result:
    def __init__(self, rows: list):
        self.rows = rows

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar_one_or_none(self):
        return self.first()


class OtpStore:
    def __init__(self, codes: list[OtpCode] | None = None):
        self.codes = list(codes or [])
        self.users: list[User] = []
        self.added: list = []
        self.locks: set[str] = set()
        self.lock_requests: list[str] = []


class OtpSession:
    def __init__(self, store: OtpStore):
        self.store = store
        self.held: set[str] = set()

    def _filter(self, rows: list, statement) -> list:
        return [row for row in rows if statement.whereclause is None or _matches(row, statement.whereclause)]

    def _try_lock(self, key: str) -> bool:
        self.store.lock_requests.append(key)
        if key in self.store.locks:
            return key in self.held
        self.store.locks.add(key)
        self.held.add(key)
        return True

    def release(self) -> None:
        self.store.locks -= self.held
        self.held.clear()

    async def execute(self, statement):
        if isinstance(statement, Update):
            values = statement.compile().params
            for row in self._filter(self.store.codes, statement):
                row.consumed_at = values["consumed_at"]
            return Result([])
        entity = statement.column_descriptions[0]["entity"]
        if entity is OtpCode:
            rows = sorted(self._filter(self.store.codes, statement), key=lambda row: row.created_at, reverse=True)
            return Result(rows)
        if entity is User:
            return Result(self._filter(self.store.users, statement))
        return Result([])

    async def scalar(self, statement, params: dict | None = None):
        if isinstance(statement, TextClause):
            return self._try_lock(params["key"])
        rows = self._filter(self.store.codes, statement)
        if statement.selected_columns[0].name == "coalesce":
            return sum(row.attempts for row in rows)
        return len(rows)

    def add(self, obj) -> None:
        self.store.added.append(obj)
        if isinstance(obj, OtpCode):
            obj.created_at = obj.created_at or datetime.now(timezone.utc)
            obj.attempts = obj.attempts or 0
            self.store.codes.append(obj)
        elif isinstance(obj, User):
            self.store.users.append(obj)

    async def flush(self) -> None:
        for obj in self.store.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self) -> None:
        await self.flush()
        self.release()


class FakeSms:
    def __init__(
        self,
        call_error: Exception | None = None,
        gate: asyncio.Event | None = None,
    ):
        self.call_error = call_error
        self.gate = gate
        self.started = asyncio.Event()
        self.sent: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []

    async def _hold(self) -> None:
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()

    async def call_otp(self, phone: str, ip: str) -> SmsCallResult:
        self.calls.append((phone, ip))
        await self._hold()
        if self.call_error is not None:
            raise self.call_error
        return SmsCallResult(code="1234", call_id="test-call", status_code=100)


def _code(
    phone: str = PHONE,
    *,
    channel: str = "sms",
    age: timedelta = timedelta(minutes=1),
    active: bool = False,
    attempts: int = 0,
    code: str = "999999",
) -> OtpCode:
    now = datetime.now(timezone.utc)
    return OtpCode(
        phone=phone,
        code_hash=hash_otp_code(code),
        channel=channel,
        attempts=attempts,
        created_at=now - age,
        expires_at=now + timedelta(minutes=4) if active else now - timedelta(seconds=1),
        consumed_at=None,
    )


class Env:
    def __init__(self, store: OtpStore, sms: FakeSms):
        self.store = store
        self.sms = sms

    def stored(self) -> list[OtpCode]:
        return [obj for obj in self.store.added if isinstance(obj, OtpCode)]


@pytest.fixture
def otp_env(monkeypatch, make_settings):
    monkeypatch.setattr(auth_routes, "call_ip_limiter", SlidingWindowLimiter(3600))

    def build(
        *,
        codes: list[OtpCode] | None = None,
        sms: FakeSms | None = None,
        **overrides,
    ) -> Env:
        values = {"otp_mock_mode": False, "sms_ru_api_id": "api-id"}
        values.update(overrides)
        settings = make_settings(**values)
        env = Env(OtpStore(codes), sms or FakeSms())

        async def fake_db():
            session = OtpSession(env.store)
            try:
                yield session
            finally:
                session.release()

        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_sms_client] = lambda: env.sms
        return env

    yield build
    app.dependency_overrides.clear()


def _client(ip: str = "127.0.0.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 5000)), base_url="http://test")


async def _request(body: dict, ip: str = "127.0.0.1") -> httpx.Response:
    async with _client(ip) as http:
        return await http.post("/auth/otp/request", json=body)


async def _verify(code: str, phone: str = PHONE) -> httpx.Response:
    async with _client() as http:
        return await http.post("/auth/otp/verify", json={"phone": phone, "code": code})


class TestCallDelivery:
    async def test_default_request_returns_four_digit_call_code(self, otp_env):
        env = otp_env()
        response = await _request({"phone": PHONE})
        assert response.status_code == 200
        assert response.json() == {
            "phone": PHONE,
            "expires_in_seconds": 300,
            "dev_code": None,
            "channel": "call",
            "code_length": 4,
        }
        assert env.sms.sent == []
        assert env.sms.calls == [(PHONE, "-1")]
        [stored] = env.stored()
        assert stored.channel == "call"
        assert verify_otp_code("1234", stored.code_hash)

    async def test_legacy_sms_channel_is_forced_to_call(self, otp_env):
        env = otp_env()
        response = await _request({"phone": PHONE, "channel": "sms"})
        assert response.status_code == 200
        assert response.json()["channel"] == "call"
        assert response.json()["code_length"] == 4
        assert env.sms.sent == []
        assert len(env.sms.calls) == 1

    async def test_unknown_channel_is_rejected(self, otp_env):
        otp_env()
        response = await _request({"phone": PHONE, "channel": "whatsapp"})
        assert response.status_code == 422

    async def test_call_failure_returns_502_without_sms_attempt(self, otp_env):
        env = otp_env(sms=FakeSms(call_error=SmsSendError("Недостаточно средств", status_code=201)))
        response = await _request({"phone": PHONE})
        assert response.status_code == 502
        assert response.json() == {"detail": "Не удалось отправить код: Недостаточно средств"}
        assert len(env.sms.calls) == 1
        assert env.sms.sent == []
        assert env.stored() == []

    async def test_active_code_is_replaced_by_a_new_call(self, otp_env):
        env = otp_env(codes=[_code(active=True)])
        response = await _request({"phone": PHONE})
        assert response.status_code == 200
        assert response.json()["channel"] == "call"
        assert len(env.sms.calls) == 1
        assert env.sms.sent == []

    async def test_logs_never_contain_codes(self, otp_env, caplog):
        env = otp_env()
        with caplog.at_level(logging.DEBUG):
            response = await _request({"phone": PHONE})
        assert response.status_code == 200
        assert "endpoint=/code/call" in caplog.text
        assert "call_id=test-call" in caplog.text
        assert "status_code=100" in caplog.text
        assert "1234" not in caplog.text


class TestExplicitCall:
    async def test_call_skips_sms_and_invalidates_active_code(self, otp_env):
        active = _code(active=True)
        env = otp_env(codes=[active])
        response = await _request({"phone": PHONE, "channel": "call"}, ip=PUBLIC_IP)
        assert response.status_code == 200
        assert response.json()["channel"] == "call"
        assert response.json()["code_length"] == 4
        assert env.sms.sent == []
        assert env.sms.calls == [(PHONE, PUBLIC_IP)]
        assert active.consumed_at is not None
        [stored] = env.stored()
        assert stored.channel == "call" and stored.consumed_at is None

    async def test_failed_call_keeps_active_code(self, otp_env):
        active = _code(active=True)
        env = otp_env(codes=[active], sms=FakeSms(call_error=SmsSendError("Номер недоступен", status_code=202)))
        response = await _request({"phone": PHONE, "channel": "call"})
        assert response.status_code == 502
        assert response.json() == {"detail": "Не удалось отправить код: Номер недоступен"}
        assert active.consumed_at is None
        assert env.stored() == []

    async def test_other_phones_codes_stay_active(self, otp_env):
        other = _code(OTHER_PHONE, active=True)
        otp_env(codes=[other])
        response = await _request({"phone": PHONE, "channel": "call"})
        assert response.status_code == 200
        assert other.consumed_at is None

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "testclient"])
    async def test_loopback_or_unknown_client_sends_minus_one(self, otp_env, host):
        env = otp_env()
        response = await _request({"phone": PHONE, "channel": "call"}, ip=host)
        assert response.status_code == 200
        assert env.sms.calls == [(PHONE, "-1")]


class TestRateLimits:
    async def test_requests_per_phone_per_hour(self, otp_env):
        env = otp_env(codes=[_code() for _ in range(5)])
        response = await _request({"phone": PHONE})
        assert response.status_code == 429
        assert response.json()["detail"] == "Слишком много запросов кода, попробуйте позже"
        assert env.sms.sent == []

    async def test_requests_per_phone_also_limit_explicit_calls(self, otp_env):
        env = otp_env(codes=[_code() for _ in range(5)])
        response = await _request({"phone": PHONE, "channel": "call"})
        assert response.status_code == 429
        assert env.sms.calls == []

    async def test_old_and_foreign_rows_do_not_count(self, otp_env):
        codes = [_code(age=timedelta(minutes=61)) for _ in range(5)] + [_code(OTHER_PHONE) for _ in range(5)]
        env = otp_env(codes=codes)
        response = await _request({"phone": PHONE})
        assert response.status_code == 200
        assert len(env.sms.calls) == 1

    async def test_calls_per_phone_per_hour(self, otp_env):
        env = otp_env(codes=[_code(channel="call") for _ in range(3)])
        response = await _request({"phone": PHONE, "channel": "call"})
        assert response.status_code == 429
        assert response.json()["detail"] == "Слишком много звонков на этот номер, попробуйте позже"
        assert env.sms.calls == []

    async def test_calls_per_phone_limit_blocks_the_only_flow(self, otp_env):
        env = otp_env(codes=[_code(channel="call") for _ in range(3)])
        response = await _request({"phone": PHONE})
        assert response.status_code == 429
        assert len(env.sms.sent) == 0
        assert env.sms.calls == []

    async def test_calls_per_ip_per_hour(self, otp_env):
        env = otp_env(otp_calls_per_hour_per_ip=2)
        phones = ["+79990000011", "+79990000012", "+79990000013"]
        statuses = [(await _request({"phone": phone, "channel": "call"}, ip=PUBLIC_IP)).status_code for phone in phones]
        assert statuses == [200, 200, 429]
        assert [phone for phone, _ in env.sms.calls] == phones[:2]
        response = await _request({"phone": phones[2], "channel": "call"}, ip="198.51.100.4")
        assert response.status_code == 200

    async def test_ip_limit_detail(self, otp_env):
        otp_env(otp_calls_per_hour_per_ip=0)
        response = await _request({"phone": PHONE, "channel": "call"}, ip=PUBLIC_IP)
        assert response.status_code == 429
        assert response.json()["detail"] == "Слишком много запросов звонка, попробуйте позже"

    async def test_unknown_ip_is_not_ip_limited(self, otp_env):
        env = otp_env(otp_calls_per_hour_per_ip=0)
        response = await _request({"phone": PHONE, "channel": "call"})
        assert response.status_code == 200
        assert env.sms.calls == [(PHONE, "-1")]


class TestFixedCodes:
    @pytest.mark.parametrize("channel", [None, "call"])
    async def test_review_bypass_unchanged(self, otp_env, channel):
        env = otp_env(review_bypass_phone=REVIEW_PHONE, review_bypass_code="4321")
        response = await _request({"phone": REVIEW_PHONE, "channel": channel})
        assert response.status_code == 200
        assert response.json() == {
            "phone": REVIEW_PHONE,
            "expires_in_seconds": 300,
            "dev_code": None,
            "channel": "call",
            "code_length": 4,
        }
        assert env.sms.sent == [] and env.sms.calls == []
        [stored] = env.stored()
        assert verify_otp_code("4321", stored.code_hash)

    async def test_review_bypass_ignores_hourly_limit(self, otp_env):
        otp_env(
            codes=[_code(REVIEW_PHONE) for _ in range(5)],
            review_bypass_phone=REVIEW_PHONE,
            review_bypass_code="4321",
        )
        response = await _request({"phone": REVIEW_PHONE})
        assert response.status_code == 200

    @pytest.mark.parametrize("channel", [None, "call"])
    async def test_mock_mode_unchanged(self, otp_env, channel):
        env = otp_env(otp_mock_mode=True, otp_dev_code="000000")
        response = await _request({"phone": PHONE, "channel": channel})
        assert response.status_code == 200
        assert response.json() == {
            "phone": PHONE,
            "expires_in_seconds": 300,
            "dev_code": "0000",
            "channel": "call",
            "code_length": 4,
        }
        assert env.sms.sent == [] and env.sms.calls == []

    async def test_mock_mode_replaces_active_code_with_call(self, otp_env):
        env = otp_env(codes=[_code(active=True)], otp_mock_mode=True)
        response = await _request({"phone": PHONE})
        assert response.status_code == 200
        assert response.json()["channel"] == "call"
        assert env.sms.sent == [] and env.sms.calls == []


class TestVerifyCallCode:
    async def test_four_digit_call_code_verifies(self, otp_env):
        env = otp_env()
        assert (await _request({"phone": PHONE})).json()["channel"] == "call"
        async with _client() as http:
            wrong = await http.post("/auth/otp/verify", json={"phone": PHONE, "code": "4321"})
            right = await http.post("/auth/otp/verify", json={"phone": PHONE, "code": "1234"})
        assert wrong.status_code == 400
        assert right.status_code == 200
        assert right.json()["is_new_user"] is True
        [stored] = env.stored()
        assert stored.attempts == 1
        assert stored.consumed_at is not None

    async def test_call_code_rejects_non_four_digit_input_without_using_attempt(self, otp_env):
        env = otp_env()
        assert (await _request({"phone": PHONE})).status_code == 200
        invalid = await _verify("12345")
        assert invalid.status_code == 400
        assert invalid.json()["detail"] == "Ожидается четырехзначный код"
        [stored] = env.stored()
        assert stored.attempts == 0


class TestGuessBudget:
    async def test_call_code_allows_three_wrong_attempts(self, otp_env):
        env = otp_env()
        assert (await _request({"phone": PHONE, "channel": "call"})).status_code == 200
        assert [(await _verify("0000")).status_code for _ in range(3)] == [400, 400, 400]
        blocked = await _verify("1234")
        assert blocked.status_code == 429
        assert blocked.json()["detail"] == CODE_ATTEMPTS_EXHAUSTED
        [stored] = env.stored()
        assert stored.attempts == 3 and stored.consumed_at is None

    async def test_call_code_allows_only_four_digits(self, otp_env):
        env = otp_env()
        assert (await _request({"phone": PHONE})).status_code == 200
        assert [(await _verify("0000")).status_code for _ in range(3)] == [400] * 3
        blocked = await _verify("1234")
        assert blocked.status_code == 429
        assert blocked.json()["detail"] == CODE_ATTEMPTS_EXHAUSTED

    async def test_failures_on_earlier_codes_block_verify(self, otp_env):
        active = _code(channel="call", active=True, code="1234")
        codes = [_code(channel="call", attempts=3), _code(channel="call", attempts=3), _code(attempts=4), active]
        otp_env(codes=codes)
        response = await _verify("1234")
        assert response.status_code == 429
        assert response.json()["detail"] == OUT_OF_ATTEMPTS
        assert active.consumed_at is None and active.attempts == 0

    async def test_old_and_foreign_failures_do_not_count(self, otp_env):
        active = _code(channel="call", active=True, code="1234")
        codes = [_code(attempts=10, age=timedelta(hours=25)), _code(OTHER_PHONE, attempts=10), active]
        otp_env(codes=codes)
        response = await _verify("1234")
        assert response.status_code == 200
        assert active.consumed_at is not None

    @pytest.mark.parametrize("channel", [None, "call"])
    async def test_exhausted_budget_blocks_new_codes(self, otp_env, channel):
        env = otp_env(codes=[_code(attempts=10)])
        response = await _request({"phone": PHONE, "channel": channel})
        assert response.status_code == 429
        assert response.json()["detail"] == OUT_OF_ATTEMPTS
        assert env.sms.sent == [] and env.sms.calls == []
        assert env.stored() == []

    async def test_budget_is_configurable(self, otp_env):
        otp_env(codes=[_code(attempts=2)], otp_failed_attempts_per_day_per_phone=2)
        response = await _request({"phone": PHONE, "channel": "call"})
        assert response.status_code == 429
        assert response.json()["detail"] == OUT_OF_ATTEMPTS

    async def test_requesting_new_calls_does_not_reset_budget(self, otp_env):
        env = otp_env(otp_calls_per_hour_per_phone=100, otp_requests_per_hour_per_phone=100)
        wrong_guesses = 0
        for _ in range(10):
            requested = await _request({"phone": PHONE, "channel": "call"}, ip=PUBLIC_IP)
            if requested.status_code != 200:
                break
            while (await _verify("0000")).json()["detail"] == "Неверный код":
                wrong_guesses += 1
        assert wrong_guesses == 10
        assert requested.status_code == 429
        assert requested.json()["detail"] == OUT_OF_ATTEMPTS
        assert len(env.sms.calls) == 4


class TestPerPhoneSerialization:
    async def test_parallel_call_requests_reach_sms_ru_once(self, otp_env):
        gate = asyncio.Event()
        env = otp_env(sms=FakeSms(gate=gate))
        first = asyncio.create_task(_request({"phone": PHONE, "channel": "call"}, ip=PUBLIC_IP))
        await asyncio.wait_for(env.sms.started.wait(), 5)
        rivals = await asyncio.wait_for(
            asyncio.gather(
                *(_request({"phone": PHONE, "channel": "call"}, ip=f"198.51.100.{i}") for i in range(9))
            ),
            5,
        )
        assert [response.status_code for response in rivals] == [429] * 9
        assert {response.json()["detail"] for response in rivals} == {BUSY}
        gate.set()
        assert (await first).status_code == 200
        assert len(env.sms.calls) == 1
        assert len(env.stored()) == 1
        assert set(env.store.lock_requests) == {f"otp:{PHONE}"}

    async def test_parallel_default_requests_make_one_call(self, otp_env):
        gate = asyncio.Event()
        env = otp_env(sms=FakeSms(gate=gate))
        first = asyncio.create_task(_request({"phone": PHONE}))
        await asyncio.wait_for(env.sms.started.wait(), 5)
        rival = await asyncio.wait_for(_request({"phone": PHONE}), 5)
        assert rival.status_code == 429
        assert rival.json()["detail"] == BUSY
        gate.set()
        assert (await first).status_code == 200
        after = await _request({"phone": PHONE})
        assert after.status_code == 200
        assert len(env.sms.sent) == 0
        assert len(env.sms.calls) == 2

    async def test_other_phones_are_not_blocked(self, otp_env):
        gate = asyncio.Event()
        env = otp_env(sms=FakeSms(gate=gate), review_bypass_phone=OTHER_PHONE, review_bypass_code="4321")
        first = asyncio.create_task(_request({"phone": PHONE, "channel": "call"}))
        await asyncio.wait_for(env.sms.started.wait(), 5)
        other = await asyncio.wait_for(_request({"phone": OTHER_PHONE}), 5)
        assert other.status_code == 200
        gate.set()
        assert (await first).status_code == 200

    async def test_verify_is_rejected_while_request_in_flight(self, otp_env):
        gate = asyncio.Event()
        active = _code(active=True, code="123456")
        env = otp_env(codes=[active], sms=FakeSms(gate=gate))
        first = asyncio.create_task(_request({"phone": PHONE, "channel": "call"}))
        await asyncio.wait_for(env.sms.started.wait(), 5)
        response = await asyncio.wait_for(_verify("000000"), 5)
        assert response.status_code == 429
        assert response.json()["detail"] == BUSY
        assert active.attempts == 0
        gate.set()
        assert (await first).status_code == 200

    async def test_lock_is_released_after_failed_delivery(self, otp_env):
        env = otp_env(sms=FakeSms(call_error=SmsSendError("Номер недоступен", status_code=202)))
        statuses = [(await _request({"phone": PHONE, "channel": "call"})).status_code for _ in range(2)]
        assert statuses == [502, 502]
        assert len(env.sms.calls) == 2
        assert env.store.locks == set()

    async def test_lock_is_released_after_rejection(self, otp_env):
        env = otp_env(codes=[_code(active=True)])
        assert (await _request({"phone": PHONE})).status_code == 200
        assert env.store.locks == set()
        assert (await _request({"phone": PHONE, "channel": "call"})).status_code == 200


class TestSlidingWindowLimiter:
    def test_limits_per_key_within_window(self):
        clock = [0.0]
        limiter = SlidingWindowLimiter(60, clock=lambda: clock[0])
        assert [limiter.try_acquire("a", 2) for _ in range(3)] == [True, True, False]
        assert limiter.try_acquire("b", 2) is True
        clock[0] = 59.0
        assert limiter.try_acquire("a", 2) is False
        clock[0] = 60.0
        assert limiter.try_acquire("a", 2) is True
