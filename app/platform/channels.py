"""Resolve WhatsApp (Cloud API and Baileys) senders to a platform principal.

A sender acts only as the user their verified `platform_channel_identities` row points to — never as
"whoever owns the connection" just because the number is on an allow-list. Unmapped numbers get no
data. `allowed_senders` remains a connection-level allow-list (the number must also be welcome on
that connection).

Step F slice 2 verification flow: a number is linked to an InfraSpine account only through
`request_sender_verification` (creates a pending, unverified row that grants nothing) followed by
`confirm_sender_verification` by the same account holder (marks the row verified). `sync_allowed_senders`
no longer mints `legacy:allowed_senders` identities for new numbers; legacy rows minted before slice 2
keep working until they are revoked. An explicit identity for a different user always wins.
Revoking the owner (or the identity) cuts the channel.
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PermissionDeniedError
from app.db.models import User
from app.platform.access import Principal, resolve_principal
from app.platform.models import PlatformChannelIdentity

LEGACY_METHOD = "legacy:allowed_senders"
PENDING_METHOD = "whatsapp:pending"
CONFIRMED_METHOD = "whatsapp:confirmed"
BAILEYS_SELF = "baileys_self_chat"
_DIGITS = re.compile(r"\D+")


def normalize_sender(value: str) -> str:
    """Digits-only phone, or a Baileys JID left intact so @lid / @s.whatsapp.net stay distinct."""
    text = (value or "").strip()
    if "@" in text:
        return text[:128]
    return _DIGITS.sub("", text)[:128]


def baileys_external_id(jid: str) -> str:
    return normalize_sender(jid)


async def link_identity(
    db: AsyncSession, user_id: uuid.UUID, channel: str, external_id: str, *,
    method: str, verified: bool = True,
) -> PlatformChannelIdentity:
    """Create or reuse a verified identity. Never silently reassigns a number already linked to someone else."""
    external_id = normalize_sender(external_id)
    if not external_id or channel not in {"whatsapp_cloud", "whatsapp_baileys", "api"}:
        raise PermissionDeniedError("This number is not linked to an InfraSpine account.")
    existing = await db.scalar(select(PlatformChannelIdentity).where(
        PlatformChannelIdentity.channel == channel, PlatformChannelIdentity.external_id == external_id,
        PlatformChannelIdentity.revoked_at.is_(None),
    ))
    if existing is not None:
        if existing.user_id != user_id:
            raise PermissionDeniedError("This number is already linked to another account.")
        if verified and existing.verified_at is None:
            existing.verified_at = datetime.now(UTC)
            existing.verification_method = method
        return existing
    row = PlatformChannelIdentity(
        user_id=user_id, channel=channel, external_id=external_id,
        verified_at=datetime.now(UTC) if verified else None, verification_method=method,
    )
    db.add(row)
    await db.flush()
    return row


async def request_sender_verification(
    db: AsyncSession, *, user_id: uuid.UUID, channel: str, external_id: str,
) -> PlatformChannelIdentity:
    """Start the Step F verification flow: the account holder asks to link a number to their account.

    Creates an UNVERIFIED identity row (`verified_at` is None, method `whatsapp:pending`). A pending
    row grants nothing — `resolve_principal` requires `verified_at` — until the same user confirms it
    with `confirm_sender_verification`. Idempotent: requesting again returns the existing row.
    Never silently reassigns a number already linked to someone else.
    """
    return await link_identity(db, user_id, channel, external_id, method=PENDING_METHOD, verified=False)


async def confirm_sender_verification(
    db: AsyncSession, *, user_id: uuid.UUID, channel: str, external_id: str,
) -> PlatformChannelIdentity:
    """Confirm a pending link: only the user who requested it may confirm it, for their own account.

    Sets `verified_at` and `verification_method="whatsapp:confirmed"`. Confirming an already-verified
    row is a no-op. A number linked to someone else, or with no link at all, is refused with a
    message safe to show (no account data).
    """
    external_id = normalize_sender(external_id)
    if not external_id or channel not in {"whatsapp_cloud", "whatsapp_baileys", "api"}:
        raise PermissionDeniedError("This number is not linked to an InfraSpine account.")
    row = await db.scalar(select(PlatformChannelIdentity).where(
        PlatformChannelIdentity.channel == channel, PlatformChannelIdentity.external_id == external_id,
        PlatformChannelIdentity.revoked_at.is_(None),
    ))
    if row is None or row.user_id != user_id:
        raise PermissionDeniedError("This number is not linked to an InfraSpine account.")
    if row.verified_at is None:
        row.verified_at = datetime.now(UTC)
        row.verification_method = CONFIRMED_METHOD
        await db.flush()
    return row


async def revoke_sender_identity(
    db: AsyncSession, *, user_id: uuid.UUID, channel: str, external_id: str,
) -> PlatformChannelIdentity:
    """Revoke one of the caller's own sender links. A revoked number resolves to nothing, even if it
    is still on a connection allow-list, until the number's owner requests verification again."""
    external_id = normalize_sender(external_id)
    if not external_id or channel not in {"whatsapp_cloud", "whatsapp_baileys", "api"}:
        raise PermissionDeniedError("This number is not linked to an InfraSpine account.")
    row = await db.scalar(select(PlatformChannelIdentity).where(
        PlatformChannelIdentity.channel == channel, PlatformChannelIdentity.external_id == external_id,
        PlatformChannelIdentity.revoked_at.is_(None),
    ))
    if row is None or row.user_id != user_id:
        raise PermissionDeniedError("This number is not linked to an InfraSpine account.")
    row.revoked_at = datetime.now(UTC)
    await db.flush()
    return row


