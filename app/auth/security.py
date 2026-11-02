"""Password hashing (Argon2id), session tokens and CSRF tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# argon2-cffi defaults to Argon2id with RFC 9106 "low memory" parameters (64 MiB, t=3, p=4),
# a sensible cost on a 4-core / 16 GB box.
_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(16))


def burn_verify_time(password: str) -> None:
    """Run one verification against a throwaway hash so unknown emails cost the same time."""
    verify_password(_dummy_hash(), password)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def csrf_token_for(session_token: str, secret_key: str) -> str:
    """CSRF token bound to the session: HMAC(SECRET_KEY, session token)."""
    return hmac.new(secret_key.encode(), b"csrf:" + session_token.encode(), "sha256").hexdigest()
