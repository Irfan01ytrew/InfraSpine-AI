"""Run the Step P live-data eval set (slice 4).

Deterministic and offline by default: it runs the fixed eval questions
against the mock provider (canned sources) and against a failing provider,
and prints citation / no-guess / expected rates. It asserts nothing about
real-world facts — only about citations, timestamps, and refusal behaviour.

Usage (from the repo root, with the project venv):

    .venv/bin/python scripts/eval_live_data.py

Exit code is 0 when every mock result matches its expectation and the
failing-provider run never guesses; otherwise 1.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.live_data import (  # noqa: E402
    FailingLiveSearchProvider,
    LiveSource,
    MockLiveSearchProvider,
)
from app.services.live_data_eval import run_live_evals  # noqa: E402

RETRIEVED = "2026-11-28T00:00:00Z"


def _mock_provider() -> MockLiveSearchProvider:
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


async def main() -> int:
    now = datetime(2026, 11, 28, tzinfo=UTC)
    mock_report = await run_live_evals(_mock_provider(), now=now)
    print("== mock provider ==")
    print(mock_report.summary())
    failing_report = await run_live_evals(FailingLiveSearchProvider(), now=now)
    print()
    print("== failing provider (lookup always fails) ==")
    print(failing_report.summary())
    ok = (
        mock_report.expected_rate == 1.0
        and mock_report.no_guess_rate == 1.0
        and all(result.timestamps_ok for result in mock_report.results)
        and failing_report.answered == 0
        and failing_report.no_guess_rate == 1.0
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
