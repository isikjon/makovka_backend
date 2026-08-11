import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from jose import JWTError, jwt

from .config import get_settings

settings = get_settings()


def hash_otp_code(code: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.sha256(f"{salt}{code}".encode()).hexdigest()
    return f"{salt}${digest}"


def verify_otp_code(code: str, code_hash: str) -> bool:
    try:
        salt, digest = code_hash.split("$", 1)
    except ValueError:
        return False
    expected = hashlib.sha256(f"{salt}{code}".encode()).hexdigest()
    return hmac.compare_digest(expected, digest)


def generate_otp_code() -> str:
    return f"{secrets.randbelow(1000000):06d}"


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def create_access_token(user_id: UUID) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> UUID | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
    if payload.get("type") != "access":
        return None
    try:
        return UUID(payload["sub"])
    except (KeyError, ValueError):
        return None
