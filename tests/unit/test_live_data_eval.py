"""Step P slice 4: live follow-up actions + evals.

Pure unit tests — the mock provider only. No network, no database.
The evals assert on citations, timestamps, and refusal behaviour, never
on real-world facts (they change).
"""

import asyncio
from datetime import UTC, datetime

from app.services.live_data import (
    FailingLiveSearchProvider,
    LiveAnswer,
    LiveSource,
    MockLiveSearchProvider,
    answer_live_question,
    live_followup_actions,
)
from app.services.live_data_eval import LIVE_EVAL_QUESTIONS, run_live_evals

NOW = datetime(2026, 11, 28, tzinfo=UTC)
RETRIEVED = "2026-11-28T00:00:00Z"


def _provider():
    fixture = LiveSource(
        title="Pakistan fixtures",
        url="https://www.cricbuzz.com/cricket-schedule/series",
        snippet="Pakistan play their next T20I on 5 December 2026 in Lahore.",
        published_at="2026-11-27T10:00:00Z",
        provider="mock",
    )
    score = LiveSource(
        title="Live cricket scores",
        url="https://www.cricbuzz.com/live-cricket-scores",
        snippet="Pakistan 250/4 after 48.2 overs.",
        published_at=None,
        provider="mock",
    )
    rate = LiveSource(
        title="Dollar rate today",
        url="https://example.com/dollar-rate",
        snippet="The dollar rate today is 278.50 PKR.",
        published_at="2026-11-28T08:00:00Z",
        provider="mock",
    )
    news = LiveSource(
        title="Latest Karachi news update",
        url="https://example.com/karachi-news",
        snippet="Karachi news update for today.",
        published_at="2026-11-28T07:00:00Z",
        provider="mock",
    )
    return MockLiveSearchProvider(
        {
            "pak a ka next match kb hai": [fixture],
            "pakistan next match": [fixture],
            "cricket live score": [score],
            "dollar rate": [rate],
            "latest karachi news": [news],
        }
    )


def test_answered_schedule_question_offers_an_approval_gated_reminder():
    answer = asyncio.run(
        answer_live_question(_provider(), "pak a ka next match kb hai?", now=NOW)
    )
    assert answer.insufficient is False
    assert len(answer.followup_actions) == 1
    action = answer.followup_actions[0]
    assert action.kind == "reminder"
    assert action.label == "Remind me before this event"
    assert action.requires_approval is True


def test_insufficient_and_non_schedule_answers_offer_nothing():
    unknown = asyncio.run(
        answer_live_question(
            _provider(), "what is the capital of France?", now=NOW
        )
    )
    assert unknown.insufficient is True
    assert unknown.followup_actions == ()
    assert live_followup_actions("pak a ka next match kb hai?", unknown) == ()

    rate = asyncio.run(
        answer_live_question(_provider(), "aaj ka dollar rate kya hai?", now=NOW)
    )
    assert rate.insufficient is False
    assert rate.followup_actions == ()


def test_mock_eval_never_guesses_and_matches_expectations():
    report = asyncio.run(run_live_evals(_provider(), now=NOW))
    assert report.total == len(LIVE_EVAL_QUESTIONS) == 7
    assert report.answered == 5
    assert report.insufficient == 2
    assert report.citation_rate == 5 / 7
    assert report.no_guess_rate == 1.0
    assert report.expected_rate == 1.0
    assert all(result.timestamps_ok for result in report.results)
    assert all(result.citations >= 1 for result in report.results if not result.insufficient)
    assert all(result.citations == 0 for result in report.results if result.insufficient)
    assert "citation_rate=0.71" in report.summary()
    assert "no_guess_rate=1.00" in report.summary()


def test_failing_provider_eval_answers_nothing_and_never_guesses():
    report = asyncio.run(run_live_evals(FailingLiveSearchProvider(), now=NOW))
    assert report.answered == 0
    assert report.insufficient == report.total
    assert report.citation_rate == 0.0
    assert report.no_guess_rate == 1.0
    assert all(result.no_guess_ok for result in report.results)


def test_eval_results_carry_kinds_and_expected_flags():
    report = asyncio.run(run_live_evals(_provider(), now=NOW))
    by_kind = {result.case.kind: result for result in report.results}
    assert by_kind["schedule"].expected_ok is True
    assert by_kind["unknown"].insufficient is True
    assert by_kind["unknown"].expected_ok is True
