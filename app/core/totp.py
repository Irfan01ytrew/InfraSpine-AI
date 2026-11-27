"""Small RFC 6238 TOTP implementation using only Python's standard library."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time

TOTP_PERIOD_SECONDS = 30
TOTP_DIGITS = 6
_WINDOW = 1


def new_totp_secret() -> str:
    """Generate a 160-bit base32 secret suitable for authenticator apps."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def totp_code(secret: str, counter: int, *, digits: int = TOTP_DIGITS) -> str:
    """Return an RFC 6238 code for a non-negative time-step counter."""
    if counter < 0 or not 6 <= digits <= 8:
        raise ValueError("Invalid TOTP counter or digit count.")
    padded = secret.upper() + "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(padded, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits)


def matching_totp_counter(secret: str, code: str, *, now: float | None = None, after_counter: int = -1) -> int | None:
    """Return the matching drift-window counter, rejecting replayed time steps."""
    if len(code) != TOTP_DIGITS or not code.isascii() or not code.isdigit():
        return None
    current = int((time.time() if now is None else now) // TOTP_PERIOD_SECONDS)
    for counter in range(max(0, current - _WINDOW), current + _WINDOW + 1):
        if counter <= after_counter:
            continue
        if hmac.compare_digest(totp_code(secret, counter), code):
            return counter
    return None


def new_recovery_codes(count: int = 10) -> list[str]:
    """Create high-entropy one-use codes; store only a hash of the normalized value."""
    if not 1 <= count <= 20:
        raise ValueError("Recovery-code count is outside the supported range.")
    codes: list[str] = []
    for _ in range(count):
        raw = secrets.token_hex(16).upper()
        codes.append("-".join(raw[index : index + 4] for index in range(0, len(raw), 4)))
    return codes


def normalize_recovery_code(code: str) -> str:
    return "".join(character for character in code.upper() if character not in " -")


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(normalize_recovery_code(code).encode("utf-8")).hexdigest()
