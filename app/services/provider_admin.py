"""Provider configuration lifecycle: configure -> stage verified key -> activate -> rotate.

Safety rules implemented here:
* A credential is only written to the database after a live provider request
  succeeds, and it is verified again immediately before activation.
* Network verification happens outside any row lock; the write then takes a
  row lock and re-checks the optimistic `version`, so concurrent admins get a
  409 instead of silently overwriting each other.
* A failed rotation never touches the currently active credential.
* Plaintext keys exist only in memory for the duration of a request.
* Custom providers: a stored key is bound (in its ciphertext) to the endpoint origin it was verified
  against and is never sent anywhere else. Changing the API base URL requires a newly supplied key,
  which replaces the stored ones; endpoints must pass the SSRF policy in `endpoint_security`.
"""

import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import CredentialCipher, DecryptionError, EncryptedValue, credential_aad
from app.core.errors import (
    ConflictError,
    CredentialUnreadableError,
    InvalidInputError,
    NotFoundError,
    ProviderVerificationFailedError,
)
from app.db.models import (
    CREDENTIAL_ACTIVE,
    CREDENTIAL_PENDING,
    CREDENTIAL_RETIRED,
    AuditLog,
    ProviderConfig,
    ProviderCredential,
)
from app.providers.endpoint_security import (
    Endpoint,
    UnsafeEndpointError,
    endpoint_origin,
    normalize_base_url,
    policy_from_settings,
    validate_endpoint,
)
from app.providers.registry import (
    ProviderDefinition,
    custom_provider_definition,
    get_provider,
    list_providers,
)
from app.schemas.providers import CredentialSummary, ProviderState, TestConnectionResponse
from app.services import audit
from app.services.audit import AuditContext
from app.services.llm import ProviderVerificationError
from app.services.llm_runtime import build_client, credential_origin, decrypt_credential
from app.services.processing_policy import model_allowed_for_content

logger = logging.getLogger(__name__)

SYSTEM_AUDIT_CONTEXT = AuditContext(actor_user_id=None, actor_email="system", ip_address=None, request_id=None)


@dataclass
class _Credentials:
    active: ProviderCredential | None = None
    pending: ProviderCredential | None = None


def credential_origin_or_none(base_url: str | None) -> str | None:
    try:
        return credential_origin(base_url)
    except DecryptionError:
        return None


def _key_hint(api_key: str) -> str:
    return api_key[-4:]


def _normalize_api_key(raw: str) -> str:
    api_key = raw.strip()
    if any(ch.isspace() or not ch.isprintable() for ch in api_key):
        raise InvalidInputError("The API key contains invalid characters.")
    return api_key


def _summary(credential: ProviderCredential | None) -> CredentialSummary | None:
    if credential is None:
        return None
    return CredentialSummary(
        id=credential.id,
        key_hint=credential.key_hint,
        verified_at=credential.verified_at,
        activated_at=credential.activated_at,
        created_at=credential.created_at,
    )


def _retire(credential: ProviderCredential, now: datetime) -> None:
    credential.status = CREDENTIAL_RETIRED
    credential.retired_at = now
    credential.ciphertext = None  # retired keys are deleted, not archived
    credential.nonce = None


def _canonical_url(base_url: str | None) -> str | None:
    try:
        return normalize_base_url(base_url).base_url if base_url else None
    except UnsafeEndpointError:
        return None


def _same_endpoint(candidate: str, stored: str | None) -> bool:
    """Compares canonical forms, so a stored URL saved before normalization still matches itself."""
    return stored is not None and candidate == _canonical_url(stored)