async def sync_allowed_senders(
    db: AsyncSession, *, owner_id: uuid.UUID, channel: str, allowed: list[str],
) -> None:
    """Revoke legacy allow-list identities whose numbers left the connection.

    Step F slice 2: this no longer mints `legacy:allowed_senders` identities for new numbers — a
    number acts only after its owner links it through the verification flow
    (`request_sender_verification` + `confirm_sender_verification`). Legacy rows minted before
    slice 2 keep working until they are revoked here (number left the allow-list) or by
    `revoke_user_access` / `revoke_sender_identity`. Explicit (non-legacy) links are left alone.
    """
    wanted = {normalize_sender(item) for item in allowed if normalize_sender(item)}
    rows = list(await db.scalars(select(PlatformChannelIdentity).where(
        PlatformChannelIdentity.channel == channel, PlatformChannelIdentity.user_id == owner_id,
        PlatformChannelIdentity.revoked_at.is_(None),
        PlatformChannelIdentity.verification_method == LEGACY_METHOD,
    )))
    now = datetime.now(UTC)
    for row in rows:
        if row.external_id not in wanted:
            row.revoked_at = now
    await db.flush()


async def resolve_whatsapp_sender(
    db: AsyncSession, *, channel: str, sender: str, allowed: list[str] | None = None,
    owner_id: uuid.UUID | None = None, auto_link_owner: bool = False,
) -> Principal:
    """Resolve a sender, or raise PermissionDeniedError with a message safe to show (no account data)."""
    external_id = normalize_sender(sender)
    if not external_id:
        raise PermissionDeniedError("This number is not linked to an InfraSpine account.")
    allowed_norm = {normalize_sender(item) for item in (allowed or []) if normalize_sender(item)}
    if allowed is not None and external_id not in allowed_norm and sender not in (allowed or []):
        raise PermissionDeniedError("This number is not linked to an InfraSpine account.")
    try:
        return await resolve_principal(db, channel=channel, external_id=external_id)
    except PermissionDeniedError:
        pass
    # Also try the raw sender (Baileys JIDs already normalized; Cloud API may have been stored with punctuation).
    if sender != external_id:
        try:
            return await resolve_principal(db, channel=channel, external_id=sender)
        except PermissionDeniedError:
            pass
    if auto_link_owner and owner_id is not None:
        user = await db.get(User, owner_id)
        if user is not None and user.is_active:
            await link_identity(db, owner_id, channel, external_id, method=BAILEYS_SELF if channel == "whatsapp_baileys" else LEGACY_METHOD)
            return await resolve_principal(db, channel=channel, external_id=external_id)
    raise PermissionDeniedError("This number is not linked to an InfraSpine account.")
