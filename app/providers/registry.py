"""Provider catalog: the single source of truth for which providers, models and
settings exist. The admin UI renders entirely from this metadata.

Adding an OpenAI-compatible provider is one new `ProviderDefinition` entry;
any other API style needs one adapter class implementing `LLMClient`.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Literal

from app.core.errors import LLMUnavailableError
from app.providers.openai_compatible import OpenAICompatibleClient
from app.services.llm import ClientParams, LLMClient


@dataclass(frozen=True)
class ModelOption:
    id: str
    label: str
    description: str
    context_window: int
    max_output_tokens: int
    default_output_tokens: int
    vision: bool = False  # accepts image input (used to describe pictures in documents)


@dataclass(frozen=True)
class SelectOption:
    value: str
    label: str


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    type: Literal["number", "integer", "select"]
    default: float | str
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    options: tuple[SelectOption, ...] = ()
    # When true, the maximum and default come from the selected model's output-token limits.
    per_model_limit: bool = False


@dataclass(frozen=True)
class ProviderDefinition:
    id: str
    display_name: str
    description: str
    docs_url: str
    models: tuple[ModelOption, ...]
    default_model: str
    settings: tuple[SettingField, ...]
    client_factory: Callable[[ClientParams], LLMClient] = field(repr=False, compare=False)

    def model_ids(self) -> set[str]:
        return {m.id for m in self.models}

    def get_model(self, model_id: str) -> ModelOption | None:
        return next((m for m in self.models if m.id == model_id), None)

    def _bounds(self, spec: SettingField, model: ModelOption | None) -> tuple[float, float, float | str]:
        if spec.per_model_limit and model is not None:
            return spec.minimum or 1, model.max_output_tokens, model.default_output_tokens
        return spec.minimum, spec.maximum, spec.default

    def default_settings(self, model_id: str | None = None) -> dict:
        model = self.get_model(model_id or self.default_model)
        return {s.key: self._bounds(s, model)[2] for s in self.settings}

    def validate_settings(self, values: dict, model_id: str) -> tuple[dict, list[str]]:
        """Return (normalized settings, errors). Unknown keys and out-of-range values are errors."""
        model = self.get_model(model_id)
        errors: list[str] = []
        known = {s.key: s for s in self.settings}
        for key in values:
            if key not in known:
                errors.append(f"Unknown setting '{key}'.")
        normalized: dict = {}
        for spec in self.settings:
            minimum, maximum, default = self._bounds(spec, model)
            value = values.get(spec.key, default)
            if spec.type == "select":
                allowed = {o.value for o in spec.options}
                if value not in allowed:
                    errors.append(f"'{spec.key}' must be one of: {', '.join(sorted(allowed))}.")
                    continue
                normalized[spec.key] = value
                continue
            if isinstance(value, bool) or not isinstance(value, int | float):
                errors.append(f"'{spec.key}' must be a number.")
                continue
            if spec.type == "integer":
                if value != int(value):
                    errors.append(f"'{spec.key}' must be a whole number.")
                    continue
                value = int(value)
            if not minimum <= value <= maximum:
                errors.append(f"'{spec.key}' must be between {minimum:g} and {maximum:g} for this model.")
                continue
            normalized[spec.key] = value
        return normalized, errors

    def to_public_dict(self) -> dict:
        data = asdict(self)
        data.pop("client_factory", None)
        return data


# Source: https://api-docs.deepseek.com/quick_start/pricing (checked 2026-10-15).
# Both models: 1M context, up to 384K output, thinking mode on by default.
DEEPSEEK = ProviderDefinition(
    id="deepseek",
    display_name="DeepSeek",
    description="DeepSeek V4 models via the OpenAI-compatible API.",
    docs_url="https://api-docs.deepseek.com/",
    models=(
        ModelOption(
            id="deepseek-flash",
            label="DeepSeek V4.1 Flash",
            description="Fast and low-cost. Best default for everyday chat. Supports vision.",
            context_window=1_000_000,
            max_output_tokens=384_000,
            default_output_tokens=32_768,
            vision=True,
        ),
        ModelOption(
            id="deepseek-v4-pro",
            label="DeepSeek V4 Pro",
            description="Most capable model for complex reasoning, analysis and coding.",
            context_window=1_000_000,
            max_output_tokens=384_000,
            default_output_tokens=32_768,
        ),
    ),
    default_model="deepseek-flash",
    settings=(
        SettingField(
            key="thinking",
            label="Thinking mode",
            type="select",
            default="enabled",
            description="Reason step by step before answering. Better quality, slower and uses more tokens.",
            options=(SelectOption("enabled", "On"), SelectOption("disabled", "Off")),
        ),
        SettingField(
            key="max_tokens",
            label="Max output tokens",
            type="integer",
            default=32_768,
            minimum=256,
            maximum=384_000,
            step=256,
            description="Upper bound per reply. In thinking mode this also covers the reasoning.",
            per_model_limit=True,
        ),
        SettingField(
            key="temperature",
            label="Temperature",
            type="number",
            default=1.0,
            minimum=0,
            maximum=2,
            step=0.1,
            description="Lower is more precise, higher is more creative. May have no effect in thinking mode.",
        ),
    ),
    client_factory=lambda params: OpenAICompatibleClient(
        "deepseek",
        "https://api.deepseek.com",
        params,
        extra_body=lambda s: {"thinking": {"type": s["thinking"]}},
    ),
)

PROVIDERS: dict[str, ProviderDefinition] = {DEEPSEEK.id: DEEPSEEK}


def get_provider(provider_id: str) -> ProviderDefinition | None:
    return PROVIDERS.get(provider_id)


def custom_provider_definition(
    provider_id: str, display_name: str, base_url: str, model_id: str
) -> ProviderDefinition:
    """Build UI metadata and an OpenAI-compatible adapter for an admin-added endpoint."""
    return ProviderDefinition(
        id=provider_id,
        display_name=display_name,
        description="Custom OpenAI-compatible chat completions API.",
        docs_url="",
        models=(
            ModelOption(
                id=model_id,
                label=model_id,
                description="Model configured for this API endpoint.",
                context_window=0,  # Custom model limits vary by endpoint and are not discoverable here.
                max_output_tokens=0,
                default_output_tokens=0,
            ),
        ),
        default_model=model_id,
        settings=(
            SettingField(
                key="max_tokens",
                label="Max output tokens",
                type="integer",
                default=4096,
                minimum=1,
                maximum=131_072,
                step=256,
                description="Upper bound per reply. The API may impose a lower limit.",
            ),
            SettingField(
                key="temperature",
                label="Temperature",
                type="number",
                default=1.0,
                minimum=0,
                maximum=2,
                step=0.1,
                description="Lower is more precise, higher is more creative.",
            ),
        ),
        client_factory=_custom_clients_use_build_client,
    )


def _custom_clients_use_build_client(params: ClientParams) -> LLMClient:
    """Custom endpoints are only reachable through `llm_runtime.build_client`, which binds the client to the
    endpoint origin and applies the SSRF policy. Never build an unguarded client from the definition."""
    raise LLMUnavailableError()


def list_providers() -> list[ProviderDefinition]:
    return list(PROVIDERS.values())