async def rebind_legacy_credentials(sessionmaker, cipher: CredentialCipher) -> int:
    """Re-encrypt custom-provider keys stored before origin binding so they are bound to the origin of
    the URL they were saved with. Idempotent and safe to run from several processes (SKIP LOCKED).
    Plaintext exists only in memory; a key that cannot be decrypted is left as is (stage a new key)."""
    rebound = 0
    async with sessionmaker() as db:
        rows = (
            await db.execute(
                select(ProviderCredential, ProviderConfig)
                .join(ProviderConfig, ProviderConfig.id == ProviderCredential.provider_config_id)
                .where(
                    ProviderConfig.base_url.is_not(None),
                    ProviderCredential.endpoint_origin.is_(None),
                    ProviderCredential.status.in_((CREDENTIAL_ACTIVE, CREDENTIAL_PENDING)),
                )
                .with_for_update(of=ProviderCredential, skip_locked=True)
            )
        ).all()
        for credential, config in rows:
            try:
                origin = endpoint_origin(config.base_url)
                api_key = cipher.decrypt(
                    EncryptedValue(credential.ciphertext, credential.nonce, credential.key_id),
                    associated_data=credential_aad(credential.id, config.provider),
                )
            except (DecryptionError, UnsafeEndpointError):
                logger.error("Credential %s for %s could not be bound to its endpoint", credential.id, config.provider)
                continue
            encrypted = cipher.encrypt(api_key, associated_data=credential_aad(credential.id, config.provider, origin))
            credential.ciphertext, credential.nonce, credential.key_id = (
                encrypted.ciphertext, encrypted.nonce, encrypted.key_id
            )
            credential.endpoint_origin = origin
            audit.record(
                db, SYSTEM_AUDIT_CONTEXT, "provider.credential_rebound", target=config.provider,
                details={"credential_id": str(credential.id), "origin": origin},
            )
            rebound += 1
        await db.commit()
    if rebound:
        logger.info("Bound %s existing custom-provider credentials to their endpoint origin", rebound)
    return rebound


