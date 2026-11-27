"""Provider-independent LLM abstraction.

Routes and services depend only on `LLMClient`. Concrete providers live in
`app.providers` and are selected at runtime from the database configuration.
"""

from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from app.schemas.chat import ChatResponse

VerificationReason = Literal[
    "invalid_credentials",
    "model_unavailable",
    "timeout",
    "rate_limited",
    "provider_unavailable",
    "unsafe_endpoint",
    "redirect_refused",
]

VERIFICATION_MESSAGES: dict[str, str] = {
    "invalid_credentials": "The provider rejected the API key.",
    "model_unavailable": "The selected model is not available for this API key.",
    "timeout": "The provider did not respond in time.",
    "rate_limited": "The provider is rate limiting requests; the key could not be verified.",
    "provider_unavailable": "The provider could not be reached or returned an error.",
    "unsafe_endpoint": "The API URL is not allowed: it resolves to a private, internal or reserved address.",
    "redirect_refused": "The API URL redirected elsewhere. Redirects are not followed; enter the final API URL.",
}


class ProviderVerificationError(Exception):
    def __init__(self, reason: VerificationReason, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = VERIFICATION_MESSAGES[reason]
        # What the provider or network actually reported, for the admin's connection test. Never holds the key.
        self.detail = detail


class VisionUnsupportedError(Exception):
    """The provider or model does not accept image input."""


@dataclass(frozen=True)
class ClientParams:
    """Everything a provider adapter needs to build a client. `repr` hides the key."""

    api_key: str = field(repr=False)
    model: str
    settings: dict
    timeout_seconds: float
    max_retries: int


class LLMClient(Protocol):
    provider: str

    @property
    def supports_image_generation(self) -> bool: ...

    async def generate_image(self, prompt: str) -> tuple[bytes, str, str, str]:
        """Return sanitized-candidate image bytes, MIME type, model and provider metadata."""
        ...

    async def edit_image(self, image_png: bytes, prompt: str) -> tuple[bytes, str, str, str]:
        """Return an edited image using the supplied image as input."""
        ...

    async def chat(self, message: str, *, system_prompt: str | None = None) -> ChatResponse:
        """Single-turn completion. `system_prompt` overrides the provider's default assistant prompt."""
        ...

    async def chat_stream(
        self, message: str, *, system_prompt: str | None = None, on_token: Callable[[str], Awaitable[None]]
    ) -> ChatResponse:
        """Stream generated text through `on_token` and return the completed provider response."""
        ...

    async def verify(self, timeout_seconds: float) -> None:
        """Make a real provider request that proves the key and model work.

        Raises `ProviderVerificationError` on failure.
        """
        ...

    async def close(self) -> None: ...
