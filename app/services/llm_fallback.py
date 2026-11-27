"""Answer from a second, operator-chosen provider when the active provider cannot.

The active provider can fail for reasons the user cannot fix: an exhausted account balance (HTTP
402), a rate limit, a timeout or an outage. When the deployment names a fallback provider
(`LLM_FALLBACK_PROVIDER`, typically a local model such as Ollama), the same request is retried
there once. Only these provider-side failures fall back; invalid input and policy blocks do not.
"""

import logging
import inspect

from app.core.errors import LLMRateLimitError, LLMTimeoutError, LLMUnavailableError
from app.services.llm import LLMClient, VisionUnsupportedError

logger = logging.getLogger(__name__)

FALLBACK_ERRORS = (LLMUnavailableError, LLMTimeoutError, LLMRateLimitError)


class FallbackLLM:
    """Retry provider-side failures on the configured backup for all content operations."""

    def __init__(self, primary: LLMClient, fallback: LLMClient) -> None:
        self._primary = primary
        self._fallback = fallback

    @property
    def supports_grounded_web_research(self) -> bool:
        return any(
            bool(getattr(client, "supports_grounded_web_research", False))
            for client in (self._primary, self._fallback)
        )

    @property
    def supports_image_generation(self) -> bool:
        return any(bool(getattr(client, "supports_image_generation", False)) for client in (self._primary, self._fallback))

    async def generate_image(self, prompt: str) -> tuple[bytes, str, str, str]:
        candidates = [
            client for client in (self._primary, self._fallback)
            if bool(getattr(client, "supports_image_generation", False))
            and callable(getattr(client, "generate_image", None))
        ]
        if not candidates:
            raise LLMUnavailableError("No configured provider supports image generation.")
        for index, client in enumerate(candidates):
            try:
                return await client.generate_image(prompt)
            except FALLBACK_ERRORS:
                if index + 1 >= len(candidates):
                    raise
                logger.warning(
                    "Image generation provider %s failed; trying configured image-capable fallback %s",
                    getattr(client, "provider", "unknown"),
                    getattr(candidates[index + 1], "provider", "unknown"),
                )
        raise LLMUnavailableError("Image generation is unavailable.")

    async def edit_image(self, image_png: bytes, prompt: str) -> tuple[bytes, str, str, str]:
        """Edit with the first configured provider that supports image generation/editing."""
        candidates = [
            client for client in (self._primary, self._fallback)
            if bool(getattr(client, "supports_image_generation", False))
            and callable(getattr(client, "edit_image", None))
        ]
        if not candidates:
            raise LLMUnavailableError("No configured provider supports image editing.")
        for index, client in enumerate(candidates):
            try:
                return await client.edit_image(image_png, prompt)
            except FALLBACK_ERRORS:
                if index + 1 >= len(candidates):
                    raise
                logger.warning(
                    "Image editing provider %s failed; trying configured image-capable fallback %s",
                    getattr(client, "provider", "unknown"),
                    getattr(candidates[index + 1], "provider", "unknown"),
                )
        raise LLMUnavailableError("Image editing is unavailable.")

    async def grounded_web_research(self, prompt: str, *, urls: list[str] | None = None) -> dict:
        """Use whichever configured provider exposes the real grounded web tool."""
        candidates = [
            client for client in (self._primary, self._fallback)
            if bool(getattr(client, "supports_grounded_web_research", False))
            and callable(getattr(client, "grounded_web_research", None))
        ]
        if not candidates:
            raise LLMUnavailableError("No configured provider supports grounded public web search.")
        for index, client in enumerate(candidates):
            try:
                return await client.grounded_web_research(prompt, urls=urls)
            except FALLBACK_ERRORS:
                if index + 1 >= len(candidates):
                    raise
                logger.warning(
                    "Grounded web search provider %s failed; trying configured web-capable fallback %s",
                    getattr(client, "provider", "unknown"),
                    getattr(candidates[index + 1], "provider", "unknown"),
                )
        raise LLMUnavailableError("Public web search is unavailable.")

    def __getattr__(self, name: str):
        primary_method = getattr(self._primary, name)
        # Verify checks the selected primary configuration; close has a dedicated dual-client implementation.
        if name in {"verify", "close"} or not callable(primary_method):
            return primary_method

        async def invoke(*args, **kwargs):
            emitted = False
            callback = kwargs.get("on_token")
            if callback is not None:
                async def track_token(token: str):
                    nonlocal emitted
                    emitted = emitted or bool(token)
                    result = callback(token)
                    if inspect.isawaitable(result):
                        await result
                kwargs["on_token"] = track_token
            try:
                return await primary_method(*args, **kwargs)
            except FALLBACK_ERRORS + ((VisionUnsupportedError,) if name == "describe_image" else ()) as exc:
                # Once any streaming content escaped, mixing a second model's answer into it would corrupt the
                # visible response. The UI will show a retryable error instead of concatenated providers.
                if name == "chat_stream" and emitted:
                    raise
                fallback_method = getattr(self._fallback, name, None)
                if fallback_method is None or not callable(fallback_method):
                    raise
                logger.warning(
                    "Provider %s operation %s failed (%s); switching to configured fallback %s",
                    getattr(self._primary, "provider", "unknown"), name, type(exc).__name__,
                    getattr(self._fallback, "provider", "unknown"),
                )
                if callback is not None:
                    kwargs["on_token"] = callback
                return await fallback_method(*args, **kwargs)

        return invoke

    async def close(self) -> None:
        primary = getattr(self._primary, "close", None)
        fallback = getattr(self._fallback, "close", None)
        if callable(primary):
            await primary()
        if callable(fallback) and self._fallback is not self._primary:
            await fallback()