class ProviderAdminService:
    def __init__(
        self, db: AsyncSession, settings: Settings, cipher: CredentialCipher, ctx: AuditContext
    ) -> None:
        self.db = db
        self.settings = settings
        self.cipher = cipher
        self.ctx = ctx

    # ----------------------------------------------------------------- reads

    async def list_states(self) -> list[ProviderState]:
        configs = {c.provider: c for c in (await self.db.execute(select(ProviderConfig))).scalars()}
        definitions = list(list_providers())
        definitions.extend(
            custom_provider_definition(c.provider, c.display_name, c.base_url, c.model)
            for c in sorted(configs.values(), key=lambda row: row.provider)
            if c.base_url and c.display_name
        )
        states = []
        for definition in definitions:
            config = configs.get(definition.id)
            creds = await self._credentials(config.id) if config else _Credentials()
            states.append(self._state(definition, config, creds))
        return states

    async def provider_catalog(self) -> list[ProviderDefinition]:
        configs = (
            await self.db.execute(
                select(ProviderConfig).where(ProviderConfig.base_url.is_not(None)).order_by(ProviderConfig.provider)
            )
        ).scalars()
        custom = [
            custom_provider_definition(c.provider, c.display_name, c.base_url, c.model)
            for c in configs
            if c.display_name and c.base_url
        ]
        return [*list_providers(), *custom]

    async def get_state(self, provider_id: str) -> ProviderState:
        config = await self._load_config(provider_id)
        definition = self._definition(provider_id, config)
        creds = await self._credentials(config.id) if config else _Credentials()
        return self._state(definition, config, creds)

    async def recent_audit_logs(self, limit: int) -> list[AuditLog]:
        result = await self.db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
        return list(result.scalars())

    # ------------------------------------------------------------- mutations

    async def create_custom_provider(
        self, display_name: str, base_url: str, model: str, raw_api_key: str
    ) -> ProviderState:
        """Verify and save a new independent endpoint without changing the active provider."""
        display_name = display_name.strip()
        model = model.strip()
        api_key = _normalize_api_key(raw_api_key)
        endpoint = await self._validated_endpoint(base_url, None)
        base_url = endpoint.base_url
        if not display_name or not model or any(not ch.isprintable() for ch in model):
            raise InvalidInputError("Provider name and model ID must contain printable characters.")

        slug = re.sub(r"[^a-z0-9]+", "-", f"{display_name}-{model}".lower()).strip("-")
        provider_id = f"custom-{slug}"[:50].rstrip("-")
        if get_provider(provider_id) is not None or await self._load_config(provider_id) is not None:
            raise ConflictError("A provider with this name and model already exists. Change the provider name to add another entry.")

        definition = custom_provider_definition(provider_id, display_name, base_url, model)
        provider_settings = definition.default_settings(model)
        await self._end_read_transaction()
        await self._verify_or_fail(
            definition,
            api_key,
            model,
            provider_settings,
            base_url=base_url,
            action="provider.custom_created",
            details={
                "display_name": display_name, "model": model, "origin": endpoint.origin, "key_hint": _key_hint(api_key)
            },
        )

        now = datetime.now(UTC)
        config_id = uuid.uuid4()
        credential_id = uuid.uuid4()
        encrypted = self.cipher.encrypt(
            api_key, associated_data=credential_aad(credential_id, provider_id, endpoint.origin)
        )
        self.db.add(
            ProviderConfig(
                id=config_id,
                provider=provider_id,
                model=model,
                display_name=display_name,
                base_url=base_url,
                settings=provider_settings,
                updated_by=self.ctx.actor_user_id,
            )
        )
        self.db.add(
            ProviderCredential(
                id=credential_id,
                provider_config_id=config_id,
                status=CREDENTIAL_PENDING,
                ciphertext=encrypted.ciphertext,
                nonce=encrypted.nonce,
                key_id=encrypted.key_id,
                key_hint=_key_hint(api_key),
                endpoint_origin=endpoint.origin,
                verified_at=now,
                created_by=self.ctx.actor_user_id,
            )
        )
        audit.record(
            self.db,
            self.ctx,
            "provider.custom_created",
            target=provider_id,
            details={
                "display_name": display_name, "model": model, "origin": endpoint.origin, "key_hint": _key_hint(api_key)
            },
        )
        await self._commit()
        return await self.get_state(provider_id)

    async def update_config(
        self,
        provider_id: str,
        model: str,
        settings_in: dict,
        expected_version: int,
        base_url: str | None = None,
        raw_api_key: str | None = None,
    ) -> ProviderState:
        config = await self._load_config(provider_id)
        definition = self._definition(provider_id, config)
        target_base_url = config.base_url if config else None
        endpoint_changed = False
        if config and config.base_url:
            model = self._normalize_model_id(model)
            if base_url is not None:
                endpoint = await self._validated_endpoint(base_url, provider_id)
                endpoint_changed = not _same_endpoint(endpoint.base_url, config.base_url)
                if endpoint_changed:
                    target_base_url = endpoint.base_url
            definition = custom_provider_definition(
                provider_id, config.display_name, target_base_url, model
            )
        else:
            if base_url is not None:
                raise InvalidInputError("The API base URL can only be edited for custom providers.")
            if model not in definition.model_ids():
                raise InvalidInputError(f"Unknown model '{model}' for provider '{provider_id}'.")
        if endpoint_changed and raw_api_key is None:
            raise InvalidInputError(
                "Enter the API key for the new API base URL. A saved key is never sent to a different endpoint."
            )
        if raw_api_key is not None and not endpoint_changed:
            raise InvalidInputError(
                "An API key is accepted here only together with a new API base URL. Use 'Replace API key' to rotate."
            )
        new_api_key = _normalize_api_key(raw_api_key) if raw_api_key is not None else None
        normalized, errors = definition.validate_settings(settings_in, model)
        if errors:
            raise InvalidInputError(details={"errors": errors})

        if config is None:
            if expected_version != 0:
                raise ConflictError()
            self.db.add(
                ProviderConfig(
                    provider=provider_id, model=model, settings=normalized, updated_by=self.ctx.actor_user_id
                )
            )
            audit.record(
                self.db, self.ctx, "provider.config_created", target=provider_id,
                details={"model": model, "settings": normalized},
            )
            await self._commit()
            return await self.get_state(provider_id)

        self._check_version(config, expected_version)

        previous_origin = credential_origin_or_none(config.base_url)
        new_origin = credential_origin_or_none(target_base_url)
        if endpoint_changed:
            # A new endpoint is verified only with the newly supplied key; no stored key is decrypted.
            await self._end_read_transaction()
            await self._verify_or_fail(
                definition,
                new_api_key,
                model,
                normalized,
                action="provider.endpoint_change",
                details={
                    "model": model, "origin": new_origin, "previous_origin": previous_origin,
                    "key_hint": _key_hint(new_api_key),
                },
                base_url=target_base_url,
            )

        # Verify a new custom model with the saved key (same endpoint) before changing the configuration.
        # For built-in active providers, preserve the same preflight when their model changes.
        custom_model_changed = config.base_url is not None and not endpoint_changed and model != config.model
        builtin_active_model_changed = config.base_url is None and config.is_active and model != config.model
        if custom_model_changed or builtin_active_model_changed:
            creds = await self._credentials(config.id)
            credential = (
                (creds.active or creds.pending)
                if config.base_url is not None
                else creds.active
            )
            if credential is not None:
                api_key = self._decrypt(credential, provider_id, config.base_url)
                await self._end_read_transaction()
                await self._verify_or_fail(
                    definition,
                    api_key,
                    model,
                    normalized,
                    action="provider.config_update",
                    details={"model": model, "credential_id": str(credential.id)},
                    base_url=target_base_url,
                )

        config = await self._load_config(provider_id, lock=True)
        self._check_version(config, expected_version)
        previous = {
            "model": config.model,
            "settings": config.settings,
            "base_url_changed": endpoint_changed,
        }
        if endpoint_changed:
            await self._replace_credentials_for_new_endpoint(
                config, provider_id, new_api_key, new_origin, await self._credentials(config.id),
                previous_origin=previous_origin, model=model,
            )
        config.model = model
        config.settings = normalized
        config.base_url = target_base_url
        if endpoint_changed and config.image_sharing_authorized_at is not None:
            # The authorization named the previous endpoint; a new recipient needs a new decision.
            config.image_sharing_authorized_at = None
            config.image_sharing_authorized_by = None
            previous["image_sharing_revoked"] = True
        config.version += 1
        config.updated_by = self.ctx.actor_user_id
        audit.record(
            self.db, self.ctx, "provider.config_updated", target=provider_id,
            details={"previous": previous, "model": model, "settings": normalized},
        )
        await self._commit()
        return await self.get_state(provider_id)

    async def stage_credential(self, provider_id: str, raw_api_key: str, expected_version: int) -> ProviderState:
        """Verify a new key against the live provider and store it encrypted as `pending`."""
        api_key = _normalize_api_key(raw_api_key)
        config = await self._load_config(provider_id)
        if config is None:
            raise ConflictError("Save the provider's model and settings before adding an API key.")
        definition = self._definition(provider_id, config)
        self._check_version(config, expected_version)
        model, provider_settings = config.model, config.settings
        await self._end_read_transaction()

        await self._verify_or_fail(
            definition, api_key, model, provider_settings, action="provider.credential_stage",
            details={"model": model, "key_hint": _key_hint(api_key)}, base_url=config.base_url,
        )

        now = datetime.now(UTC)
        config = await self._load_config(provider_id, lock=True)
        self._check_version(config, expected_version)
        creds = await self._credentials(config.id)
        if creds.pending is not None:
            _retire(creds.pending, now)
            await self.db.flush()  # free the one-pending-per-provider slot before inserting

        credential_id = uuid.uuid4()
        origin = credential_origin_or_none(config.base_url)
        encrypted = self.cipher.encrypt(api_key, associated_data=credential_aad(credential_id, provider_id, origin))
        self.db.add(
            ProviderCredential(
                id=credential_id,
                provider_config_id=config.id,
                status=CREDENTIAL_PENDING,
                ciphertext=encrypted.ciphertext,
                nonce=encrypted.nonce,
                key_id=encrypted.key_id,
                key_hint=_key_hint(api_key),
                endpoint_origin=origin,
                verified_at=now,
                created_by=self.ctx.actor_user_id,
            )
        )
        config.version += 1
        config.updated_by = self.ctx.actor_user_id
        audit.record(
            self.db, self.ctx, "provider.credential_staged", target=provider_id,
            details={
                "credential_id": str(credential_id),
                "key_hint": _key_hint(api_key),
                "model": model,
                "replaced_pending_id": str(creds.pending.id) if creds.pending else None,
            },
        )
        await self._commit()
        return await self.get_state(provider_id)

    async def activate(self, provider_id: str, expected_version: int) -> ProviderState:
        """Re-verify the pending (or current) key, then make this provider the active one."""
        config = await self._load_config(provider_id)
        if config is None:
            raise NotFoundError("This provider has not been configured.")
        definition = self._definition(provider_id, config)
        self._check_version(config, expected_version)
        creds = await self._credentials(config.id)
        candidate = creds.pending or creds.active
        if candidate is None:
            raise ConflictError("No verified API key is available. Add and verify a key first.")
        if candidate is creds.active and config.is_active:
            raise ConflictError("This provider is already active with this key.")
        candidate_id, candidate_status = candidate.id, candidate.status
        api_key = self._decrypt(candidate, provider_id, config.base_url)
        model, provider_settings = config.model, config.settings
        await self._end_read_transaction()

        # The key may have been revoked since it was staged: verify again right before go-live.
        await self._verify_or_fail(
            definition, api_key, model, provider_settings, action="provider.activate",
            details={"model": model, "credential_id": str(candidate_id)}, base_url=config.base_url,
        )

        now = datetime.now(UTC)
        # Lock every provider row in a fixed order: activation flips is_active across rows,
        # and a consistent lock order prevents deadlocks between concurrent activations.
        all_configs = list(
            (
                await self.db.execute(
                    select(ProviderConfig)
                    .order_by(ProviderConfig.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        config = next((c for c in all_configs if c.provider == provider_id), None)
        if config is None:
            raise ConflictError()
        self._check_version(config, expected_version)
        creds = await self._credentials(config.id)
        current = creds.pending if candidate_status == CREDENTIAL_PENDING else creds.active
        if current is None or current.id != candidate_id:
            raise ConflictError()

        deactivated = []
        for other in all_configs:
            if other.is_active and other.id != config.id:
                other.is_active = False
                other.version += 1
                deactivated.append(other.provider)
        await self.db.flush()  # free the single-active-provider slot first

        rotated_from = None
        if candidate_status == CREDENTIAL_PENDING:
            if creds.active is not None:
                rotated_from = str(creds.active.id)
                _retire(creds.active, now)
                await self.db.flush()  # free the one-active-key slot before promoting
            current.status = CREDENTIAL_ACTIVE
            current.activated_at = now

        config.is_active = True
        config.version += 1
        config.updated_by = self.ctx.actor_user_id
        audit.record(
            self.db, self.ctx, "provider.activated", target=provider_id,
            details={
                "credential_id": str(candidate_id),
                "key_hint": current.key_hint,
                "model": model,
                "rotated_from_credential_id": rotated_from,
                "deactivated_providers": deactivated,
            },
        )
        await self._commit()
        logger.info("Provider %s activated (rotation=%s)", provider_id, rotated_from is not None)
        return await self.get_state(provider_id)

    async def deactivate(self, provider_id: str, expected_version: int) -> ProviderState:
        config = await self._load_config(provider_id, lock=True)
        if config is None:
            raise NotFoundError("This provider has not been configured.")
        self._definition(provider_id, config)
        self._check_version(config, expected_version)
        if not config.is_active:
            raise ConflictError("This provider is not active.")
        config.is_active = False
        config.version += 1
        config.updated_by = self.ctx.actor_user_id
        audit.record(self.db, self.ctx, "provider.deactivated", target=provider_id)
        await self._commit()
        return await self.get_state(provider_id)

    async def set_image_sharing(self, provider_id: str, authorized: bool, expected_version: int) -> ProviderState:
        """Record (or withdraw) the admin's authorization to send pictures from documents to this provider.
        Without it, no document image ever leaves the server; text answers are unaffected."""
        config = await self._load_config(provider_id, lock=True)
        if config is None:
            raise NotFoundError("This provider has not been configured.")
        self._definition(provider_id, config)
        self._check_version(config, expected_version)
        if authorized == (config.image_sharing_authorized_at is not None):
            return await self.get_state(provider_id)
        config.image_sharing_authorized_at = datetime.now(UTC) if authorized else None
        config.image_sharing_authorized_by = self.ctx.actor_user_id if authorized else None
        config.version += 1
        config.updated_by = self.ctx.actor_user_id
        action = "provider.image_sharing_authorized" if authorized else "provider.image_sharing_revoked"
        audit.record(self.db, self.ctx, action, target=provider_id, details={"model": config.model})
        await self._commit()
        return await self.get_state(provider_id)

    async def remove(self, provider_id: str, expected_version: int) -> None:
        """Delete a provider's configuration and its stored keys (they cascade, so no ciphertext remains).
        A custom provider disappears; a built-in one returns to "not configured". The active provider
        must be deactivated first, so chat is never silently left without a provider."""
        config = await self._load_config(provider_id, lock=True)
        if config is None:
            raise NotFoundError("This provider has not been configured.")
        self._check_version(config, expected_version)
        if config.is_active:
            raise ConflictError("Deactivate this provider before removing it.")
        custom = config.base_url is not None
        await self.db.delete(config)  # credentials are deleted with it (ON DELETE CASCADE)
        audit.record(
            self.db, self.ctx, "provider.removed", target=provider_id, details={"model": config.model, "custom": custom}
        )
        await self._commit()
        logger.info("Provider %s removed (custom=%s)", provider_id, custom)

    async def test_connection(
        self, provider_id: str, raw_api_key: str | None, model: str | None, base_url: str | None = None
    ) -> TestConnectionResponse:
        """Live connectivity check. Never persists the key; records only the outcome."""
        config = await self._load_config(provider_id)
        definition = self._definition(provider_id, config)
        model = model or (config.model if config else definition.default_model)
        target_base_url = config.base_url if config else None
        endpoint_changed = False
        if config and config.base_url:
            model = self._normalize_model_id(model)
            if base_url is not None:
                endpoint = await self._validated_endpoint(base_url, provider_id)
                endpoint_changed = not _same_endpoint(endpoint.base_url, config.base_url)
                if endpoint_changed:
                    target_base_url = endpoint.base_url
            definition = custom_provider_definition(
                provider_id, config.display_name, target_base_url, model
            )
        elif model not in definition.model_ids():
            raise InvalidInputError(f"Unknown model '{model}' for provider '{provider_id}'.")
        elif base_url is not None:
            raise InvalidInputError("The API base URL can only be edited for custom providers.")
        if endpoint_changed and raw_api_key is None:
            # Checked before any stored key is read: a saved key is only sent to the URL it is bound to.
            raise InvalidInputError(
                "Enter the API key to test a new API base URL. A saved key is never sent to a different endpoint."
            )
        provider_settings = config.settings if config else definition.default_settings(model)

        if raw_api_key is not None:
            api_key, tested = _normalize_api_key(raw_api_key), "provided_key"
        else:
            creds = await self._credentials(config.id) if config else _Credentials()
            stored = creds.pending or creds.active
            if stored is None:
                raise InvalidInputError("Enter an API key to test; no key is stored for this provider.")
            api_key = self._decrypt(stored, provider_id, config.base_url)
            tested = "pending_key" if stored is creds.pending else "active_key"
        await self._end_read_transaction()

        started = time.perf_counter()
        reason, message, detail = None, "Connection verified: the key is valid and the model is available.", None
        try:
            await self._verify(definition, api_key, model, provider_settings, target_base_url)
        except ProviderVerificationError as exc:
            reason, message, detail = exc.reason, exc.message, exc.detail
        latency_ms = int((time.perf_counter() - started) * 1000)

        audit.record(
            self.db, self.ctx, "provider.connection_tested", target=provider_id,
            outcome="success" if reason is None else "failure",
            details={
                "tested": tested, "model": model, "key_hint": _key_hint(api_key), "reason": reason,
                **({"origin": credential_origin_or_none(target_base_url)} if target_base_url else {}),
            },
        )
        await self.db.commit()
        return TestConnectionResponse(
            ok=reason is None, reason=reason, message=message, detail=detail, tested=tested,
            model=model, latency_ms=latency_ms,
        )

    # --------------------------------------------------------------- helpers

    def _definition(self, provider_id: str, config: ProviderConfig | None = None) -> ProviderDefinition:
        definition = get_provider(provider_id)
        if definition is not None:
            return definition
        if config and config.base_url and config.display_name:
            return custom_provider_definition(provider_id, config.display_name, config.base_url, config.model)
        raise NotFoundError(f"Unknown provider '{provider_id}'.")

    @staticmethod
    def _normalize_model_id(raw: str) -> str:
        value = raw.strip()
        if not value or len(value) > 100 or any(ch.isspace() or not ch.isprintable() for ch in value):
            raise InvalidInputError("Enter a model ID of up to 100 printable characters with no spaces.")
        return value

    async def _validated_endpoint(self, raw: str, provider_id: str | None) -> Endpoint:
        """Syntax, HTTPS/SSRF policy and current DNS answers. A rejection is audited (origin only)."""
        try:
            return await validate_endpoint(raw, policy_from_settings(self.settings))
        except UnsafeEndpointError as exc:
            try:
                origin = normalize_base_url(raw).origin
            except UnsafeEndpointError:
                origin = None
            await audit.record_failure_now(
                self.db, self.ctx, "provider.endpoint_rejected", target=provider_id,
                details={"reason": "unsafe_endpoint", "origin": origin},
            )
            raise InvalidInputError(exc.message) from None

    async def _replace_credentials_for_new_endpoint(
        self,
        config: ProviderConfig,
        provider_id: str,
        api_key: str,
        origin: str,
        creds: _Credentials,
        *,
        previous_origin: str | None,
        model: str,
    ) -> None:
        """Keys bound to the old endpoint are shredded; the newly verified key takes their place. An active
        provider keeps serving (with the new key), an inactive one gets it as pending."""
        now = datetime.now(UTC)
        retired = [c for c in (creds.pending, creds.active) if c is not None]
        for credential in retired:
            _retire(credential, now)
        await self.db.flush()  # free the one-active/one-pending slots first
        credential_id = uuid.uuid4()
        status = CREDENTIAL_ACTIVE if config.is_active else CREDENTIAL_PENDING
        encrypted = self.cipher.encrypt(api_key, associated_data=credential_aad(credential_id, provider_id, origin))
        self.db.add(
            ProviderCredential(
                id=credential_id,
                provider_config_id=config.id,
                status=status,
                ciphertext=encrypted.ciphertext,
                nonce=encrypted.nonce,
                key_id=encrypted.key_id,
                key_hint=_key_hint(api_key),
                endpoint_origin=origin,
                verified_at=now,
                activated_at=now if status == CREDENTIAL_ACTIVE else None,
                created_by=self.ctx.actor_user_id,
            )
        )
        audit.record(
            self.db, self.ctx, "provider.endpoint_changed", target=provider_id,
            details={
                "previous_origin": previous_origin,
                "origin": origin,
                "model": model,
                "credential_id": str(credential_id),
                "credential_status": status,
                "key_hint": _key_hint(api_key),
                "retired_credential_ids": [str(c.id) for c in retired],
            },
        )

    def _state(
        self, definition: ProviderDefinition, config: ProviderConfig | None, creds: _Credentials
    ) -> ProviderState:
        return ProviderState(
            provider=definition.id,
            display_name=definition.display_name,
            base_url=config.base_url if config else None,
            configured=config is not None,
            model=config.model if config else definition.default_model,
            settings=config.settings if config else definition.default_settings(),
            is_active=config.is_active if config else False,
            version=config.version if config else 0,
            active_credential=_summary(creds.active),
            pending_credential=_summary(creds.pending),
            updated_at=config.updated_at if config else None,
            image_sharing_authorized=bool(config and config.image_sharing_authorized_at),
            image_sharing_authorized_at=config.image_sharing_authorized_at if config else None,
        )

    async def _load_config(self, provider_id: str, *, lock: bool = False) -> ProviderConfig | None:
        query = select(ProviderConfig).where(ProviderConfig.provider == provider_id)
        if lock:
            # populate_existing: refresh objects already in the session with the locked row.
            query = query.with_for_update().execution_options(populate_existing=True)
        return (await self.db.execute(query)).scalar_one_or_none()

    async def _credentials(self, config_id: uuid.UUID) -> _Credentials:
        rows = (
            await self.db.execute(
                select(ProviderCredential)
                .where(
                    ProviderCredential.provider_config_id == config_id,
                    ProviderCredential.status.in_((CREDENTIAL_ACTIVE, CREDENTIAL_PENDING)),
                )
                .execution_options(populate_existing=True)
            )
        ).scalars()
        creds = _Credentials()
        for row in rows:
            if row.status == CREDENTIAL_ACTIVE:
                creds.active = row
            else:
                creds.pending = row
        return creds

    @staticmethod
    def _check_version(config: ProviderConfig | None, expected_version: int) -> None:
        if config is None or config.version != expected_version:
            raise ConflictError()

    def _decrypt(self, credential: ProviderCredential, provider_id: str, base_url: str | None) -> str:
        """Decrypt only for the endpoint the key is bound to (`base_url` is where it will be sent)."""
        try:
            return decrypt_credential(self.cipher, credential, provider_id, base_url)
        except DecryptionError:
            logger.error("Stored credential %s for %s failed decryption", credential.id, provider_id)
            raise CredentialUnreadableError() from None

    async def _verify(
        self,
        definition: ProviderDefinition,
        api_key: str,
        model: str,
        provider_settings: dict,
        base_url: str | None = None,
    ) -> None:
        if not model_allowed_for_content(definition.id, base_url, self.settings):
            raise ProviderVerificationError(
                "unsafe_endpoint", "Outbound provider verification is disabled by the local-only processing policy."
            )
        client = build_client(self.settings, definition.id, api_key, model, provider_settings, base_url)
        try:
            await client.verify(self.settings.provider_verify_timeout_seconds)
        finally:
            await client.close()

    async def _verify_or_fail(
        self,
        definition: ProviderDefinition,
        api_key: str,
        model: str,
        provider_settings: dict,
        *,
        action: str,
        details: dict,
        base_url: str | None = None,
    ) -> None:
        try:
            await self._verify(definition, api_key, model, provider_settings, base_url)
        except ProviderVerificationError as exc:
            logger.warning("Provider verification failed provider=%s reason=%s", definition.id, exc.reason)
            await audit.record_failure_now(
                self.db, self.ctx, action, target=definition.id, details={**details, "reason": exc.reason}
            )
            message = f"{exc.message} {exc.detail}" if exc.detail else exc.message
            raise ProviderVerificationFailedError(message, details={"reason": exc.reason}) from None

    async def _end_read_transaction(self) -> None:
        """Don't hold a DB transaction open while waiting on the provider's network call."""
        await self.db.commit()

    async def _commit(self) -> None:
        try:
            await self.db.commit()
        except IntegrityError:
            # A concurrent request won the race for a unique slot (e.g. single active provider).
            await self.db.rollback()
            raise ConflictError() from None
