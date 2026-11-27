"""Password hashing and session token helpers."""

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

SESSION_COOKIE_NAME = "infraspine_session"
PARTNER_SESSION_COOKIE_NAME = "infraspine_partner_session"
CSRF_HEADER_NAME = "X-Requested-With"
MIN_PASSWORD_LENGTH = 8

_hasher = PasswordHasher()  # Argon2id with library-recommended parameters
# Verified against when the user does not exist, so response time doesn't reveal valid emails.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """Only the SHA-256 of a session token is stored, so a DB leak doesn't leak live sessions."""
    return hashlib.sha256(token.encode()).hexdigest()
