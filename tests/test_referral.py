import asyncio
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.db import get_db
from app.deps import get_current_user
from app.iiko.client import IikoClientError, IikoCloudClient, IikoMutationBlocked
from app.main import app
from app.models import ReferralReward, User
from app.referral import service as referral_service
from app.referral.service import (
    ALREADY_APPLIED,
    CODE_ALPHABET,
    CODE_LENGTH,
    CODE_NOT_FOUND,
    MUTUAL_CODE,
    OWN_CODE,
    REWARD_MAX_ATTEMPTS,
    ReferralError,
    RewardParties,
    apply_referral_code,
    ensure_referral_code,
    generate_referral_code,
    normalize_code,
    outcome_unknown,
    process_referral_reward,
    share_text,
)
from app.runtime_settings import get_effective_settings

from .conftest import TEST_GUEST_PHONE

REFERRED_PHONE = TEST_GUEST_PHONE
REFERRER_PHONE = "+79990000005"
REFERRED_CUSTOMER = "guest-new"
REFERRER_CUSTOMER = "guest-old"
WALLET = "bonus-wallet"


class TestCodes:
    def test_generated_codes_use_unambiguous_alphabet(self):
        codes = {generate_referral_code() for _ in range(300)}
        assert len(codes) > 290
        for code in codes:
            assert len(code) == CODE_LENGTH == 8
            assert set(code) <= set(CODE_ALPHABET)
        assert not set("01IO") & set(CODE_ALPHABET)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (" abcd2345 ", "ABCD2345"),
            ("Makov123", "MAKOV123"),
            ("AB12", "AB12"),
            ("К7МРХЕ3А", "K7MPXE3A"),
            ("авсенкмртху", "ABCEHKMPTXY"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_code(raw) == expected

    @pytest.mark.parametrize("raw", ["", "abc", "ABCD-2345", "код12345", "A" * 17, "ABCD 2345", "'; drop"])
    def test_normalize_rejects(self, raw):
        assert normalize_code(raw) is None

    def test_share_text(self, make_settings):
        assert share_text(make_settings(), "ABCD2345") == (
            "Мой промокод в приложении пекарни: ABCD2345. Скачайте приложение и введите промокод при регистрации: "
            "https://app.top1makovka.ru"
        )


class Savepoint:
    def __init__(self, session: "CodeSession"):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.session.savepoint_rollbacks += 1
            for user in self.session.expire_on_rollback:
                user.id = None
        return False


class CodeSession:
    def __init__(self, outcomes: list, rows: list | None = None, expire_on_rollback: list[User] | None = None):
        self.outcomes = list(outcomes)
        self.rows = list(rows or [])
        self.expire_on_rollback = list(expire_on_rollback or [])
        self.statements: list = []
        self.rollbacks = 0
        self.savepoint_rollbacks = 0
        self.commits = 0
        self.refreshed: list = []
        self.concurrent_code: str | None = None

    async def scalar(self, statement):
        self.statements.append(statement)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def execute(self, statement):
        self.statements.append(statement)
        return list(self.rows)

    def begin_nested(self) -> Savepoint:
        return Savepoint(self)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, obj, attribute_names=None):
        self.refreshed.append(attribute_names)
        obj.referral_code = self.concurrent_code


def _collision() -> IntegrityError:
    return IntegrityError("UPDATE users", {}, Exception("duplicate key value violates uq_users_referral_code"))


def _user(phone: str = REFERRED_PHONE, **fields) -> User:
    return User(id=fields.pop("id", uuid.uuid4()), phone=phone, name="Анна", **fields)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _params(statement) -> list:
    return list(statement.compile(dialect=postgresql.dialect()).params.values())


