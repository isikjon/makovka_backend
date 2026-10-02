import uuid
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt
from starlette.requests import Request

from app.admin.security import (
    LoginThrottle,
    client_ip,
    create_session_token,
    csrf_token,
    decode_session_token,
    hash_password,
    login_csrf_token,
    session_fingerprint,
    verify_csrf,
    verify_login_csrf,
    verify_password,
    verify_password_or_decoy,
)
from app.security import create_access_token, decode_access_token

ADMIN_ID = uuid.uuid4()


class TestPasswordHashing:
    def test_roundtrip(self):
        stored = hash_password("correct horse battery")
        assert verify_password("correct horse battery", stored) is True
        assert verify_password("correct horse batterY", stored) is False

    def test_hash_is_salted_and_self_describing(self):
        first, second = hash_password("same-password"), hash_password("same-password")
        assert first != second
        scheme, n, r, p, salt, digest = first.split("$")
        assert (scheme, n, r, p) == ("scrypt", "16384", "8", "1")
        assert len(bytes.fromhex(salt)) == 16
        assert len(bytes.fromhex(digest)) == 32

    @pytest.mark.parametrize(
        "stored",
        [
            "",
            "plain-text",
            "bcrypt$16384$8$1$00$00",
            "scrypt$16384$8$1$zz$00",
            "scrypt$16385$8$1$00ff$00ff",
            "scrypt$1048576$8$1$00ff$00ff",
            "scrypt$16384$64$1$00ff$00ff",
            "scrypt$16384$8$1$$00ff",
        ],
    )
    def test_malformed_or_hostile_hashes_never_verify(self, stored):
        assert verify_password("anything", stored) is False

    def test_unknown_user_still_spends_hashing_time_and_fails(self):
        assert verify_password_or_decoy("password", None) is False
        assert verify_password_or_decoy("password", hash_password("password")) is True


class TestSessionFingerprint:
    def test_changes_with_password_and_session_version(self):
        base = session_fingerprint("hash-1", 0)
        assert session_fingerprint("hash-1", 0) == base
        assert session_fingerprint("hash-2", 0) != base
        assert session_fingerprint("hash-1", 1) != base

    def test_does_not_reveal_the_password_hash(self):
        assert "hash-1" not in session_fingerprint("hash-1", 0)


