"""Resolves the LLM client for each request from the database configuration.

Runtime changes need no restart: every chat request reads the active
(config id, config version, credential id) tuple, which is one indexed query.
If it differs from the cached tuple, the credential is decrypted and a new
client is built. Every server process converges on the same configuration.
"""

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import CredentialCipher, DecryptionError, EncryptedValue, credential_aad
from app.core.errors import (
    InvalidInputError,
    LLMNotConfiguredError,
    LLMUnavailableError,
    RoutingBudgetExceededError,
)
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
from app.schemas.chat import RoutingInfo
from app.services.llm import ClientParams, LLMClient
from app.services.llm_fallback import FallbackLLM
from app.services.model_router import (
    BudgetAction,
    check_budget,
    ModelCandidate,
    NoCapableModelError,
    RoutingDecision,
    RoutingPolicy,
    TaskClass,
    classify_task,
    route,
)
from app.services.processing_policy import model_allowed_for_content
from app.services.routing_records import routing_spent_this_month
from app.services.routing_policy import (
    RoutingPolicyConfig,
    effective_budget_usd,
    effective_capability_tier,
    effective_policy,
    load_routing_policy,
)

logger = logging.getLogger(__name__)


# A model name counts as natively grounded only for Gemini models served from
# Google's generative-language origin -- the same rule the Gemini adapter uses
# for ``supports_grounded_web_research``. Anything else is a heuristic "no";
# ``get_routed_client`` verifies the built client's real capability before
# answering a grounded-research task.
_GEMINI_GROUNDED_MODEL = re.compile(
    r"gemini-(?:(?:2\.[5-9]|[3-9](?:\.[0-9]+)?)[A-Za-z0-9._-]*|(?:flash|flash-lite|pro)-latest)",
    re.IGNORECASE,
)


def _capability_tier(model: str) -> int:
    """Reasoning tier from the model id (the catalog has no tier field)."""
    name = model.lower()
    if "pro" in name:
        return 3
    if any(token in name for token in ("flash", "lite", "mini", "haiku")):
        return 1
    return 2


def _looks_grounded(provider: str, model: str, base_url: str | None) -> bool:
    """Heuristic grounded-search flag for a candidate. Built-in providers
    keep base_url NULL and are never grounded; a custom provider is grounded
    only when it points at Google's generative-language origin with a
    Gemini model name. The runtime verifies the real client capability."""
    if base_url is None:
        return False
    from urllib.parse import urlparse

    try:
        host = urlparse(base_url).hostname
    except ValueError:
        return False
    return host == "generativelanguage.googleapis.com" and bool(_GEMINI_GROUNDED_MODEL.fullmatch(model))


@dataclass
class RoutedClient:
    """A routed client plus the decision that picked it."""

    client: LLMClient
    decision: RoutingDecision