class TestEnsureCode:
    async def test_existing_code_is_returned_without_queries(self):
        db = CodeSession([])
        assert await ensure_referral_code(db, _user(referral_code="KEEPME22")) == "KEEPME22"
        assert db.statements == []

    async def test_retries_on_collision_without_reading_the_expired_user(self, monkeypatch):
        codes = iter(["TAKEN222", "FRESH333"])
        monkeypatch.setattr(referral_service, "generate_referral_code", lambda: next(codes))
        user = _user()
        user_id = user.id
        db = CodeSession([_collision(), "FRESH333"], expire_on_rollback=[user])
        assert await ensure_referral_code(db, user) == "FRESH333"
        assert user.referral_code == "FRESH333"
        assert db.savepoint_rollbacks == 1
        assert db.rollbacks == 0
        assert [user_id in _params(statement) for statement in db.statements] == [True, True]
        assert "FRESH333" in _params(db.statements[1])
        assert db.commits == 1

    async def test_concurrent_assignment_wins(self):
        db = CodeSession([None])
        db.concurrent_code = "OTHER222"
        user = _user()
        assert await ensure_referral_code(db, user) == "OTHER222"
        assert db.refreshed == [["referral_code"]]

    async def test_gives_up_after_repeated_collisions(self):
        db = CodeSession([_collision() for _ in range(8)])
        with pytest.raises(RuntimeError):
            await ensure_referral_code(db, _user())
        assert db.savepoint_rollbacks == 8


REFERRED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


class TestApply:
    async def test_applies_case_insensitively_under_row_locks(self, make_settings):
        referrer = _user(REFERRER_PHONE, referral_code="MAKOV234")
        user = _user()
        db = CodeSession([referrer, REFERRED_AT], rows=[(user.id, None), (referrer.id, None)])
        await apply_referral_code(db, user, " makov234 ", make_settings())
        assert user.referred_by_id == referrer.id
        assert user.referred_at == REFERRED_AT
        assert db.commits == 1
        lookup, lock, update = db.statements
        assert "MAKOV234" in _params(lookup)
        assert "FOR UPDATE" in _sql(lock)
        assert "ORDER BY users.id" in _sql(lock)
        assert "referred_at=now()" in _sql(update)

    async def test_cyrillic_look_alike_letters_are_accepted(self, make_settings):
        referrer = _user(REFERRER_PHONE, referral_code="K7MPXE3A")
        user = _user()
        db = CodeSession([referrer, REFERRED_AT], rows=[(user.id, None), (referrer.id, None)])
        await apply_referral_code(db, user, "к7мрхе3а", make_settings())
        assert "K7MPXE3A" in _params(db.statements[0])
        assert user.referred_by_id == referrer.id

    async def test_unknown_code(self, make_settings):
        db = CodeSession([None])
        with pytest.raises(ReferralError) as exc_info:
            await apply_referral_code(db, _user(), "NOPE2345", make_settings())
        assert (exc_info.value.status_code, exc_info.value.detail) == (400, CODE_NOT_FOUND)

    async def test_malformed_code_is_not_queried(self, make_settings):
        db = CodeSession([])
        with pytest.raises(ReferralError) as exc_info:
            await apply_referral_code(db, _user(), "%%%", make_settings())
        assert exc_info.value.detail == CODE_NOT_FOUND
        assert db.statements == []

    async def test_own_code(self, make_settings):
        user = _user(referral_code="SELF2345")
        db = CodeSession([user])
        with pytest.raises(ReferralError) as exc_info:
            await apply_referral_code(db, user, "self2345", make_settings())
        assert (exc_info.value.status_code, exc_info.value.detail) == (400, OWN_CODE)
        assert user.referred_by_id is None

    async def test_demo_account_code_is_not_offered(self, make_settings):
        demo = _user(REFERRER_PHONE, referral_code="DEMO2345")
        settings = make_settings(review_bypass_phone=REFERRER_PHONE, review_demo_card_number="7777000011112222")
        db = CodeSession([demo])
        with pytest.raises(ReferralError) as exc_info:
            await apply_referral_code(db, _user(), "DEMO2345", settings)
        assert exc_info.value.detail == CODE_NOT_FOUND
        assert len(db.statements) == 1

    async def test_mutual_referral_is_detected_from_locked_rows(self, make_settings):
        user = _user()
        referrer = _user(REFERRER_PHONE, referral_code="FRND2345")
        db = CodeSession([referrer], rows=[(user.id, None), (referrer.id, user.id)])
        with pytest.raises(ReferralError) as exc_info:
            await apply_referral_code(db, user, "FRND2345", make_settings())
        assert (exc_info.value.status_code, exc_info.value.detail) == (400, MUTUAL_CODE)
        assert user.referred_by_id is None
        assert db.rollbacks == 1
        assert db.commits == 0
        assert len(db.statements) == 2

    async def test_cannot_change_once_set(self, make_settings):
        db = CodeSession([])
        with pytest.raises(ReferralError) as exc_info:
            await apply_referral_code(db, _user(referred_by_id=uuid.uuid4()), "ANY23456", make_settings())
        assert (exc_info.value.status_code, exc_info.value.detail) == (409, ALREADY_APPLIED)
        assert db.statements == []

    async def test_concurrent_apply_is_conflict(self, make_settings):
        referrer = _user(REFERRER_PHONE, referral_code="MAKOV234")
        user = _user()
        db = CodeSession([referrer], rows=[(user.id, uuid.uuid4()), (referrer.id, None)])
        with pytest.raises(ReferralError) as exc_info:
            await apply_referral_code(db, user, "MAKOV234", make_settings())
        assert exc_info.value.status_code == 409
        assert user.referred_by_id is None
        assert db.rollbacks == 1


