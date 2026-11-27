"""Login, server-side sessions and logout."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import CredentialCipher, EncryptedValue
from app.core.errors import AccountLockedError, ConflictError, InvalidInputError, InvalidLoginError
from app.core.security import (
    hash_password,
    hash_session_token,
    new_session_token,
    password_needs_rehash,
    verify_password,
)
from app.core.totp import hash_recovery_code, matching_totp_counter, new_recovery_codes, new_totp_secret
from app.db.models import User, UserMfaChallenge, UserMfaFactor, UserMfaRecoveryCode, UserSession
from app.services import audit
from app.services.audit import AuditContext

logger = logging.getLogger(__name__)
MFA_CHALLENGE_MINUTES = 5
MFA_MAX_ATTEMPTS = 5
MFA_RECOVERY_CODE_COUNT = 10


@dataclass(frozen=True)
class LoginResult:
    user: User
    session_token: str | None
    mfa_challenge: str | None
    mfa_enrollment_required: bool = False


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def login(
    db: AsyncSession, settings: Settings, email: str, password: str, ctx: AuditContext
) -> LoginResult:
    """Validate a password and either issue a session or a short-lived second-factor challenge."""
    now = datetime.now(UTC)
    email = normalize_email(email)
    user = (
        await db.execute(select(User).where(User.email == email).with_for_update())
    ).scalar_one_or_none()

    # Always run one Argon2 verification so timing doesn't reveal whether the account exists.
    password_ok = verify_password(user.password_hash if user else None, password)

    failure_reason = None
    if user is None:
        failure_reason = "unknown_user"
    elif not user.is_active:
        failure_reason = "inactive_user"
    elif user.locked_until is not None and user.locked_until > now:
        failure_reason = "locked"
    elif not password_ok:
        failure_reason = "bad_password"
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= settings.login_max_failed_attempts:
            user.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
            user.failed_login_attempts = 0
            failure_reason = "bad_password_locked"

    if failure_reason is not None:
        failure_ctx = AuditContext(
            actor_user_id=user.id if user else None,
            actor_email=email,
            ip_address=ctx.ip_address,
            request_id=ctx.request_id,
        )
        audit.record(db, failure_ctx, "auth.login", outcome="failure", details={"reason": failure_reason})
        await db.commit()
        logger.info("Login failed reason=%s", failure_reason)
        if failure_reason in ("locked", "bad_password_locked"):
            raise AccountLockedError()
        raise InvalidLoginError()

    assert user is not None
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    factor = await db.get(UserMfaFactor, user.id)
    if factor is not None and factor.enabled_at is not None:
        await db.execute(
            delete(UserMfaChallenge).where(
                UserMfaChallenge.user_id == user.id,
                (UserMfaChallenge.expires_at < now) | (UserMfaChallenge.consumed_at.is_not(None)),
            )
        )
        challenge_token = new_session_token()
        db.add(
            UserMfaChallenge(
                user_id=user.id,
                token_hash=hash_session_token(challenge_token),
                expires_at=now + timedelta(minutes=MFA_CHALLENGE_MINUTES),
            )
        )
        audit.record(
            db,
            AuditContext(user.id, user.email, ctx.ip_address, ctx.request_id),
            "auth.mfa_challenge_issued",
        )
        await db.commit()
        return LoginResult(user=user, session_token=None, mfa_challenge=challenge_token)

    # Opportunistic cleanup of this user's expired sessions.
    user.failed_login_attempts = 0
    user.locked_until = None
    await db.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.expires_at < now))
    token = _create_session(db, user, settings, now)
    audit.record(
        db,
        AuditContext(user.id, user.email, ctx.ip_address, ctx.request_id),
        "auth.login",
    )
    await db.commit()
    return LoginResult(
        user=user,
        session_token=token,
        mfa_challenge=None,
        mfa_enrollment_required=settings.org_mfa_required,
    )


def _create_session(
    db: AsyncSession,
    user: User,
    settings: Settings,
    now: datetime,
    *,
    mfa_verified_at: datetime | None = None,
) -> str:
    token = new_session_token()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_session_token(token),
            expires_at=now + timedelta(minutes=settings.session_ttl_minutes),
            mfa_verified_at=mfa_verified_at,
        )
    )
    return token


def _mfa_aad(user_id) -> bytes:
    return f"infraspine:user-mfa:v1:{user_id}".encode("ascii")


def _decrypt_factor(user_id, factor: UserMfaFactor, cipher: CredentialCipher) -> str:
    return cipher.decrypt(
        EncryptedValue(factor.secret_ciphertext, factor.secret_nonce, factor.key_id),
        associated_data=_mfa_aad(user_id),
    )


async def begin_mfa_enrollment(
    db: AsyncSession, user: User, password: str, cipher: CredentialCipher, ctx: AuditContext
) -> tuple[str, str]:
    if not verify_password(user.password_hash, password):
        raise InvalidLoginError()
    factor = (
        await db.execute(select(UserMfaFactor).where(UserMfaFactor.user_id == user.id).with_for_update())
    ).scalar_one_or_none()
    if factor is not None and factor.enabled_at is not None:
        raise ConflictError("Multi-factor authentication is already enabled for this account.")

    secret = new_totp_secret()
    encrypted = cipher.encrypt(secret, associated_data=_mfa_aad(user.id))
    if factor is None:
        factor = UserMfaFactor(
            user_id=user.id,
            secret_ciphertext=encrypted.ciphertext,
            secret_nonce=encrypted.nonce,
            key_id=encrypted.key_id,
            last_totp_counter=-1,
        )
        db.add(factor)
    else:
        factor.secret_ciphertext = encrypted.ciphertext
        factor.secret_nonce = encrypted.nonce
        factor.key_id = encrypted.key_id
        factor.last_totp_counter = -1
    await db.execute(delete(UserMfaRecoveryCode).where(UserMfaRecoveryCode.user_id == user.id))
    audit.record(db, ctx, "auth.mfa_enrollment_started")
    await db.commit()
    issuer = "InfraSpine AI"
    label = quote(f"{issuer}:{user.email}", safe="")
    uri = f"otpauth://totp/{label}?{urlencode({'secret': secret, 'issuer': issuer, 'algorithm': 'SHA1', 'digits': 6, 'period': 30})}"
    return secret, uri


async def confirm_mfa_enrollment(
    db: AsyncSession,
    user: User,
    code: str,
    cipher: CredentialCipher,
    current_session_token: str,
    ctx: AuditContext,
) -> list[str]:
    factor = (
        await db.execute(select(UserMfaFactor).where(UserMfaFactor.user_id == user.id).with_for_update())
    ).scalar_one_or_none()
    if factor is None or factor.enabled_at is not None:
        raise ConflictError("No multi-factor enrollment is waiting for confirmation.")
    secret = _decrypt_factor(user.id, factor, cipher)
    now = datetime.now(UTC)
    counter = matching_totp_counter(secret, code, now=now.timestamp(), after_counter=factor.last_totp_counter)
    if counter is None:
        raise InvalidInputError("Enter the current six-digit code from your authenticator app.")
    factor.enabled_at = now
    factor.last_totp_counter = counter
    session = (
        await db.execute(
            select(UserSession)
            .where(
                UserSession.user_id == user.id,
                UserSession.token_hash == hash_session_token(current_session_token),
                UserSession.revoked_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if session is None:
        raise InvalidLoginError()
    # Successful factor proof during enrollment authorizes this exact session.
    session.mfa_verified_at = now
    recovery_codes = new_recovery_codes(MFA_RECOVERY_CODE_COUNT)
    await db.execute(delete(UserMfaRecoveryCode).where(UserMfaRecoveryCode.user_id == user.id))
    db.add_all(
        [UserMfaRecoveryCode(user_id=user.id, code_hash=hash_recovery_code(value)) for value in recovery_codes]
    )
    audit.record(db, ctx, "auth.mfa_enabled", details={"recovery_codes_issued": len(recovery_codes)})
    await db.commit()
    return recovery_codes


async def mfa_status(db: AsyncSession, user: User) -> tuple[bool, int]:
    factor = await db.get(UserMfaFactor, user.id)
    remaining = await db.scalar(
        select(func.count()).select_from(UserMfaRecoveryCode).where(
            UserMfaRecoveryCode.user_id == user.id,
            UserMfaRecoveryCode.used_at.is_(None),
        )
    )
    return bool(factor and factor.enabled_at is not None), int(remaining or 0)


async def mfa_policy_state(db: AsyncSession, user: User, token: str, settings: Settings) -> tuple[bool, bool]:
    """Return whether this user must enroll or this session must reauthenticate."""
    if not settings.org_mfa_required:
        return False, False
    state = (
        await db.execute(
            select(UserMfaFactor.enabled_at, UserSession.mfa_verified_at)
            .select_from(UserSession)
            .outerjoin(UserMfaFactor, UserMfaFactor.user_id == UserSession.user_id)
            .where(
                UserSession.user_id == user.id,
                UserSession.token_hash == hash_session_token(token),
                UserSession.revoked_at.is_(None),
            )
        )
    ).one_or_none()
    if state is None:
        return False, True
    factor_enabled_at, session_verified_at = state
    if factor_enabled_at is None:
        return True, False
    return False, session_verified_at is None


async def _check_factor_code(
    db: AsyncSession,
    user: User,
    factor: UserMfaFactor,
    code: str,
    cipher: CredentialCipher,
    now: datetime,
    *,
    allow_recovery: bool,
) -> bool:
    if factor.enabled_at is None:
        return False
    secret = _decrypt_factor(user.id, factor, cipher)
    counter = matching_totp_counter(secret, code, now=now.timestamp(), after_counter=factor.last_totp_counter)
    if counter is not None:
        factor.last_totp_counter = counter
        return True
    if not allow_recovery:
        return False
    normalized = code.strip().replace("-", "").replace(" ", "").upper()
    if len(normalized) != 32 or any(character not in "0123456789ABCDEF" for character in normalized):
        return False
    recovery = (
        await db.execute(
            select(UserMfaRecoveryCode)
            .where(
                UserMfaRecoveryCode.user_id == user.id,
                UserMfaRecoveryCode.code_hash == hash_recovery_code(normalized),
                UserMfaRecoveryCode.used_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if recovery is None:
        return False
    recovery.used_at = now
    return True


async def complete_mfa_login(
    db: AsyncSession,
    settings: Settings,
    challenge_token: str,
    code: str,
    cipher: CredentialCipher,
    ctx: AuditContext,
) -> tuple[User, str]:
    now = datetime.now(UTC)
    challenge = (
        await db.execute(
            select(UserMfaChallenge)
            .where(UserMfaChallenge.token_hash == hash_session_token(challenge_token))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        challenge is None
        or challenge.consumed_at is not None
        or challenge.expires_at <= now
        or challenge.attempts >= MFA_MAX_ATTEMPTS
    ):
        raise InvalidLoginError()
    user = (
        await db.execute(select(User).where(User.id == challenge.user_id).with_for_update())
    ).scalar_one_or_none()
    factor = (
        await db.execute(select(UserMfaFactor).where(UserMfaFactor.user_id == challenge.user_id).with_for_update())
    ).scalar_one_or_none()
    if user is None or not user.is_active or factor is None:
        challenge.consumed_at = now
        await db.commit()
        raise InvalidLoginError()

    if not await _check_factor_code(db, user, factor, code, cipher, now, allow_recovery=True):
        challenge.attempts += 1
        if challenge.attempts >= MFA_MAX_ATTEMPTS:
            challenge.consumed_at = now
        user.failed_login_attempts += 1
        locked = user.failed_login_attempts >= settings.login_max_failed_attempts
        if locked:
            user.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
            user.failed_login_attempts = 0
        audit.record(
            db,
            AuditContext(user.id, user.email, ctx.ip_address, ctx.request_id),
            "auth.mfa_verification",
            outcome="failure",
            details={"attempt": challenge.attempts, "account_locked": locked},
        )
        await db.commit()
        if locked:
            raise AccountLockedError()
        raise InvalidLoginError()

    challenge.consumed_at = now
    user.failed_login_attempts = 0
    user.locked_until = None
    token = _create_session(db, user, settings, now, mfa_verified_at=now)
    audit.record(db, AuditContext(user.id, user.email, ctx.ip_address, ctx.request_id), "auth.login")
    await db.commit()
    return user, token


async def rotate_mfa_recovery_codes(
    db: AsyncSession, user: User, password: str, code: str, cipher: CredentialCipher, ctx: AuditContext
) -> list[str]:
    if not verify_password(user.password_hash, password):
        raise InvalidLoginError()
    factor = (
        await db.execute(select(UserMfaFactor).where(UserMfaFactor.user_id == user.id).with_for_update())
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if factor is None or not await _check_factor_code(db, user, factor, code, cipher, now, allow_recovery=False):
        raise InvalidLoginError()
    codes = new_recovery_codes(MFA_RECOVERY_CODE_COUNT)
    await db.execute(delete(UserMfaRecoveryCode).where(UserMfaRecoveryCode.user_id == user.id))
    db.add_all([UserMfaRecoveryCode(user_id=user.id, code_hash=hash_recovery_code(value)) for value in codes])
    audit.record(db, ctx, "auth.mfa_recovery_codes_rotated", details={"recovery_codes_issued": len(codes)})
    await db.commit()
    return codes


async def disable_mfa(
    db: AsyncSession,
    user: User,
    password: str,
    code: str,
    cipher: CredentialCipher,
    current_session_token: str,
    ctx: AuditContext,
) -> None:
    if not verify_password(user.password_hash, password):
        raise InvalidLoginError()
    factor = (
        await db.execute(select(UserMfaFactor).where(UserMfaFactor.user_id == user.id).with_for_update())
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if factor is None or not await _check_factor_code(db, user, factor, code, cipher, now, allow_recovery=True):
        raise InvalidLoginError()
    await db.execute(delete(UserMfaFactor).where(UserMfaFactor.user_id == user.id))
    await db.execute(delete(UserMfaRecoveryCode).where(UserMfaRecoveryCode.user_id == user.id))
    await db.execute(delete(UserMfaChallenge).where(UserMfaChallenge.user_id == user.id))
    await db.execute(
        update(UserSession)
        .where(
            UserSession.user_id == user.id,
            UserSession.revoked_at.is_(None),
            UserSession.token_hash != hash_session_token(current_session_token),
        )
        .values(revoked_at=now)
    )
    audit.record(db, ctx, "auth.mfa_disabled")
    await db.commit()


async def set_password(db: AsyncSession, email: str, password: str, ctx: AuditContext) -> bool:
    """Reset a password, clear any lockout and revoke the user's existing sessions."""
    user = (
        await db.execute(select(User).where(User.email == normalize_email(email)).with_for_update())
    ).scalar_one_or_none()
    if user is None:
        return False
    user.password_hash = hash_password(password)
    user.failed_login_attempts = 0
    user.locked_until = None
    await db.execute(delete(UserSession).where(UserSession.user_id == user.id))
    audit.record(db, ctx, "auth.password_reset", target=user.email)
    await db.commit()
    return True


async def resolve_session(db: AsyncSession, token: str) -> User | None:
    now = datetime.now(UTC)
    return (
        await db.execute(
            select(User)
            .join(UserSession, UserSession.user_id == User.id)
            .where(
                UserSession.token_hash == hash_session_token(token),
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
                User.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def logout(db: AsyncSession, token: str, ctx: AuditContext) -> None:
    session = (
        await db.execute(
            select(UserSession).where(
                UserSession.token_hash == hash_session_token(token), UserSession.revoked_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if session is not None:
        session.revoked_at = datetime.now(UTC)
        audit.record(db, ctx, "auth.logout")
        await db.commit()
