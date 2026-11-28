"""Resolves the LLM client for each request from the database configuration.

Runtime changes need no restart: every chat request reads the active
(config id, config version, credential id) tuple, which is one indexed query.
If it differs from the cached tuple, the credential is decrypted and a new
client is built. Every server process converges on the same configuration.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import CredentialCipher, DecryptionError, EncryptedValue, credential_aad
from app.core.errors import InvalidInputError, LLMNotConfiguredError, LLMUnavailableError
from app.db.models import CREDENTIAL_ACTIVE, ProviderConfig, ProviderCredential
from app.providers.endpoint_security import (
    UnsafeEndpointError,
    endpoint_origin,
    guarded_http_client,
    normalize_base_url,
    policy_from_settings,
)
from app.providers.registry import get_provider
from app.providers.openai_compatible import OpenAICompatibleClient
from app.services.llm import ClientParams, LLMClient
from app.services.llm_fallback import FallbackLLM
from app.services.processing_policy import model_allowed_for_content

logger = logging.getLogger(__name__)


def build_client(
    settings: Settings, provider_id: str, api_key: str, model: str, provider_settings: dict,
    base_url: str | None = None,
) -> LLMClient:
    definition = get_provider(provider_id)
    if definition is not None:
        return definition.client_factory(
            ClientParams(
                api_key=api_key,
                model=model,
                settings={**definition.default_settings(model), **provider_settings},
                timeout_seconds=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
            )
        )
    if base_url is None:
        raise LLMUnavailableError()
    # Custom endpoint: every connection is restricted to this URL's origin under the deployment policy.
    try:
        endpoint = normalize_base_url(base_url)
    except UnsafeEndpointError:
        raise LLMUnavailableError() from None
    return OpenAICompatibleClient(
        provider_id,
        endpoint.base_url,
        http_client=guarded_http_client(endpoint, policy_from_settings(settings)),
        params=ClientParams(
            api_key=api_key,
            model=model,
            settings=provider_settings,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        ),
    )


def allowed_models(config: ProviderConfig) -> list[str]:
    definition = get_provider(config.provider)
    return [m.id for m in definition.models] if definition is not None else [config.model]


async def model_options(db: AsyncSession, settings: Settings | None = None) -> list[dict]:
    """What users may pick in chat: every configured provider with an active credential and its models.
    The active provider's configured model is the default. No keys or endpoints are included."""
    rows = (
        await db.execute(
            select(ProviderConfig)
            .join(ProviderCredential, ProviderCredential.provider_config_id == ProviderConfig.id)
            .where(ProviderCredential.status == CREDENTIAL_ACTIVE)
            .order_by(ProviderConfig.is_active.desc(), ProviderConfig.created_at)
        )
    ).scalars()
    options = []
    for config in rows:
        if settings is not None and not model_allowed_for_content(config.provider, config.base_url, settings):
            continue
        definition = get_provider(config.provider)
        provider_label = definition.display_name if definition else (config.display_name or config.provider)
        for model in allowed_models(config):
            spec = definition.get_model(model) if definition else None
            options.append({
                "provider": config.provider,
                "provider_label": provider_label,
                "model": model,
                # Preserve custom model IDs exactly as configured. Hiding a path
                # prefix can make a broken endpoint/model pair look valid in the picker.
                "model_label": spec.label if spec else model,
                "is_default": bool(config.is_active and model == config.model),
            })
    options.sort(key=lambda o: not o["is_default"])
    return options


def credential_origin(base_url: str | None) -> str | None:
    """The origin a custom provider's credentials are bound to (None for built-in providers)."""
    if base_url is None:
        return None
    try:
        return endpoint_origin(base_url)
    except UnsafeEndpointError:
        raise DecryptionError("The provider URL is not valid") from None


def decrypt_credential(
    cipher: CredentialCipher, credential: ProviderCredential, provider_id: str, base_url: str | None
) -> str:
    """Decrypt a key only for the endpoint it is bound to. `base_url` is where the key is about to be sent:
    a custom-provider key decrypts only when that URL's origin is the one it was verified against."""
    if credential.ciphertext is None or credential.nonce is None:
        raise LLMUnavailableError()
    origin = credential_origin(base_url)
    if credential.endpoint_origin != origin:
        raise DecryptionError("Credential is bound to a different endpoint")
    return cipher.decrypt(
        EncryptedValue(credential.ciphertext, credential.nonce, credential.key_id),
        associated_data=credential_aad(credential.id, provider_id, origin),
    )