@pytest.fixture
def referral_env(make_settings):
    user = _user(referral_code="MAKOV234")
    session = CodeSession([3, 2])

    async def fake_db():
        yield session

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_effective_settings] = lambda: make_settings(
        referral_rewards_enabled=True, referral_bonus_amount=250
    )
    yield session
    app.dependency_overrides.clear()


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


class TestRoutes:
    async def test_requires_auth(self, client):
        assert (await client.get("/referral")).status_code == 401
        assert (await client.post("/referral/apply", json={"code": "X"})).status_code == 401

    async def test_get_referral(self, referral_env, client):
        body = (await client.get("/referral")).json()
        assert body == {
            "code": "MAKOV234",
            "invited_count": 3,
            "rewarded_count": 2,
            "bonus_per_friend": 250.0,
            "rewards_enabled": True,
            "share_text": "Мой промокод в приложении пекарни: MAKOV234. Скачайте приложение и введите промокод при "
            "регистрации: https://app.top1makovka.ru",
        }

    @pytest.mark.parametrize(
        ("error", "status_code"),
        [
            (ReferralError(400, CODE_NOT_FOUND), 400),
            (ReferralError(400, OWN_CODE), 400),
            (ReferralError(409, ALREADY_APPLIED), 409),
        ],
    )
    async def test_apply_errors(self, referral_env, client, monkeypatch, error, status_code):
        async def fail(db, user, code, settings):
            raise error

        monkeypatch.setattr("app.routers.referral.apply_referral_code", fail)
        response = await client.post("/referral/apply", json={"code": "ABCD2345"})
        assert response.status_code == status_code
        assert response.json() == {"detail": error.detail}

    async def test_apply_success(self, referral_env, client, monkeypatch):
        applied = []

        async def apply(db, user, code, settings):
            applied.append((code, settings.referral_bonus_amount))

        monkeypatch.setattr("app.routers.referral.apply_referral_code", apply)
        response = await client.post("/referral/apply", json={"code": "abcd2345"})
        assert response.json() == {"applied": True}
        assert applied == [("abcd2345", 250.0)]

    async def test_apply_rejects_oversized_payload(self, referral_env, client):
        assert (await client.post("/referral/apply", json={"code": "A" * 65})).status_code == 422


class RewardSession:
    def __init__(self):
        self.in_transaction = False
        self.commits = 0

    def begin_work(self) -> None:
        self.in_transaction = True

    async def commit(self) -> None:
        self.in_transaction = False
        self.commits += 1


_REWARD_FIELDS = (
    "id",
    "referred_user_id",
    "referrer_user_id",
    "amount",
    "referred_done",
    "referrer_done",
    "referred_started_at",
    "referrer_started_at",
    "attempts",
    "last_error",
)


def _reward(**fields) -> ReferralReward:
    values = {
        "id": uuid.uuid4(),
        "referred_user_id": uuid.uuid4(),
        "referrer_user_id": uuid.uuid4(),
        "amount": Decimal("300.00"),
        "referred_done": False,
        "referrer_done": False,
        "referred_started_at": None,
        "referrer_started_at": None,
        "attempts": 0,
        "last_error": None,
    }
    values.update(fields)
    return ReferralReward(**values)


def _copy(reward: ReferralReward) -> ReferralReward:
    return ReferralReward(**{name: getattr(reward, name) for name in _REWARD_FIELDS})