class TestSessionTokens:
    def test_roundtrip(self, make_settings):
        settings = make_settings()
        fingerprint = session_fingerprint("hash-1", 0)
        token = create_session_token(settings, ADMIN_ID, fingerprint)
        session = decode_session_token(settings, token)
        assert session.admin_id == ADMIN_ID
        assert session.fingerprint == fingerprint
        assert session.session_id

    def test_each_login_gets_its_own_session_id(self, make_settings):
        settings = make_settings()
        first = decode_session_token(settings, create_session_token(settings, ADMIN_ID, "hash-1"))
        second = decode_session_token(settings, create_session_token(settings, ADMIN_ID, "hash-1"))
        assert first.session_id != second.session_id

    def test_expires_after_twelve_hours(self, make_settings):
        settings = make_settings()
        issued = datetime.now(timezone.utc) - timedelta(hours=12, seconds=1)
        assert decode_session_token(settings, create_session_token(settings, ADMIN_ID, "h", issued)) is None
        fresh = datetime.now(timezone.utc) - timedelta(hours=11, minutes=59)
        assert decode_session_token(settings, create_session_token(settings, ADMIN_ID, "h", fresh)) is not None

    def test_app_access_token_is_not_an_admin_session(self, make_settings):
        settings = make_settings()
        assert decode_session_token(settings, create_access_token(ADMIN_ID)) is None

    def test_admin_session_is_not_an_app_access_token(self, make_settings):
        settings = make_settings()
        assert decode_access_token(create_session_token(settings, ADMIN_ID, "h")) is None

    def test_other_secret_or_tampering_is_rejected(self, make_settings):
        token = create_session_token(make_settings(jwt_secret="secret-a"), ADMIN_ID, "h")
        assert decode_session_token(make_settings(jwt_secret="secret-b"), token) is None
        assert decode_session_token(make_settings(jwt_secret="secret-a"), token[:-2] + "xx") is None

    def test_plain_jwt_signed_with_app_secret_is_rejected(self, make_settings):
        settings = make_settings()
        forged = jwt.encode(
            {
                "sub": str(ADMIN_ID),
                "aud": "makovka-admin",
                "type": "admin_session",
                "sid": "s",
                "pfp": "f",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            settings.jwt_secret,
            algorithm="HS256",
        )
        assert decode_session_token(settings, forged) is None


class TestCsrf:
    def test_token_is_bound_to_session(self, make_settings):
        settings = make_settings()
        token = csrf_token(settings, "session-a")
        assert verify_csrf(settings, "session-a", token) is True
        assert verify_csrf(settings, "session-b", token) is False

    @pytest.mark.parametrize("submitted", [None, "", "не-ascii-токен", 42, "0" * 64])
    def test_missing_or_garbage_tokens_fail_closed(self, make_settings, submitted):
        assert verify_csrf(make_settings(), "session-a", submitted) is False

    def test_login_token_requires_matching_cookie_nonce(self, make_settings):
        settings = make_settings()
        token = login_csrf_token(settings, "nonce-1")
        assert verify_login_csrf(settings, "nonce-1", token) is True
        assert verify_login_csrf(settings, "nonce-2", token) is False
        assert verify_login_csrf(settings, None, token) is False
        assert verify_login_csrf(settings, "", login_csrf_token(settings, "")) is False

    def test_login_and_session_tokens_are_not_interchangeable(self, make_settings):
        settings = make_settings()
        assert verify_csrf(settings, "x", login_csrf_token(settings, "x")) is False


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestLoginThrottle:
    def test_blocks_after_five_failures_within_fifteen_minutes(self):
        clock = Clock()
        throttle = LoginThrottle(clock=clock)
        for _ in range(4):
            throttle.record_failure("10.0.0.1")
            clock.now += 60
        assert throttle.blocked("10.0.0.1") is False
        throttle.record_failure("10.0.0.1")
        assert throttle.blocked("10.0.0.1") is True
        assert throttle.blocked("10.0.0.2") is False

    def test_failures_expire_after_window(self):
        clock = Clock()
        throttle = LoginThrottle(clock=clock)
        for _ in range(5):
            throttle.record_failure("10.0.0.1")
        clock.now += 15 * 60
        assert throttle.blocked("10.0.0.1") is False

    def test_sliding_window(self):
        clock = Clock()
        throttle = LoginThrottle(clock=clock)
        throttle.record_failure("ip")
        clock.now += 14 * 60
        for _ in range(4):
            throttle.record_failure("ip")
        assert throttle.blocked("ip") is True
        clock.now += 61
        assert throttle.blocked("ip") is False

    def test_reserved_attempts_count_before_the_outcome_is_known(self):
        throttle = LoginThrottle(clock=Clock())
        assert [throttle.reserve_attempt("ip") for _ in range(7)] == [True] * 5 + [False] * 2
        assert throttle.blocked("ip") is True
        assert throttle.reserve_attempt("other-ip") is True

    def test_success_resets(self):
        throttle = LoginThrottle(clock=Clock())
        for _ in range(4):
            throttle.record_failure("ip")
        throttle.reset("ip")
        throttle.record_failure("ip")
        assert throttle.blocked("ip") is False

    def test_memory_is_bounded(self):
        throttle = LoginThrottle(max_tracked=3, clock=Clock())
        for index in range(10):
            throttle.record_failure(f"ip-{index}")
        assert len(throttle._failures) == 3


def _request(peer: str, headers: dict[str, str] | None = None) -> Request:
    raw_headers = [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw_headers, "client": (peer, 5000)})


class TestClientIp:
    def test_uses_real_ip_behind_local_proxy(self):
        assert client_ip(_request("127.0.0.1", {"X-Real-IP": "203.0.113.7"})) == "203.0.113.7"

    def test_ignores_header_from_remote_peer(self):
        assert client_ip(_request("198.51.100.4", {"X-Real-IP": "203.0.113.7"})) == "198.51.100.4"

    def test_ignores_garbage_header(self):
        assert client_ip(_request("127.0.0.1", {"X-Real-IP": "evil, 1.2.3.4"})) == "127.0.0.1"

    def test_local_peer_without_header(self):
        assert client_ip(_request("127.0.0.1")) == "127.0.0.1"
