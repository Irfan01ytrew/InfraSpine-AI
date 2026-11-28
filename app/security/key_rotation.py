"""Master-key rotation support (Step I, slice 5).

Why this module exists
----------------------
``CredentialCipher`` seals provider credentials with a single master key
(``MASTER_ENCRYPTION_KEY``, identified by ``MASTER_ENCRYPTION_KEY_ID``) and
*refuses* to decrypt any ciphertext whose ``key_id`` it does not know. That is
the right default, but it makes rotating the master key impossible: the moment
the key changes, every stored credential becomes undecryptable and the app
breaks.

Rotation therefore needs two pieces, both in this slice:

1. :class:`app.core.crypto.RotatingCredentialCipher` — decrypts with the
   current key *or* any retired key, encrypts only with the current key.
   Deploy the new key as current with the old key retired, re-encrypt stored
   values at leisure, then drop the retired key. At no point is anything
   undecryptable.
2. :func:`rotate_provider_credentials` — the sweep that re-encrypts stored
   provider credentials under the current key, one row at a time, with an
   audit event per row (``provider.credential_reencrypted``). It never prints
   or logs a secret, never deletes a row, and leaves any row it cannot
   decrypt exactly as it was (reported under ``skipped``) — the same
   fail-open-per-row, fail-closed-overall behaviour as
   ``rebind_legacy_credentials``.

What this module deliberately does NOT do
-----------------------------------------
* It does not rotate anything itself: the owner changes the environment
  (see ``docs/step-i-key-rotation-runbook.md``) and runs the sweep.
* It does not touch the database credential exposed in older docs. That
  credential can only be rotated by the owner (new role/password, then update
  the connection string); the runbook gives the exact steps.
* It does not re-encrypt the JSON sealed payloads (integrations, workflows,
  agent runs) automatically — those are sealed under ad-hoc ``{"ct","iv",
  "kid"}`` dicts scattered across many rows, and a blind sweep would need to
  know each payload's associated data. :func:`reencrypt_sealed_json` is the
  reusable helper for those stores; a store owner calls it with the right
  associated data. ``SEALED_STORES`` inventories every store and its status.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import DecryptionError, RotatingCredentialCipher, credential_aad
from app.db.models import ProviderConfig, ProviderCredential
from app.services import audit
from app.services.audit import AuditContext
from app.services.provider_admin import (
    CREDENTIAL_ACTIVE,
    CREDENTIAL_PENDING,
    SYSTEM_AUDIT_CONTEXT,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inventory of sealed stores (runbook §1.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SealedStore:
    """One place sealed values live, and how rotation handles it."""

    name: str
    status: str  # "supported" | "manual"
    detail: str


SEALED_STORES: tuple[SealedStore, ...] = (
    SealedStore(
        name="provider_credentials",
        status="supported",
        detail=(
            "Provider credentials (LLM keys, WhatsApp token, …). "
            "Re-encrypted by rotate_provider_credentials()."
        ),
    ),
    SealedStore(
        name="integration sealed payloads (JSON ct/iv/kid)",
        status="manual",
        detail=(
            "app/integrations/security.py seal/unseal; payloads live inside "
            "integration rows. Re-encrypt with reencrypt_sealed_json() and "
            "the payload's own associated data — no blind sweep exists."
        ),
    ),
    SealedStore(
        name="workflow sealed payloads (JSON ct/iv/kid)",
        status="manual",
        detail=(
            "app/workflows/security.py seal/unseal. Same as integrations: "
            "re-encrypt with reencrypt_sealed_json() per payload."
        ),
    ),
    SealedStore(
        name="agent-run sealed task/result",
        status="manual",
        detail=(
            "app/models AgentProfileRun task/result ciphertext columns. "
            "Historical run records; re-encrypt only if a run must stay "
            "readable after the retired key is dropped, otherwise they age "
            "out with run retention."
        ),
    ),
    SealedStore(
        name="agent_studio_baileys sealed payloads",
        status="manual",
        detail=(
            "app/services/agent_studio_baileys.py seal/unseal. Same helper, "
            "per payload."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Reusable helper for the JSON sealed payloads
# ---------------------------------------------------------------------------


def reencrypt_sealed_json(
    value: dict,
    cipher: RotatingCredentialCipher,
    *,
    associated_data: bytes,
) -> dict | None:
    """Re-encrypt one ``{"ct", "iv", "kid"}`` dict under the current key.

    Returns the new dict when the payload was sealed under a retired key,
    ``None`` when it is already under the current key (no churn, no rewrite).
    Raises :class:`DecryptionError` when the payload cannot be decrypted at
    all — the caller must leave the stored value untouched in that case.
    """
    kid = value.get("kid")
    if kid == cipher.key_id:
        return None
    plaintext = cipher.decrypt_sealed_json(value, associated_data=associated_data)
    return cipher.encrypt_sealed_json(plaintext, associated_data=associated_data)


# ---------------------------------------------------------------------------
# Provider-credential sweep
# ---------------------------------------------------------------------------


@dataclass
class RotationResult:
    """Outcome of one :func:`rotate_provider_credentials` pass."""

    current_key_id: str
    reencrypted: int = 0
    already_current: int = 0
    skipped: list[int] = field(default_factory=list)  # credential ids left as-is
    failed: list[dict] = field(default_factory=list)  # {"id", "reason"}

    @property
    def done(self) -> bool:
        """True when nothing remains under a retired key and nothing failed."""
        return not self.skipped and not self.failed


async def count_stale_provider_credentials(
    db: AsyncSession, current_key_id: str
) -> int:
    """How many provider credentials are still sealed under a retired key.

    This is the verification query for the runbook: it must read 0 before the
    retired key may be removed from the environment.
    """
    result = await db.execute(
        select(ProviderCredential.id).where(
            ProviderCredential.status.in_([CREDENTIAL_ACTIVE, CREDENTIAL_PENDING]),
            ProviderCredential.key_id != current_key_id,
        )
    )
    return len(result.scalars().all())


async def rotate_provider_credentials(
    db: AsyncSession,
    cipher: RotatingCredentialCipher,
    *,
    limit: int = 100,
    audit_context: AuditContext = SYSTEM_AUDIT_CONTEXT,
) -> RotationResult:
    """Re-encrypt provider credentials sealed under a retired key.

    Selects active/pending credentials whose ``key_id`` is not the current
    key (``SKIP LOCKED`` so two sweeps never fight over the same row),
    decrypts each with the rotating cipher (succeeds for retired keys),
    re-encrypts it under the current key, updates the row, and audits
    ``provider.credential_reencrypted``. A row that cannot be decrypted is
    left exactly as it was and reported under ``skipped`` — never deleted,
    never re-written with a guess.

    The associated data is unchanged by rotation (the row's own
    ``endpoint_origin`` binding is preserved), so the credential keeps working
    exactly as before, only under the new key.
    """
    result = RotationResult(current_key_id=cipher.key_id)
    rows = (
        await db.execute(
            select(ProviderCredential, ProviderConfig)
            .join(
                ProviderConfig,
                ProviderConfig.id == ProviderCredential.provider_config_id,
            )
            .where(
                ProviderCredential.status.in_(
                    [CREDENTIAL_ACTIVE, CREDENTIAL_PENDING]
                ),
                ProviderCredential.key_id != cipher.key_id,
            )
            .with_for_update(of=ProviderCredential, skip_locked=True)
            .limit(limit)
        )
    ).all()

    for credential, config in rows:
        aad = credential_aad(
            credential.id, config.provider, credential.endpoint_origin
        )
        previous_key_id = credential.key_id
        try:
            plaintext = cipher.decrypt(credential, associated_data=aad)
        except DecryptionError as exc:
            logger.error(
                "Skipping credential %s during key rotation: %s",
                credential.id,
                type(exc).__name__,
            )
            result.skipped.append(credential.id)
            result.failed.append({"id": credential.id, "reason": str(exc)})
            continue
        new_value = cipher.encrypt(plaintext, associated_data=aad)
        credential.ciphertext, credential.nonce, credential.key_id = (
            new_value.ciphertext,
            new_value.nonce,
            new_value.key_id,
        )
        audit.record(
            db,
            audit_context,
            "provider.credential_reencrypted",
            target=config.provider,
            details={
                "credential_id": str(credential.id),
                "previous_key_id": previous_key_id,
                "current_key_id": cipher.key_id,
            },
        )
        result.reencrypted += 1

    await db.commit()
    return result