class RewardStore:
    def __init__(
        self,
        reward: ReferralReward | None = None,
        *,
        qualifying: bool = True,
        parties: RewardParties = RewardParties(REFERRED_CUSTOMER, REFERRER_PHONE, REFERRER_CUSTOMER),
    ):
        self.reward = reward
        self.qualifying = qualifying
        self.parties = parties
        self.created: list[Decimal] = []
        self.touched = False
        self.fail_finish = False

    def install(self, monkeypatch) -> None:
        async def load_reward(db, referred_user_id):
            self.touched = True
            db.begin_work()
            return _copy(self.reward) if self.reward is not None else None

        async def has_qualifying_purchase(db, user):
            db.begin_work()
            return self.qualifying

        async def create_reward(db, user, amount):
            db.begin_work()
            self.created.append(amount)
            self.reward = _reward(referred_user_id=user.id, referrer_user_id=user.referred_by_id, amount=amount)
            await db.commit()
            return _copy(self.reward)

        async def load_parties(db, user, reward):
            db.begin_work()
            return self.parties

        async def claim_side(db, reward_id, side):
            assert reward_id == self.reward.id
            claimed = not getattr(self.reward, side.done) and getattr(self.reward, side.started_at) is None
            if claimed:
                setattr(self.reward, side.started_at, datetime.now(timezone.utc))
            await db.commit()
            return claimed

        async def release_side(db, reward_id, side):
            setattr(self.reward, side.started_at, None)
            await db.commit()

        async def finish_side(db, reward_id, side):
            if self.fail_finish:
                raise ConnectionError("database went away")
            setattr(self.reward, side.done, True)
            await db.commit()

        async def record_result(db, reward_id, error, *, counted=False, final=False):
            self.reward.last_error = error
            if final:
                self.reward.attempts = REWARD_MAX_ATTEMPTS
            elif counted:
                self.reward.attempts += 1
            await db.commit()

        monkeypatch.setattr(referral_service, "_load_reward", load_reward)
        monkeypatch.setattr(referral_service, "_has_qualifying_purchase", has_qualifying_purchase)
        monkeypatch.setattr(referral_service, "_create_reward", create_reward)
        monkeypatch.setattr(referral_service, "_load_parties", load_parties)
        monkeypatch.setattr(referral_service, "_claim_side", claim_side)
        monkeypatch.setattr(referral_service, "_release_side", release_side)
        monkeypatch.setattr(referral_service, "_finish_side", finish_side)
        monkeypatch.setattr(referral_service, "_record_result", record_result)


class TopupIiko:
    def __init__(self, failures: dict[str, Exception] | None = None, blocked: set[str] | None = None):
        self.failures = dict(failures or {})
        self.blocked = set(blocked or ())
        self.calls: list[tuple] = []
        self.session: RewardSession | None = None
        self.store: RewardStore | None = None
        self.pause: asyncio.Event | None = None
        self.paused = asyncio.Event()

    def guard_mutation(self, guard_phone, payload=None):
        if guard_phone in self.blocked:
            raise IikoMutationBlocked(f"staging only allows mutations for someone else, got {guard_phone!r}")

    async def topup_wallet(self, *, customer_id, wallet_id, amount, comment, guard_phone, db=None):
        assert self.session is not None and not self.session.in_transaction
        side = referral_service.REFERRED_SIDE if customer_id == REFERRED_CUSTOMER else referral_service.REFERRER_SIDE
        assert getattr(self.store.reward, side.started_at) is not None, "side must be claimed before calling iiko"
        self.calls.append((customer_id, wallet_id, amount, guard_phone, comment))
        if self.pause is not None and customer_id == REFERRED_CUSTOMER:
            self.paused.set()
            await self.pause.wait()
        if customer_id in self.failures:
            raise self.failures.pop(customer_id)
        return {}


REWARDS_ON = {"referral_rewards_enabled": True, "referral_bonus_amount": 300, "loyalty_bonus_wallet_id": WALLET}


@pytest.fixture
def reward_env(monkeypatch, make_settings):
    def setup(store: RewardStore, iiko=None, **settings_overrides):
        store.install(monkeypatch)
        session = RewardSession()
        client = iiko or TopupIiko()
        if isinstance(client, TopupIiko):
            client.session = session
            client.store = store
        user = _user(referred_by_id=uuid.uuid4(), referred_at=REFERRED_AT)
        settings = make_settings(**{**REWARDS_ON, **settings_overrides})

        async def run():
            await process_referral_reward(session, client, user, settings)

        return run, client, session

    return setup