def routing_info(decision: RoutingDecision) -> RoutingInfo:
    """Public "why this model?" metadata for a routing decision. Ids, cost
    estimate and reason only -- never prompt text, keys or endpoints."""
    return RoutingInfo(
        provider=decision.provider,
        model=decision.model,
        task_class=decision.task_class.value,
        policy=decision.policy.value,
        estimated_cost_usd=float(decision.estimated_cost_usd) if decision.estimated_cost_usd is not None else None,
        reason=decision.reason,
        alternates=[candidate.key for candidate in decision.alternates],
        budget_action=decision.budget_action,
    )


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

    async def get_routed_client(
        self,
        db: AsyncSession,
        prompt: str,
        *,
        task_class: TaskClass | None = None,
        policy: RoutingPolicy | None = None,
        needs_vision: bool = False,
        needs_research: bool = False,
        estimated_completion_tokens: int = 512,
        department_id: uuid.UUID | None = None,
        monthly_budget_usd: Decimal | None = None,
        policy_config: RoutingPolicyConfig | None = None,
    ) -> RoutedClient:
        """Pick the cheapest/fastest capable model for ``prompt`` and return a
        client that tries it first, then each alternate in rank order, then the
        deployment's single fallback provider as the last resort. The router
        only picks which model answers; it never changes what a user may see
        or do. No prompt text, key or endpoint is ever logged here."""
        estimated_prompt_tokens = max(1, len(prompt or "") // 4)
        task = classify_task(
            prompt,
            task_class=task_class,
            needs_vision=needs_vision,
            needs_research=needs_research,
            estimated_prompt_tokens=estimated_prompt_tokens,
        )
        # The stored policy (admin-editable) decides the ranking policy for
        # this task class and any per-model tier overrides; an explicit
        # caller policy still wins. No stored row means the default: cheapest.
        config = policy_config or await load_routing_policy(db)
        policy = policy or effective_policy(config, task)
        candidates = await self._routing_candidates(db, policy_config=config)
        if not candidates:
            raise LLMNotConfiguredError()
        try:
            decision = route(
                task,
                candidates,
                estimated_prompt_tokens=estimated_prompt_tokens,
                estimated_completion_tokens=estimated_completion_tokens,
                policy=policy,
            )
        except NoCapableModelError as exc:
            raise LLMUnavailableError("No configured model can handle this request.") from exc
        # The active provider's configured model is the deployment default
        # (see model_options: "The active provider's configured model is the
        # default"). When the router has no price basis for its choice -- the
        # chosen model is unpriced -- a cost-driven policy (cheapest /
        # balanced) must not silently override that default with a latency
        # tie-break: the admin's explicit model wins, so changing it changes
        # what answers. A priced choice is the router's job and stands, and
        # FASTEST ranks on latency, which is a real basis.
        if decision.estimated_cost_usd is None and policy in (
            RoutingPolicy.CHEAPEST,
            RoutingPolicy.BALANCED,
        ):
            default = await self._configured_default_model(db)
            if default is not None:
                default_key = f"{default[0]}:{default[1]}"
                if default_key != decision.key:
                    ranked = [
                        next(c for c in candidates if c.key == decision.key),
                        *decision.alternates,
                    ]
                    preferred = next(
                        (c for c in ranked if c.key == default_key), None
                    )
                    if preferred is not None:
                        decision = replace(
                            decision,
                            provider=preferred.provider,
                            model=preferred.model,
                            alternates=tuple(
                                c for c in ranked if c.key != default_key
                            ),
                            reason=(
                                decision.reason
                                + f"; provider default {default_key}"
                                " (no price basis to prefer another model)"
                            ),
                        )
                        logger.info(
                            "Routed %s task to provider default %s (unpriced)",
                            task.value,
                            default_key,
                        )
        # An explicit caller budget wins; otherwise the stored policy's
        # budget applies (the department's own budget, else the org-wide
        # one). None anywhere means no budget is enforced.
        budget_usd = (
            monthly_budget_usd
            if monthly_budget_usd is not None
            else effective_budget_usd(config, department_id)
        )
        if budget_usd is not None:
            spent = await routing_spent_this_month(db, department_id=department_id)
            budget = check_budget(
                decision.task_class,
                spent_usd=spent,
                estimated_cost_usd=decision.estimated_cost_usd,
                monthly_budget_usd=budget_usd,
            )
            if budget.action is BudgetAction.REFUSE:
                raise RoutingBudgetExceededError(
                    budget.reason,
                    details={
                        "task_class": decision.task_class.value,
                        "provider": decision.provider,
                        "model": decision.model,
                        "estimated_cost_usd": (
                            str(decision.estimated_cost_usd)
                            if decision.estimated_cost_usd is not None
                            else None
                        ),
                        "spent_usd": str(spent),
                        "monthly_budget_usd": str(budget_usd),
                    },
                )
            if budget.action is BudgetAction.DOWNGRADE:
                downgraded = route(
                    task,
                    candidates,
                    estimated_prompt_tokens=estimated_prompt_tokens,
                    estimated_completion_tokens=estimated_completion_tokens,
                    policy=RoutingPolicy.CHEAPEST,
                )
                decision = replace(
                    downgraded,
                    budget_action="downgrade",
                    reason=downgraded.reason + "; " + budget.reason,
                )
                logger.warning(
                    "Routing downgraded %s task to %s: %s", task.value, decision.key, budget.reason
                )
        ordered = [next(c for c in candidates if c.key == decision.key), *decision.alternates]
        client = await self._routed_chain(db, ordered, task)
        # The reason names provider/model ids and the cost estimate only.
        logger.info("Routed %s task to %s", task.value, decision.key)
        return RoutedClient(client=client, decision=decision)

    async def _configured_default_model(
        self, db: AsyncSession
    ) -> tuple[str, str] | None:
        """The active provider's configured model: (provider, model)."""
        config = (
            await db.execute(
                select(ProviderConfig).where(ProviderConfig.is_active.is_(True))
            )
        ).scalars().first()
        if config is None:
            return None
        return (config.provider, config.model)

    async def _routing_candidates(
        self, db: AsyncSession, *, policy_config: RoutingPolicyConfig | None = None
    ) -> list[ModelCandidate]:
        """Enabled models as router candidates. Pricing comes from
        Settings.llm_model_pricing_json; a model without a rate stays in the
        list and the router sorts it after every priced model."""
        rows = (
            await db.execute(
                select(ProviderConfig)
                .join(ProviderCredential, ProviderCredential.provider_config_id == ProviderConfig.id)
                .where(
                    ProviderCredential.status == CREDENTIAL_ACTIVE,
                    # Deactivation flips is_active, not the credential:
                    # without this filter a deactivated provider kept
                    # answering routed traffic (test_deactivate_stops_traffic).
                    ProviderConfig.is_active.is_(True),
                )
                .order_by(ProviderConfig.is_active.desc(), ProviderConfig.created_at)
            )
        ).scalars().all()
        candidates: list[ModelCandidate] = []
        for config in rows:
            if not model_allowed_for_content(config.provider, config.base_url, self._settings):
                continue
            definition = get_provider(config.provider)
            for model in allowed_models(config):
                spec = definition.get_model(model) if definition else None
                rates = self._settings.llm_model_pricing_json.get(f"{config.provider}:{model}") or {}
                tier = _capability_tier(model)
                if policy_config is not None:
                    tier = effective_capability_tier(policy_config, config.provider, model, tier)
                candidates.append(
                    ModelCandidate(
                        provider=config.provider,
                        model=model,
                        context_window=spec.context_window if spec else 131_072,
                        max_output_tokens=spec.max_output_tokens if spec else 8192,
                        vision=bool(spec.vision) if spec else False,
                        grounded_research=_looks_grounded(config.provider, model, config.base_url),
                        capability_tier=tier,
                        latency_tier=_capability_tier(model),
                        input_per_1m_tokens_usd=(
                            Decimal(str(rates["input_per_1m_tokens_usd"]))
                            if rates.get("input_per_1m_tokens_usd") is not None
                            else None
                        ),
                        output_per_1m_tokens_usd=(
                            Decimal(str(rates["output_per_1m_tokens_usd"]))
                            if rates.get("output_per_1m_tokens_usd") is not None
                            else None
                        ),
                    )
                )
        return candidates

    async def _routed_chain(
        self, db: AsyncSession, ordered: list[ModelCandidate], task_class: TaskClass
    ) -> LLMClient:
        """Build the fallback chain for a routing decision, in rank order. A
        grounded-research task only accepts a client that really supports
        grounded web research; the router's flag is only a heuristic. The
        deployment's single fallback provider (if configured and not already
        in the chain) stays as the last resort."""
        primary: LLMClient | None = None
        alternates: list[LLMClient] = []
        for candidate in ordered:
            client = await self._client(db, (candidate.provider, candidate.model))
            if task_class is TaskClass.GROUNDED_RESEARCH and not getattr(
                client, "supports_grounded_web_research", False
            ):
                continue
            if primary is None:
                primary = client
            else:
                alternates.append(client)
        if primary is None:
            raise LLMUnavailableError("No configured model supports grounded web research.")
        chain: LLMClient = primary
        if alternates:
            tail: LLMClient = alternates[-1]
            for alternate in reversed(alternates[:-1]):
                tail = FallbackLLM(alternate, tail)  # type: ignore[assignment]
            chain = FallbackLLM(primary, tail)  # type: ignore[assignment]
        fallback_id = self._settings.llm_fallback_provider
        if fallback_id and fallback_id not in {c.provider for c in ordered}:
            try:
                fallback = await self._client(db, (fallback_id, None))
            except Exception as exc:
                logger.warning("Fallback provider %s is not usable: %s", fallback_id, type(exc).__name__)
            else:
                chain = FallbackLLM(chain, fallback)  # type: ignore[assignment]
        return chain

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

async def resolve_chat_client(
    runtime: LLMRuntime,
    db: AsyncSession,
    prompt: str,
    *,
    model_choice: tuple[str, str] | None = None,
    task_class: TaskClass | None = None,
    needs_vision: bool = False,
    needs_research: bool = False,
    department_id: uuid.UUID | None = None,
    monthly_budget_usd: Decimal | None = None,
    policy_config: RoutingPolicyConfig | None = None,
) -> tuple[LLMClient, RoutingDecision | None]:
    """Pick the client for one chat/RAG question. An explicit user pick wins
    and bypasses routing (decision None). Otherwise the smart router picks
    the cheapest capable model and the decision is returned for metadata and
    telemetry. Runtimes without get_routed_client (older test doubles) fall
    back to the plain active-provider client."""
    if model_choice is not None:
        return await runtime.get_client(db, model_choice), None
    get_routed = getattr(runtime, "get_routed_client", None)
    if get_routed is None:
        return await runtime.get_client(db), None
    routed = await get_routed(
        db,
        prompt,
        task_class=task_class,
        needs_vision=needs_vision,
        needs_research=needs_research,
        department_id=department_id,
        monthly_budget_usd=monthly_budget_usd,
        policy_config=policy_config,
    )
    return routed.client, routed.decision
