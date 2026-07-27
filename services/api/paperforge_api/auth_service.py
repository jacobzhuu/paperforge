from __future__ import annotations

import hashlib
import secrets
import unicodedata
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19_456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)
_DUMMY_LOGIN_HASH = _PASSWORD_HASHER.hash("paperforge login timing placeholder")

# Kept offline so registration never sends a password or its prefix to a third party.
_COMMON_PASSWORDS = frozenset(
    {
        "passwordpassword",
        "password123456",
        "123456789012345",
        "qwertyuiop12345",
        "letmeinletmein",
        "administrator123",
        "paperforgepaperforge",
    }
)


def normalize_password(password: str) -> str:
    return unicodedata.normalize("NFC", password)


def validate_password(password: str) -> str:
    normalized = normalize_password(password)
    if len(normalized) < 8:
        raise ValueError("password must contain at least 8 characters")
    if len(normalized) > 128:
        raise ValueError("password must contain at most 128 characters")
    if normalized.casefold() in _COMMON_PASSWORDS:
        raise ValueError("password is too common")
    return normalized


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(validate_password(password))


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, normalize_password(password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def verify_login_password(password_hash: str | None, password: str) -> bool:
    """Always perform Argon2 verification, including for unknown/disabled accounts."""
    return verify_password(password_hash or _DUMMY_LOGIN_HASH, password)


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _PASSWORD_HASHER.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_expiry(days: int) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


def action_expiry(*, hours: int = 0, minutes: int = 0) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours, minutes=minutes)