def _customers(iiko: TopupIiko) -> list[str]:
    return [call[0] for call in iiko.calls]


class TestRewards:
    async def test_first_qualifying_sync_tops_up_both_once(self, reward_env):
        store = RewardStore()
        run, iiko, session = reward_env(store)
        await run()
        assert store.created == [Decimal("300.00")]
        assert iiko.calls == [
            (REFERRED_CUSTOMER, WALLET, Decimal("300.00"), REFERRED_PHONE, referral_service.REFERRED_COMMENT),
            (REFERRER_CUSTOMER, WALLET, Decimal("300.00"), REFERRER_PHONE, referral_service.REFERRER_COMMENT),
        ]
        assert store.reward.referred_done and store.reward.referrer_done
        assert store.reward.attempts == 0
        assert session.in_transaction is False
        await run()
        await run()
        assert len(iiko.calls) == 2

    async def test_disabled_does_nothing(self, reward_env):
        store = RewardStore()
        run, iiko, _ = reward_env(store, referral_rewards_enabled=False)
        await run()
        assert store.touched is False
        assert iiko.calls == []

    async def test_user_without_referrer_does_nothing(self, monkeypatch, make_settings):
        store = RewardStore()
        store.install(monkeypatch)
        iiko = TopupIiko()
        iiko.session = RewardSession()
        await process_referral_reward(iiko.session, iiko, _user(), make_settings(**REWARDS_ON))
        assert store.touched is False
        assert iiko.calls == []

    async def test_needs_a_purchase_after_the_code_was_applied(self, reward_env):
        store = RewardStore(qualifying=False)
        run, iiko, _ = reward_env(store)
        await run()
        assert store.created == []
        assert iiko.calls == []

    async def test_zero_amount_creates_nothing(self, reward_env):
        store = RewardStore()
        run, iiko, _ = reward_env(store, referral_bonus_amount=0)
        await run()
        assert store.created == []
        assert iiko.calls == []

    async def test_amount_is_frozen_when_reward_is_created(self, reward_env):
        store = RewardStore(_reward(amount=Decimal("150.00")))
        run, iiko, _ = reward_env(store, referral_bonus_amount=999)
        await run()
        assert [call[2] for call in iiko.calls] == [Decimal("150.00"), Decimal("150.00")]

    async def test_done_side_is_never_repeated(self, reward_env):
        store = RewardStore(_reward(referred_done=True))
        run, iiko, _ = reward_env(store)
        await run()
        assert _customers(iiko) == [REFERRER_CUSTOMER]
        assert store.reward.referrer_done is True

    async def test_known_failure_releases_the_side_and_retries_only_it(self, reward_env):
        store = RewardStore()
        failure = IikoClientError("iiko topup responded 400", 400, {"errorDescription": "bad wallet"})
        run, iiko, session = reward_env(store, TopupIiko({REFERRER_CUSTOMER: failure}))
        await run()
        assert store.reward.referred_done is True
        assert store.reward.referrer_done is False
        assert store.reward.referrer_started_at is None
        assert store.reward.attempts == 1
        assert "400" in store.reward.last_error
        assert session.in_transaction is False
        await run()
        assert _customers(iiko) == [REFERRED_CUSTOMER, REFERRER_CUSTOMER, REFERRER_CUSTOMER]
        assert store.reward.referrer_done is True
        assert store.reward.last_error is None

    async def test_stops_after_max_attempts(self, reward_env):
        store = RewardStore(_reward(attempts=REWARD_MAX_ATTEMPTS))
        run, iiko, _ = reward_env(store)
        await run()
        assert iiko.calls == []

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_server_error_is_unknown_and_never_retried(self, reward_env, status):
        store = RewardStore()
        failure = IikoClientError(f"iiko topup responded {status}", status)
        run, iiko, _ = reward_env(store, TopupIiko({REFERRED_CUSTOMER: failure}))
        await run()
        await run()
        assert _customers(iiko) == [REFERRED_CUSTOMER]
        assert store.reward.attempts == REWARD_MAX_ATTEMPTS
        assert store.reward.referred_started_at is not None
        assert "неизвестен" in store.reward.last_error

    async def test_unknown_outcome_is_never_retried(self, reward_env):
        store = RewardStore()
        timeout = IikoClientError("iiko topup request failed: timed out")
        timeout.__cause__ = httpx.ReadTimeout("timed out")
        run, iiko, _ = reward_env(store, TopupIiko({REFERRED_CUSTOMER: timeout}))
        await run()
        assert store.reward.referred_done is False
        assert store.reward.attempts == REWARD_MAX_ATTEMPTS
        assert "неизвестен" in store.reward.last_error
        await run()
        assert _customers(iiko) == [REFERRED_CUSTOMER]

    async def test_concurrent_syncs_pay_each_side_once(self, reward_env):
        store = RewardStore(_reward())
        iiko = TopupIiko()
        iiko.pause = asyncio.Event()
        run, _, _ = reward_env(store, iiko)
        first = asyncio.create_task(run())
        await iiko.paused.wait()
        await run()
        iiko.pause.set()
        await first
        assert sorted(_customers(iiko)) == sorted([REFERRED_CUSTOMER, REFERRER_CUSTOMER])
        assert store.reward.referred_done and store.reward.referrer_done

    async def test_crash_after_iiko_accepted_never_pays_again(self, reward_env):
        store = RewardStore(_reward())
        store.fail_finish = True
        run, iiko, _ = reward_env(store)
        with pytest.raises(ConnectionError):
            await run()
        store.fail_finish = False
        await run()
        assert _customers(iiko) == [REFERRED_CUSTOMER, REFERRER_CUSTOMER]
        assert store.reward.referred_done is False
        assert store.reward.attempts == 0
        store.reward.referred_started_at = datetime.now(timezone.utc) - referral_service.REWARD_CLAIM_TIMEOUT
        await run()
        await run()
        assert _customers(iiko) == [REFERRED_CUSTOMER, REFERRER_CUSTOMER]
        assert store.reward.attempts == REWARD_MAX_ATTEMPTS
        assert store.reward.last_error == referral_service.INTERRUPTED

    async def test_both_guests_must_be_linked(self, reward_env):
        store = RewardStore(parties=RewardParties(REFERRED_CUSTOMER, REFERRER_PHONE, None))
        run, iiko, _ = reward_env(store)
        await run()
        assert iiko.calls == []
        assert store.reward.attempts == 0
        assert "привязаны" in store.reward.last_error

    async def test_requires_explicit_bonus_wallet(self, reward_env):
        store = RewardStore()
        run, iiko, _ = reward_env(store, loyalty_bonus_wallet_id="", coffee_stamp_wallet_id="coffee-wallet")
        await run()
        await run()
        assert iiko.calls == []
        assert store.reward.attempts == 0
        assert store.reward.last_error == referral_service.NO_BONUS_WALLET
        assert not (store.reward.referred_started_at or store.reward.referrer_started_at)

    async def test_demo_referrer_is_never_topped_up(self, reward_env):
        store = RewardStore()
        run, iiko, _ = reward_env(
            store, review_bypass_phone=REFERRER_PHONE, review_demo_card_number="7777000011112222"
        )
        await run()
        await run()
        assert iiko.calls == []
        assert store.reward.attempts == REWARD_MAX_ATTEMPTS
        assert store.reward.last_error == referral_service.DEMO_REFERRER

    async def test_blocked_side_does_not_stop_the_other_or_use_attempts(self, reward_env):
        store = RewardStore()
        run, iiko, _ = reward_env(store, TopupIiko(blocked={REFERRED_PHONE}))
        for _ in range(REWARD_MAX_ATTEMPTS + 2):
            await run()
        assert _customers(iiko) == [REFERRER_CUSTOMER]
        assert store.reward.referrer_done is True
        assert store.reward.referred_done is False
        assert store.reward.referred_started_at is None
        assert store.reward.attempts == 0
        assert "staging" in store.reward.last_error
        iiko.blocked.clear()
        await run()
        assert _customers(iiko) == [REFERRER_CUSTOMER, REFERRED_CUSTOMER]
        assert store.reward.referred_done is True


