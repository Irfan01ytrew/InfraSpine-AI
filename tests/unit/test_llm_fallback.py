"""LLM_FALLBACK_PROVIDER: a provider failure on the active provider is answered by the fallback once."""

import asyncio

import pytest

from app.core.config import Settings
from app.core.errors import InvalidInputError, LLMRateLimitError, LLMTimeoutError, LLMUnavailableError
from app.services.llm_fallback import FallbackLLM
from app.services.llm_runtime import LLMRuntime


class Client:
    def __init__(self, provider: str, error: Exception | None = None) -> None:
        self.provider = provider
        self.error = error
        self.calls: list[tuple[str, str | None]] = []

    async def chat(self, message: str, *, system_prompt: str | None = None):
        self.calls.append((message, system_prompt))
        if self.error is not None:
            raise self.error
        return f"{self.provider}: {message}"


def _chat(client, message: str = "Q"):
    return asyncio.run(client.chat(message, system_prompt="S"))


def test_a_working_primary_answers_and_the_fallback_is_not_called():
    primary, fallback = Client("deepseek"), Client("ollama-local")
    assert _chat(FallbackLLM(primary, fallback)) == "deepseek: Q"
    assert fallback.calls == []


@pytest.mark.parametrize("error", [LLMUnavailableError("HTTP 402 insufficient balance"), LLMTimeoutError(), LLMRateLimitError()])
def test_provider_failures_are_answered_by_the_fallback_with_the_same_prompt(error):
    primary, fallback = Client("deepseek", error), Client("ollama-local")
    assert _chat(FallbackLLM(primary, fallback)) == "ollama-local: Q"
    assert primary.calls == fallback.calls == [("Q", "S")]


def test_invalid_requests_do_not_fall_back():
    primary, fallback = Client("deepseek", InvalidInputError("bad request")), Client("ollama-local")
    with pytest.raises(InvalidInputError):
        _chat(FallbackLLM(primary, fallback))
    assert fallback.calls == []


def test_when_both_fail_the_fallback_error_is_reported():
    primary, fallback = Client("deepseek", LLMUnavailableError()), Client("ollama-local", LLMTimeoutError())
    with pytest.raises(LLMTimeoutError):
        _chat(FallbackLLM(primary, fallback))


def test_image_edit_provider_failures_use_the_configured_image_capable_fallback():
    class ImageClient(Client):
        supports_image_generation = True

        def __init__(self, provider: str, error: Exception | None = None) -> None:
            super().__init__(provider, error)
            self.image_calls = []

        async def edit_image(self, image: bytes, prompt: str):
            self.image_calls.append((image, prompt))
            if self.error is not None:
                raise self.error
            return b"edited", "image/png", "image-model", self.provider

    primary = ImageClient("gemini", LLMRateLimitError())
    fallback = ImageClient("ollama-local")
    result = asyncio.run(FallbackLLM(primary, fallback).edit_image(b"source", "Change the background"))
    assert result == (b"edited", "image/png", "image-model", "ollama-local")
    assert primary.image_calls == fallback.image_calls == [(b"source", "Change the background")]


def test_image_edit_does_not_claim_support_when_no_provider_can_edit():
    primary, fallback = Client("deepseek"), Client("ollama-local")
    with pytest.raises(LLMUnavailableError, match="supports image editing"):
        asyncio.run(FallbackLLM(primary, fallback).edit_image(b"source", "change the background"))


def test_other_attributes_belong_to_the_primary():
    primary, fallback = Client("deepseek"), Client("ollama-local")
    assert FallbackLLM(primary, fallback).provider == "deepseek"


def _runtime(fallback: str, clients: dict) -> LLMRuntime:
    runtime = LLMRuntime(Settings(llm_fallback_provider=fallback), cipher=None)  # type: ignore[arg-type]

    async def fake_client(db, choice):
        key = "active" if choice is None else choice[0]
        if key not in clients:
            raise InvalidInputError("The selected model is not available. Choose another one.")
        return clients[key]

    runtime._client = fake_client  # type: ignore[method-assign]
    return runtime


def test_runtime_wraps_the_active_provider_only_when_a_usable_fallback_is_configured():
    active, local = Client("deepseek"), Client("ollama-local")
    wrapped = asyncio.run(_runtime("ollama-local", {"active": active, "ollama-local": local}).get_client(None))
    assert isinstance(wrapped, FallbackLLM)
    # No fallback configured, or the configured one is missing: the active client is returned unchanged.
    assert asyncio.run(_runtime("", {"active": active}).get_client(None)) is active
    assert asyncio.run(_runtime("ollama-local", {"active": active}).get_client(None)) is active
    # Chat always sends the picked model, so a pick of another provider falls back as well ...
    picked = asyncio.run(_runtime("ollama-local", {"deepseek": active, "ollama-local": local}).get_client(None, ("deepseek", "m")))
    assert isinstance(picked, FallbackLLM)
    # ... but picking the fallback itself, or the fallback being the active provider, is never wrapped.
    assert asyncio.run(_runtime("ollama-local", {"active": active, "ollama-local": local}).get_client(None, ("ollama-local", "m"))) is local
    assert asyncio.run(_runtime("deepseek", {"active": active, "deepseek": active}).get_client(None)) is active