class LLMRuntime:
    def __init__(self, settings: Settings, cipher: CredentialCipher) -> None:
        self._settings = settings
        self._cipher = cipher
        # One client per (provider config, model); rebuilt when the config version or credential changes.
        self._clients: dict[tuple, tuple[tuple, LLMClient]] = {}
        self._lock = asyncio.Lock()
        self._retiring: dict[asyncio.Task, LLMClient] = {}

    async def get_client(self, db: AsyncSession, choice: tuple[str, str] | None = None) -> LLMClient:
        """The active provider's client, or the client for a (provider, model) picked in chat. A pick must be a
        configured provider with an active credential and one of its models; it never changes the active provider.
        With LLM_FALLBACK_PROVIDER set, the client falls back to that provider on a provider failure. Chat always
        sends the picked model (the default included), so a pick falls back too; the answer names the provider used."""
        client = await self._client(db, choice)
        fallback_id = self._settings.llm_fallback_provider
        if not fallback_id or getattr(client, "provider", None) == fallback_id:
            return client
        try:
            fallback = await self._client(db, (fallback_id, None))
        except Exception as exc:  # a missing or blocked fallback must not break the working primary
            logger.warning("Fallback provider %s is not usable: %s", fallback_id, type(exc).__name__)
            return client
        return FallbackLLM(client, fallback)  # type: ignore[return-value]

    async def get_web_research_client(self, db: AsyncSession) -> LLMClient:
        """Resolve a configured provider with native, grounded public search, independent of chat model choice."""
        rows = (
            await db.execute(
                select(ProviderConfig.provider, ProviderConfig.model)
                .join(ProviderCredential, ProviderCredential.provider_config_id == ProviderConfig.id)
                .where(ProviderCredential.status == CREDENTIAL_ACTIVE)
                .order_by(ProviderConfig.is_active.desc(), ProviderConfig.created_at)
                .limit(20)
            )
        ).all()
        for provider_id, model in rows:
            try:
                client = await self._client(db, (provider_id, model))
            except Exception as exc:
                logger.info("Skipping unavailable web provider %s (%s)", provider_id, type(exc).__name__)
                continue
            if bool(getattr(client, "supports_grounded_web_research", False)):
                return client
        raise LLMUnavailableError("Live web search requires a configured Gemini model that supports Google Search.")

    async def get_audio_transcription_client(self, db: AsyncSession) -> LLMClient:
        """The configured fallback provider's client for Whisper-compatible audio transcription.

        Local Whisper is the primary transcriber (see app/integrations/whatsapp_voice.py);
        the API fallback follows the platform fallback rule: only the provider named by
        ``llm_fallback_provider`` is tried, and only when it serves audio transcription.
        Raises LLMUnavailableError when no fallback is configured or it cannot transcribe.
        """
        fallback_id = self._settings.llm_fallback_provider
        if not fallback_id:
            raise LLMUnavailableError("No fallback provider is configured for audio transcription.")
        try:
            client = await self._client(db, (fallback_id, None))
        except Exception as exc:
            logger.warning("Fallback provider %s is not usable for audio transcription: %s", fallback_id, type(exc).__name__)
            raise LLMUnavailableError() from exc
        if not bool(getattr(client, "supports_audio_transcription", False)) or not callable(
            getattr(client, "transcribe_audio", None)
        ):
            raise LLMUnavailableError("The configured fallback provider does not support audio transcription.")
        return client

    async def _client(self, db: AsyncSession, choice: tuple[str, str | None] | None) -> LLMClient:
        query = select(ProviderConfig, ProviderCredential).join(
            ProviderCredential, ProviderCredential.provider_config_id == ProviderConfig.id
        ).where(ProviderCredential.status == CREDENTIAL_ACTIVE)
        if choice is None:
            query = query.where(ProviderConfig.is_active.is_(True))
        else:
            query = query.where(ProviderConfig.provider == choice[0])
        row = (await db.execute(query)).first()
        if row is None:
            if choice is None:
                raise LLMNotConfiguredError()
            raise InvalidInputError("The selected model is not available. Choose another one.")
        config, credential = row
        # A fallback is named by provider only and uses that provider's configured model.
        model = config.model if choice is None or choice[1] is None else choice[1]
        if model not in allowed_models(config):
            raise InvalidInputError("The selected model is not available. Choose another one.")
        if not model_allowed_for_content(config.provider, config.base_url, self._settings):
            raise LLMUnavailableError("The configured model is blocked by the local-only processing policy.")
        slot, cache_key = (config.id, model), (config.version, credential.id)

        cached = self._clients.get(slot)
        if cached is not None and cached[0] == cache_key:
            return cached[1]
        async with self._lock:
            cached = self._clients.get(slot)
            if cached is not None and cached[0] == cache_key:
                return cached[1]
            try:
                api_key = decrypt_credential(self._cipher, credential, config.provider, config.base_url)
            except Exception:
                logger.error("Active credential for %s could not be decrypted", config.provider)
                raise LLMUnavailableError() from None
            # Saved settings belong to the configured model; another model of the provider uses its defaults.
            settings = config.settings if model == config.model else {}
            client = build_client(self._settings, config.provider, api_key, model, settings, config.base_url)
            self._clients[slot] = (cache_key, client)
            logger.info(
                "LLM client loaded provider=%s model=%s config_version=%s", config.provider, model, config.version
            )
            if cached is not None:
                self._retire_later(cached[1])
            return client

    def _retire_later(self, client: LLMClient) -> None:
        """Close a replaced client after in-flight requests on it have had time to finish."""
        grace = self._settings.llm_timeout_seconds * (self._settings.llm_max_retries + 1) + 5

        async def _close() -> None:
            await asyncio.sleep(grace)
            await client.close()

        task = asyncio.create_task(_close())
        self._retiring[task] = client
        task.add_done_callback(lambda t: self._retiring.pop(t, None))

    async def close(self) -> None:
        clients = list(self._retiring.values())
        for task in list(self._retiring):
            task.cancel()
        clients.extend(client for _, client in self._clients.values())
        for client in clients:
            await client.close()