class StatementSession:
    def __init__(self, result=None):
        self.result = result
        self.statements: list = []
        self.commits = 0

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.result

    async def commit(self):
        self.commits += 1


class TestRewardStatements:
    async def test_claim_is_a_conditional_update(self):
        reward_id = uuid.uuid4()
        db = StatementSession(reward_id)
        assert await referral_service._claim_side(db, reward_id, referral_service.REFERRED_SIDE) is True
        sql = _sql(db.statements[0])
        assert "SET referred_started_at=now()" in sql
        assert "referral_rewards.referred_done IS false" in sql
        assert "referral_rewards.referred_started_at IS NULL" in sql
        assert db.commits == 1
        assert await referral_service._claim_side(StatementSession(None), reward_id, referral_service.REFERRER_SIDE) is False

    async def test_qualifying_purchase_must_follow_the_application(self):
        db = StatementSession(True)
        user = _user(referred_by_id=uuid.uuid4(), referred_at=REFERRED_AT)
        assert await referral_service._has_qualifying_purchase(db, user) is True
        assert "purchases.ordered_at >=" in _sql(db.statements[0])
        assert REFERRED_AT in _params(db.statements[0])

    async def test_counted_failure_increments_attempts_in_sql(self):
        db = StatementSession(uuid.uuid4())
        await referral_service._record_result(db, uuid.uuid4(), "boom", counted=True)
        assert "attempts=(referral_rewards.attempts +" in _sql(db.statements[0])


