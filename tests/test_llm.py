"""OpenAICompatibleClient tests: the OpenAI SDK calls are patched, so no network is used."""

import asyncio

import httpx
import openai
import pytest
from openai.types.chat import ChatCompletion

from app.core.errors import LLMRateLimitError, LLMTimeoutError, LLMUnavailableError
from app.providers.openai_compatible import OpenAICompatibleClient
from app.services.llm import ClientParams, ProviderVerificationError

REQUEST = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
PARAMS = ClientParams(
    api_key="sk-test-key-123456789",
    model="deepseek-flash",
    settings={"temperature": 0.7, "max_tokens": 64, "thinking": "disabled"},
    timeout_seconds=5,
    max_retries=0,
)


def _status_error(cls, code):
    return cls("error", response=httpx.Response(code, request=REQUEST), body=None)


def make_client(monkeypatch, result=None, error=None, calls=None) -> OpenAICompatibleClient:
    client = OpenAICompatibleClient(
        "deepseek", "https://api.deepseek.com", PARAMS, extra_body=lambda s: {"thinking": {"type": s["thinking"]}}
    )

    async def fake_create(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)
    return client


def make_verifying_client(monkeypatch, model_ids=(), error=None) -> OpenAICompatibleClient:
    client = OpenAICompatibleClient("deepseek", "https://api.deepseek.com", PARAMS)

    class FakeModels:
        async def _iterate(self):
            if error is not None:
                raise error
            for model_id in model_ids:
                yield type("Model", (), {"id": model_id})()

        def list(self):
            return self._iterate()

    fake = type("FakeClient", (), {"models": FakeModels()})()
    monkeypatch.setattr(client._client, "with_options", lambda **kwargs: fake)
    return client


def test_params_repr_hides_api_key():
    assert "sk-test-key" not in repr(PARAMS)


def test_successful_completion(monkeypatch):
    completion = ChatCompletion.model_validate(
        {
            "id": "cmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Hi"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }
    )
    calls = []
    response = asyncio.run(make_client(monkeypatch, result=completion, calls=calls).chat("hello"))
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert calls[0]["max_tokens"] == 64
    assert response.reply == "Hi"
    assert response.provider == "deepseek"
    assert response.usage.total_tokens == 7


def test_describe_image_sends_the_picture_and_reports_unsupported_models(monkeypatch):
    from app.services.llm import VisionUnsupportedError

    completion = ChatCompletion.model_validate(
        {
            "id": "cmpl-2",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "A bar chart"}}],
        }
    )
    calls = []
    response = asyncio.run(make_client(monkeypatch, result=completion, calls=calls).describe_image(b"\x89PNG", "Describe"))
    content = calls[0]["messages"][0]["content"]
    assert response.reply == "A bar chart"
    assert content[0] == {"type": "text", "text": "Describe"}
    assert content[1]["image_url"]["url"] == "data:image/png;base64,iVBORw=="

    for refusal in (_status_error(openai.BadRequestError, 400), _status_error(openai.UnprocessableEntityError, 422)):
        with pytest.raises(VisionUnsupportedError):
            asyncio.run(make_client(monkeypatch, error=refusal).describe_image(b"\x89PNG", "Describe"))
    with pytest.raises(LLMTimeoutError):  # an outage is not mistaken for missing vision support
        asyncio.run(make_client(monkeypatch, error=openai.APITimeoutError(request=REQUEST)).describe_image(b"x", "Describe"))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (openai.APITimeoutError(request=REQUEST), LLMTimeoutError),
        (_status_error(openai.RateLimitError, 429), LLMRateLimitError),
        (_status_error(openai.AuthenticationError, 401), LLMUnavailableError),
        (_status_error(openai.InternalServerError, 500), LLMUnavailableError),
        (openai.APIConnectionError(request=REQUEST), LLMUnavailableError),
    ],
)
def test_chat_errors_are_mapped(monkeypatch, error, expected):
    with pytest.raises(expected):
        asyncio.run(make_client(monkeypatch, error=error).chat("hello"))


def test_deepseek_insufficient_balance_is_actionable_without_provider_body(monkeypatch):
    error = openai.APIStatusError(
        "provider body must not be exposed",
        response=httpx.Response(402, request=REQUEST, text='{"message":"provider body must not be exposed"}'),
        body={"message": "provider body must not be exposed"},
    )
    with pytest.raises(LLMUnavailableError) as exc_info:
        asyncio.run(make_client(monkeypatch, error=error).chat("hello"))

    assert "insufficient API balance (HTTP 402)" in str(exc_info.value)
    assert "top up before retrying" in str(exc_info.value)
    assert "provider body must not be exposed" not in str(exc_info.value)


def test_verify_succeeds_when_model_is_listed(monkeypatch):
    client = make_verifying_client(monkeypatch, model_ids=["deepseek-flash", "deepseek-v4-pro"])
    asyncio.run(client.verify(5))


@pytest.mark.parametrize(
    ("model_ids", "error", "reason"),
    [
        (["other-model"], None, "model_unavailable"),
        ((), _status_error(openai.AuthenticationError, 401), "invalid_credentials"),
        ((), _status_error(openai.PermissionDeniedError, 403), "invalid_credentials"),
        ((), _status_error(openai.RateLimitError, 429), "rate_limited"),
        ((), openai.APITimeoutError(request=REQUEST), "timeout"),
        ((), openai.APIConnectionError(request=REQUEST), "provider_unavailable"),
        ((), _status_error(openai.InternalServerError, 500), "provider_unavailable"),
    ],
)
def test_verify_failures_are_classified(monkeypatch, model_ids, error, reason):
    client = make_verifying_client(monkeypatch, model_ids=model_ids, error=error)
    with pytest.raises(ProviderVerificationError) as exc_info:
        asyncio.run(client.verify(5))
    assert exc_info.value.reason == reason


def test_reply_cut_off_by_token_limit_gets_a_clear_error(monkeypatch):
    completion = ChatCompletion.model_validate(
        {
            "id": "cmpl-2",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": ""}}],
        }
    )
    with pytest.raises(LLMUnavailableError, match="output tokens"):
        asyncio.run(make_client(monkeypatch, result=completion).chat("hello"))
