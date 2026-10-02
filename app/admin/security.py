from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import secrets
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import cache

from jose import JWTError, jwt
from starlette.requests import Request

from ..config import Settings

SESSION_COOKIE = "admin_session"
LOGIN_CSRF_COOKIE = "admin_login_csrf"
COOKIE_PATH = "/admin"
SESSION_TTL = timedelta(hours=12)
LOGIN_CSRF_TTL = timedelta(hours=1)
MAX_CONCURRENT_PASSWORD_CHECKS = 2

_SESSION_AUDIENCE = "makovka-admin"
_SESSION_TYPE = "admin_session"
_SESSION_ALGORITHM = "HS256"

_SCRYPT_SCHEME = "scrypt"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_SALT_BYTES = 16
_SCRYPT_MAXMEM = 128 * 1024 * 1024
_SCRYPT_MAX_N = 2**16
_SCRYPT_MAX_R = 16
_SCRYPT_MAX_P = 4
_PROXY_PEER = "127.0.0.1"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"{_SCRYPT_SCHEME}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def _scrypt_params_valid(n: int, r: int, p: int) -> bool:
    return 2 <= n <= _SCRYPT_MAX_N and n & (n - 1) == 0 and 1 <= r <= _SCRYPT_MAX_R and 1 <= p <= _SCRYPT_MAX_P


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n_raw, r_raw, p_raw, salt_hex, digest_hex = stored.split("$")
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    if scheme != _SCRYPT_SCHEME or not salt or not expected or not _scrypt_params_valid(n, r, p):
        return False
    try:
        digest = hashlib.scrypt(
            password.encode(), salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=_SCRYPT_MAXMEM
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(digest, expected)


@cache
def _decoy_hash() -> str:
    return hash_password(secrets.token_urlsafe(24))


def verify_password_or_decoy(password: str, stored: str | None) -> bool:
    if stored is None:
        verify_password(password, _decoy_hash())
        return False
    return verify_password(password, stored)


def generate_password() -> str:
    return secrets.token_urlsafe(18)


def _derived_key(settings: Settings, purpose: bytes) -> bytes:
    return hmac.new(settings.jwt_secret.encode(), b"makovka-admin:" + purpose, hashlib.sha256).digest()


def session_fingerprint(password_hash: str, session_version: int) -> str:
    return hashlib.sha256(f"{session_version}:{password_hash}".encode()).hexdigest()[:32]


@dataclass(frozen=True)
class AdminSession:
    admin_id: uuid.UUID
    session_id: str
    fingerprint: str


def create_session_token(
    settings: Settings, admin_id: uuid.UUID, fingerprint: str, now: datetime | None = None
) -> str:
    issued = now or datetime.now(timezone.utc)
    payload = {
        "sub": str(admin_id),
        "aud": _SESSION_AUDIENCE,
        "type": _SESSION_TYPE,
        "sid": secrets.token_urlsafe(24),
        "pfp": fingerprint,
        "iat": issued,
        "exp": issued + SESSION_TTL,
    }
    return jwt.encode(payload, _derived_key(settings, b"session"), algorithm=_SESSION_ALGORITHM)


def decode_session_token(settings: Settings, token: str) -> AdminSession | None:
    try:
        payload = jwt.decode(
            token,
            _derived_key(settings, b"session"),
            algorithms=[_SESSION_ALGORITHM],
            audience=_SESSION_AUDIENCE,
            options={"require_aud": True, "require_exp": True, "require_sub": True},
        )
    except JWTError:
        return None
    if payload.get("type") != _SESSION_TYPE:
        return None
    try:
        return AdminSession(uuid.UUID(payload["sub"]), str(payload["sid"]), str(payload["pfp"]))
    except (KeyError, TypeError, ValueError):
        return None


def _hmac_hex(settings: Settings, message: str) -> str:
    return hmac.new(_derived_key(settings, b"csrf"), message.encode(), hashlib.sha256).hexdigest()


def _matches(expected: str, submitted: object) -> bool:
    return isinstance(submitted, str) and hmac.compare_digest(expected.encode(), submitted.encode())


def csrf_token(settings: Settings, session_id: str) -> str:
    return _hmac_hex(settings, f"session:{session_id}")


def verify_csrf(settings: Settings, session_id: str, submitted: object) -> bool:
    return _matches(csrf_token(settings, session_id), submitted)


def new_login_nonce() -> str:
    return secrets.token_urlsafe(32)


def login_csrf_token(settings: Settings, nonce: str) -> str:
    return _hmac_hex(settings, f"login:{nonce}")


def verify_login_csrf(settings: Settings, nonce: str | None, submitted: object) -> bool:
    return bool(nonce) and _matches(login_csrf_token(settings, nonce), submitted)


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if peer == _PROXY_PEER:
        forwarded = request.headers.get("x-real-ip", "").strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer or "unknown"


class LoginThrottle:
    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: float = 15 * 60,
        max_tracked: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.max_tracked = max_tracked
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, key: str, now: float) -> deque[float]:
        failures = self._failures.get(key)
        if failures is None:
            return deque()
        while failures and now - failures[0] >= self.window_seconds:
            failures.popleft()
        if not failures:
            del self._failures[key]
        return failures

    def blocked(self, key: str) -> bool:
        return len(self._recent(key, self._clock())) >= self.max_failures

    def reserve_attempt(self, key: str) -> bool:
        if self.blocked(key):
            return False
        self.record_failure(key)
        return True

    def record_failure(self, key: str) -> None:
        now = self._clock()
        if key not in self._failures and len(self._failures) >= self.max_tracked:
            for tracked in list(self._failures):
                self._recent(tracked, now)
            if len(self._failures) >= self.max_tracked:
                self._failures.pop(next(iter(self._failures)))
        self._failures.setdefault(key, deque()).append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)

    def clear(self) -> None:
        self._failures.clear()


login_throttle = LoginThrottle()
password_checks = asyncio.Semaphore(MAX_CONCURRENT_PASSWORD_CHECKS)