class FakeHttpIiko:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/api/1/access_token":
            return httpx.Response(200, json={"token": "token-1"})
        self.calls.append((request.url.path, body))
        return httpx.Response(200, json={})


def _http_client(settings, fake: FakeHttpIiko) -> IikoCloudClient:
    client = IikoCloudClient(settings)
    client._http = httpx.AsyncClient(base_url="https://iiko.invalid", transport=httpx.MockTransport(fake))
    client._retry_delays = (0.0, 0.0, 0.0)
    return client


class TestStagingGuard:
    async def test_staging_tops_up_only_the_test_guest_even_as_referrer(self, reward_env, make_settings):
        fake = FakeHttpIiko()
        client = _http_client(make_settings(iiko_test_guest_phone=REFERRER_PHONE), fake)
        store = RewardStore()
        try:
            run, _, _ = reward_env(store, client)
            await run()
            await run()
        finally:
            await client.aclose()
        assert fake.calls == [
            (
                "/api/1/loyalty/iiko/customer/wallet/topup",
                {
                    "organizationId": "org-1",
                    "customerId": REFERRER_CUSTOMER,
                    "walletId": WALLET,
                    "sum": 300.0,
                    "comment": referral_service.REFERRER_COMMENT,
                },
            )
        ]
        assert store.reward.referrer_done is True
        assert store.reward.referred_done is False
        assert store.reward.attempts == 0
        assert "staging" in store.reward.last_error

    async def test_kill_switch_never_uses_attempts_or_claims(self, reward_env, make_settings):
        fake = FakeHttpIiko()
        client = _http_client(make_settings(iiko_mutations_enabled=False), fake)
        store = RewardStore()
        try:
            run, _, _ = reward_env(store, client)
            for _ in range(REWARD_MAX_ATTEMPTS + 1):
                await run()
        finally:
            await client.aclose()
        assert fake.calls == []
        assert store.reward.referred_done is False
        assert store.reward.referred_started_at is None
        assert store.reward.attempts == 0
        assert "kill switch" in store.reward.last_error


class TestOutcome:
    def test_blocked_is_known(self):
        assert outcome_unknown(IikoMutationBlocked("blocked")) is False

    @pytest.mark.parametrize(
        ("status", "unknown"),
        [(400, False), (409, False), (429, False), (500, True), (502, True), (503, True), (504, True)],
    )
    def test_http_statuses(self, status, unknown):
        assert outcome_unknown(IikoClientError("x", status)) is unknown

    def test_connect_errors_never_reached_iiko(self):
        exc = IikoClientError("connect failed")
        exc.__cause__ = httpx.ConnectError("refused")
        assert outcome_unknown(exc) is False

    def test_read_timeout_is_unknown(self):
        exc = IikoClientError("timed out")
        exc.__cause__ = httpx.ReadTimeout("timed out")
        assert outcome_unknown(exc) is True
