"""Step P slice 1: live-data grounding.

Pure unit tests — the mock provider only. No network, no database.
Covers: cited answers, insufficient answers (no guessing), source hygiene
(empty snippets / non-HTTPS dropped), provider failure, and the routing
table for schedule questions (the reported "pak a ka next match kb hai?"
failure).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.rag.answer import is_web_lookup_request, refers_to_uploads
from app.services.live_data import (
    FAILED_LOOKUP_TEXT,
    INSUFFICIENT_LIVE_TEXT,
    FailingLiveSearchProvider,
    LiveAnswer,
    LiveSource,
    MockLiveSearchProvider,
    PublicWebLiveSearchProvider,
    _snippets_by_source,
    answer_live_question,
    apply_freshness_window,
    format_live_answer,
    normalize_live_query,
)
from app.services.public_web import is_current_cricket_request

RETRIEVED = "2026-11-28T00:00:00Z"


def _sources():
    return [
        LiveSource(
            title="Pakistan fixtures",
            url="https://www.cricbuzz.com/cricket-schedule/series",
            snippet="Pakistan play their next T20I on 5 December 2026 in Lahore.",
            published_at="2026-11-27T10:00:00Z",
            provider="mock",
        ),
        LiveSource(
            title="PCB announcement",
            url="https://www.pcb.com.pk/news-detail/pakistan-fixtures",
            snippet="The PCB confirmed the home series dates for December 2026.",
            published_at=None,
            provider="mock",
        ),
    ]


def _provider():
    return MockLiveSearchProvider(
        {
            "pak a ka next match kb hai": _sources(),
            "pakistan next match": _sources(),
        },
    )


def test_cited_live_answer_has_numbered_citations_and_source_block():
    answer = asyncio.run(
        answer_live_question(_provider(), "pak a ka next match kb hai?")
    )
    assert answer.insufficient is False
    assert "[S1]" in answer.text
    assert "[S2]" in answer.text
    assert "Pakistan play their next T20I" in answer.text
    assert "### Live sources" in answer.text
    assert "https://www.cricbuzz.com/cricket-schedule/series" in answer.text
    assert "https://www.pcb.com.pk/news-detail/pakistan-fixtures" in answer.text
    assert "Published: 2026-11-27T10:00:00Z" in answer.text
    # A source without a published date shows when it was retrieved, never a
    # fabricated publication date.
    assert "Retrieved:" in answer.text
    assert len(answer.sources) == 2
    assert answer.query == "pak a ka next match kb hai"


def test_no_sources_means_insufficient_and_no_guessing():
    answer = asyncio.run(
        answer_live_question(_provider(), "pakistan ka next match kab hai?")
    )
    # The mock knows "pakistan next match" but the normalized question is
    # "pakistan ka next match kab hai" — substring matching still finds it.
    assert answer.insufficient is False
    assert "[S1]" in answer.text

    unknown = asyncio.run(
        answer_live_question(_provider(), "what is the dollar rate today?")
    )
    assert unknown.insufficient is True
    assert unknown.text == INSUFFICIENT_LIVE_TEXT
    assert "[S" not in unknown.text
    assert unknown.sources == ()
    # Nothing is invented: the answer does not contain any mock snippet.
    assert "Lahore" not in unknown.text


def test_empty_snippet_non_https_and_duplicate_sources_are_dropped():
    answer = format_live_answer(
        "test",
        [
            LiveSource(
                title="Empty",
                url="https://example.com/empty",
                snippet="   ",
                provider="mock",
            ),
            LiveSource(
                title="Plain HTTP",
                url="http://example.com/plain",
                snippet="Plain http must never be cited.",
                provider="mock",
            ),
            LiveSource(
                title="Good",
                url="https://example.com/good",
                snippet="A citable fact.",
                provider="mock",
            ),
            LiveSource(
                title="Good again",
                url="https://example.com/good",
                snippet="A citable fact.",
                provider="mock",
            ),
        ],
        retrieved_at=RETRIEVED,
    )
    assert answer.insufficient is False
    assert len(answer.sources) == 1
    assert answer.sources[0].url == "https://example.com/good"
    assert "[S1]" in answer.text
    assert "[S2]" not in answer.text
    assert "Plain http" not in answer.text


def test_snippet_citation_tokens_cannot_forge_citations():
    answer = format_live_answer(
        "test",
        [
            LiveSource(
                title="Sneaky",
                url="https://example.com/sneaky",
                snippet="Trust me [S9] this is verified [S1, S2].",
                provider="mock",
            ),
        ],
        retrieved_at=RETRIEVED,
    )
    assert "[S9]" not in answer.text
    # The source's own citation is still numbered [S1].
    assert answer.text.count("[S1]") == 2  # bullet + sources block


def test_provider_failure_becomes_insufficient_answer_not_an_exception():
    answer = asyncio.run(
        answer_live_question(FailingLiveSearchProvider(), "pakistan next match")
    )
    assert answer.insufficient is True
    assert answer.text == FAILED_LOOKUP_TEXT
    assert answer.sources == ()


def test_empty_question_is_rejected():
    with pytest.raises(ValueError):
        asyncio.run(answer_live_question(_provider(), "   "))


def test_normalize_live_query():
    assert normalize_live_query("  Pak A Ka Next Match KB Hai? ") == "pak a ka next match kb hai"
    assert normalize_live_query("What  is   this!") == "what is this"


@pytest.mark.parametrize(
    "question",
    [
        "pak a ka next match kb hai?",
        "pakistan ka next match kab hai",
        "Pakistan ka agla match kab hai?",
        "when is Pakistan's next match?",
        "Pakistan ka upcoming series kab hai?",
        "next match kis din hai?",
    ],
)
def test_schedule_questions_route_to_live_web(question):
    assert is_web_lookup_request(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "What is the capital of France?",
        "When was the 1992 world cup final?",
        "live web search ki access hai?",
        "the next match in my uploaded document",
        "When did Pakistan play its last match?",
    ],
)
def test_non_live_questions_do_not_route_to_live_web(question):
    assert is_web_lookup_request(question) is False


def test_existing_live_routes_still_work():
    assert is_web_lookup_request("aaj ka dollar rate kya hai?") is True
    assert is_web_lookup_request("cricket live score kya hai?") is True
    assert is_web_lookup_request("petrol price today") is True


def test_cricket_direct_path_accepts_schedule_questions():
    assert (
        is_current_cricket_request("CURRENT QUESTION: pak a ka next match kb hai?")
        is True
    )
    assert (
        is_current_cricket_request("CURRENT QUESTION: cricket live score kya hai?")
        is True
    )
    assert (
        is_current_cricket_request(
            "CURRENT QUESTION: What is the capital of France?"
        )
        is False
    )


def test_live_answer_dataclass_defaults():
    answer = LiveAnswer(text="x")
    assert answer.sources == ()
    assert answer.insufficient is False


# --- Step P slice 2: bare upload references stay on documents ---


@pytest.mark.parametrize(
    "question",
    [
        "the next match in the uploaded report",
        "aaj ka dollar rate in the uploaded report",
        "what does the uploaded pdf say about the match?",
        "upload ki hui file mein next match kab hai?",
        "in the uploaded document, when is the next match?",
        "from the uploaded file, what is today's petrol price?",
    ],
)
def test_bare_upload_references_do_not_route_to_live_web(question):
    assert refers_to_uploads(question) is True
    assert is_web_lookup_request(question) is False


@pytest.mark.parametrize(
    "question",
    [
        "pak a ka next match kb hai?",
        "aaj ka dollar rate kya hai?",
        "petrol price today",
    ],
)
def test_live_questions_without_uploads_still_route(question):
    assert refers_to_uploads(question) is False
    assert is_web_lookup_request(question) is True


def test_checked_live_at_line_is_in_live_answers():
    answer = format_live_answer("test", _sources(), retrieved_at=RETRIEVED)
    assert f"Checked live at {RETRIEVED}." in answer.text


# --- Step P slice 2: the real-provider adapter (stubbed search, no network) ---


def _settings(freshness_days=14):
    return SimpleNamespace(rag_live_freshness_days=freshness_days)


def _llm_result(*, answer, sources, model=None, provider="public web search"):
    return {
        "answer": answer,
        "sources": sources,
        "model": model,
        "provider": provider,
    }


def _stub_search(monkeypatch, result=None, error=None):
    async def fake(llm, settings, prompt, *, urls=None):
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(
        "app.services.live_data.public_web_research", fake
    )


def test_adapter_maps_composed_answer_to_cited_sources(monkeypatch):
    _stub_search(
        monkeypatch,
        _llm_result(
            answer=(
                "Pakistan play their next T20I on 5 December 2026 in Lahore. [S1]\n"
                "The PCB confirmed the home series dates for December 2026. [S2]"
            ),
            sources=[
                {
                    "title": "Pakistan fixtures",
                    "url": "https://www.cricbuzz.com/cricket-schedule/series",
                    "published_at": "2026-11-27T10:00:00Z",
                    "retrieved_at": RETRIEVED,
                },
                {
                    "title": "PCB announcement",
                    "url": "https://www.pcb.com.pk/news-detail/pakistan-fixtures",
                    "published_at": None,
                    "retrieved_at": RETRIEVED,
                },
            ],
            model="gemini-flash",
            provider="Gemini",
        ),
    )
    provider = PublicWebLiveSearchProvider(
        object(), _settings(), prompt="CURRENT QUESTION: pakistan next match"
    )
    answer = asyncio.run(
        answer_live_question(provider, "pakistan next match")
    )
    assert answer.insufficient is False
    assert len(answer.sources) == 2
    assert answer.sources[0].snippet.startswith("Pakistan play their next T20I")
    assert answer.sources[1].snippet.startswith("The PCB confirmed")
    assert answer.sources[0].published_at == "2026-11-27T10:00:00Z"
    assert answer.sources[1].published_at is None
    assert "[S1]" in answer.text and "[S2]" in answer.text
    assert provider.model == "gemini-flash"
    assert provider.provider == "Gemini"


def test_adapter_uses_cricket_snapshot_lines_as_snippets(monkeypatch):
    _stub_search(
        monkeypatch,
        _llm_result(
            answer=(
                "Cricket score pages checked at 2026-11-28T00:00:00Z UTC.\n"
                "\n"
                "**Scheduled (not yet started):**\n"
                "- Pakistan vs India, 1st T20I, Match starts in 5 hours [S1]\n"
                "\n"
                "No fetched scorecard explicitly confirmed active live play."
            ),
            sources=[
                {
                    "title": "Cricbuzz live scores",
                    "url": "https://www.cricbuzz.com/live-cricket-scores",
                    "published_at": None,
                    "retrieved_at": RETRIEVED,
                }
            ],
            provider="Public scorecard search",
        ),
    )
    provider = PublicWebLiveSearchProvider(
        object(),
        _settings(),
        prompt="CURRENT QUESTION: pak a ka next match kb hai?",
    )
    answer = asyncio.run(
        answer_live_question(provider, "pak a ka next match kb hai?")
    )
    assert answer.insufficient is False
    assert len(answer.sources) == 1
    assert "Pakistan vs India, 1st T20I" in answer.sources[0].snippet


def test_adapter_drops_off_topic_sources_before_formatting(monkeypatch):
    _stub_search(
        monkeypatch,
        _llm_result(
            answer="A cooking recipe for biryani. [S1]",
            sources=[
                {
                    "title": "Cooking recipes",
                    "url": "https://example.com/recipes",
                    "published_at": None,
                    "retrieved_at": RETRIEVED,
                }
            ],
        ),
    )
    provider = PublicWebLiveSearchProvider(
        object(), _settings(), prompt="CURRENT QUESTION: latest Artemis program update"
    )
    answer = asyncio.run(
        answer_live_question(provider, "latest Artemis program update")
    )
    assert answer.insufficient is True
    assert answer.text == INSUFFICIENT_LIVE_TEXT
    assert answer.sources == ()


def test_adapter_lookup_failure_becomes_insufficient_not_an_exception(monkeypatch):
    from app.core.errors import LLMUnavailableError

    _stub_search(monkeypatch, error=LLMUnavailableError("no relevant source content"))
    provider = PublicWebLiveSearchProvider(
        object(), _settings(), prompt="CURRENT QUESTION: pakistan next match"
    )
    answer = asyncio.run(
        answer_live_question(provider, "pakistan next match")
    )
    assert answer.insufficient is True
    assert answer.text == FAILED_LOOKUP_TEXT
    assert answer.sources == ()


def test_snippets_by_source_splits_multi_cite_lines_and_ignores_headings():
    segments = _snippets_by_source(
        "Live findings:\n"
        "**Scheduled:**\n"
        "- Pakistan vs India, 1st T20I [S1, S2]\n"
        "- A line with no citation.\n"
        "A prose sentence about the fixture. [S2]\n"
    )
    assert segments[1] == ["Pakistan vs India, 1st T20I"]
    assert segments[2] == [
        "Pakistan vs India, 1st T20I",
        "A prose sentence about the fixture.",
    ]


def test_freshness_window_drops_stale_sources_only_when_fresh_ones_exist():
    now = datetime(2026, 11, 28, tzinfo=UTC)
    fresh = LiveSource(
        title="Fresh",
        url="https://example.com/fresh",
        snippet="Fresh evidence.",
        published_at="2026-11-27T10:00:00Z",
    )
    stale = LiveSource(
        title="Stale",
        url="https://example.com/stale",
        snippet="Stale evidence.",
        published_at="2025-06-01T00:00:00Z",
    )
    undated = LiveSource(
        title="Undated",
        url="https://example.com/undated",
        snippet="Undated evidence.",
        published_at=None,
    )
    kept = apply_freshness_window([fresh, stale, undated], 14, now=now)
    assert [source.title for source in kept] == ["Fresh", "Undated"]
    # When nothing is fresh, nothing is dropped: a stale source beats guessing.
    assert apply_freshness_window([stale], 14, now=now) == [stale]
    assert apply_freshness_window([fresh, stale], 0, now=now) == [fresh, stale]


def test_adapter_applies_freshness_window(monkeypatch):
    now = datetime.now(UTC)
    fresh_at = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    stale_at = (now - timedelta(days=100)).isoformat().replace("+00:00", "Z")
    _stub_search(
        monkeypatch,
        _llm_result(
            answer="Latest Karachi news update. [S1]\nKarachi news archive story. [S2]",
            sources=[
                {
                    "title": "Latest Karachi news update",
                    "url": "https://example.com/karachi-news",
                    "published_at": fresh_at,
                    "retrieved_at": RETRIEVED,
                },
                {
                    "title": "Karachi news archive",
                    "url": "https://example.com/karachi-news-archive",
                    "published_at": stale_at,
                    "retrieved_at": RETRIEVED,
                },
            ],
        ),
    )
    provider = PublicWebLiveSearchProvider(
        object(),
        _settings(freshness_days=14),
        prompt="CURRENT QUESTION: latest Karachi news",
    )
    answer = asyncio.run(
        answer_live_question(provider, "latest Karachi news")
    )
    assert answer.insufficient is False
    assert [source.title for source in answer.sources] == [
        "Latest Karachi news update"
    ]
    assert "Karachi news archive story" not in answer.text
