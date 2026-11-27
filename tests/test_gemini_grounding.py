"""Public market research stays on the configured Gemini origin and preserves safe citations."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.routes.rag import market_research
from app.core.errors import InvalidInputError
from app.providers import openai_compatible
from app.providers.openai_compatible import OpenAICompatibleClient, _grounded_interaction_result, _safe_grounding_url
from app.schemas.rag import MarketResearchRequest
from app.services.llm import ClientParams


def _grounded_body():
    text = "Málaga competitor announced a new service."
    return {
        "status": "completed",
        "steps": [{
            "type": "model_output",
            "content": [{
                "type": "text",
                "text": text,
                "annotations": [
                    {
                        "type": "url_citation",
                        "start_index": 0,
                        "end_index": len(text.encode("utf-8")),
                        "title": "Public announcement",
                        "url": "https://example.com/news",
                    },
                    {
                        "type": "url_citation",
                        "start_index": 0,
                        "end_index": len(text.encode("utf-8")),
                        "title": "Unsafe target",
                        "url": "https://127.0.0.1/private",
                    },
                ],
            }],
        }],
    }


def test_grounded_response_keeps_only_safe_https_citations_and_marks_answer():
    result = _grounded_interaction_result(_grounded_body(), "gemini-2.5-flash", "gemini")

    assert result["answer"].endswith("[S1]")
    assert result["sources"] == [{"title": "Public announcement", "url": "https://example.com/news"}]
    assert _safe_grounding_url("https://example.com/article")
    assert not _safe_grounding_url("http://example.com/article")
    assert not _safe_grounding_url("https://localhost/private")
    assert not _safe_grounding_url('https' + '://' + 'user:pass' + '@' + 'example.com/private')


def test_grounded_request_reuses_guarded_gemini_client_and_key_header(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return _grounded_body()

    class FakeHttpClient:
        async def post(self, url, *, headers, json, timeout):
            calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return FakeResponse()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(openai_compatible, "AsyncOpenAI", FakeOpenAI)
    client = OpenAICompatibleClient(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        ClientParams(
            api_key="secret-test-key",
            model="gemini-2.5-flash",
            settings={"max_tokens": 1024, "temperature": 0},
            timeout_seconds=30,
            max_retries=0,
        ),
        http_client=FakeHttpClient(),
    )

    result = asyncio.run(client.grounded_web_research("Compare public products"))

    assert client.supports_grounded_web_research
    assert result["sources"][0]["url"] == "https://example.com/news"
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://generativelanguage.googleapis.com/v1beta/interactions"
    assert call["headers"]["x-goog-api-key"] == "secret-test-key"
    assert "secret-test-key" not in call["url"]
    assert call["json"]["tools"] == [{"type": "google_search"}]
    assert call["json"]["generation_config"]["max_output_tokens"] == 4096


def test_unsupported_gemini_model_cannot_start_grounded_research(monkeypatch):
    class FakeOpenAI:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(openai_compatible, "AsyncOpenAI", FakeOpenAI)
    client = OpenAICompatibleClient(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        ClientParams(
            api_key="secret-test-key",
            model="gemini-2.0-flash",
            settings={"max_tokens": 1024, "temperature": 0},
            timeout_seconds=30,
            max_retries=0,
        ),
        http_client=object(),
    )
    assert not client.supports_grounded_web_research


def test_public_research_consent_defaults_off_and_model_override_is_rejected():
    request = MarketResearchRequest(topic="Compare public competitors")
    assert request.confirm_external_search is False
    with pytest.raises(ValidationError):
        MarketResearchRequest(topic="Compare public competitors", provider="other", model="other-model")


def test_market_research_route_requires_consent_and_sends_only_public_topic():
    class FakeGemini:
        provider = "gemini"
        _model = "gemini-2.5-flash"
        supports_grounded_web_research = True

        def __init__(self):
            self.prompts = []

        async def grounded_web_research(self, prompt):
            assert db.commits == 1  # Consent/disclosure audit is durable before the provider call.
            self.prompts.append(prompt)
            return {
                "answer": "Publicly sourced comparison [S1]",
                "sources": [{"title": "Source", "url": "https://example.com"}],
                "model": self._model,
                "provider": self.provider,
            }

    client = FakeGemini()

    class FakeDb:
        def __init__(self):
            self.entries = []
            self.commits = 0

        def add(self, entry):
            self.entries.append(entry)

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            pass

    db = FakeDb()

    class FakeRuntime:
        async def get_client(self, db):
            return client

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(llm_runtime=FakeRuntime(), sessionmaker=None)),
        state=SimpleNamespace(request_id="test-request"),
        client=SimpleNamespace(host="127.0.0.1"),
    )
    settings = SimpleNamespace(rag_external_research_enabled=True, rag_trace_enabled=False)
    user = SimpleNamespace(id=uuid.uuid4(), email="researcher@example.com")
    with pytest.raises(InvalidInputError):
        asyncio.run(market_research(
            MarketResearchRequest(topic="Compare two public products"), request, db, user, settings
        ))
    assert client.prompts == []

    report = asyncio.run(market_research(
        MarketResearchRequest(topic="Compare two public products", confirm_external_search=True),
        request,
        db,
        user,
        settings,
    ))
    assert len(client.prompts) == 1
    assert "Compare two public products" in client.prompts[0]
    assert "internal company information" in client.prompts[0]
    assert "confidential internal handbook contents" not in client.prompts[0]
    assert report.sources[0].url == "https://example.com"
    assert db.entries[0].action == "market_research.external_search_requested"
    assert db.entries[0].details["topic_chars"] == len("Compare two public products")
