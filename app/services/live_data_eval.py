"""Fixed evaluation set for live-data grounding (Step P slice 4).

Runs a fixed set of live questions against any ``LiveSearchProvider`` and
measures what the plan requires:

- ``citation_rate`` — the share of eval questions that got a cited answer.
- ``no_guess_rate`` — the share of results that never guessed: an
  insufficient answer carries zero citations and no facts, and every
  answered question carries at least one citation.
- ``expected_rate`` — the share of results matching what the case expects
  (answered vs insufficient).

Never asserts on real-world facts — they change. It asserts on citations,
freshness fields, and refusal behaviour. Real providers are exercised by
``scripts/eval_live_data.py`` (manual, needs network and credentials);
unit tests use the mock provider only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.services.live_data import (
    LiveSearchProvider,
    answer_live_question,
)

NOW = "2026-11-28T00:00:00Z"


@dataclass(frozen=True)
class LiveEvalCase:
    """One eval question. ``expect_answered`` is about the provider's
    coverage, never about a real-world fact."""

    question: str
    kind: str  # schedule | live_score | rate | news | unknown
    expect_answered: bool


LIVE_EVAL_QUESTIONS: tuple[LiveEvalCase, ...] = (
    LiveEvalCase("pak a ka next match kb hai?", "schedule", True),
    LiveEvalCase("pakistan next match", "schedule", True),
    LiveEvalCase("cricket live score kya hai?", "live_score", True),
    LiveEvalCase("aaj ka dollar rate kya hai?", "rate", True),
    LiveEvalCase("latest Karachi news", "news", True),
    LiveEvalCase("what is the capital of France?", "unknown", False),
    LiveEvalCase("when was the 1992 world cup final?", "unknown", False),
)


@dataclass(frozen=True)
class LiveEvalResult:
    case: LiveEvalCase
    insufficient: bool
    citations: int
    timestamps_ok: bool  # every cited source carries a retrieved timestamp
    no_guess_ok: bool
    expected_ok: bool


@dataclass(frozen=True)
class LiveEvalReport:
    total: int
    answered: int
    insufficient: int
    citation_rate: float
    no_guess_rate: float
    expected_rate: float
    results: tuple[LiveEvalResult, ...] = ()

    def summary(self) -> str:
        lines = [
            f"Live-data eval: {self.answered}/{self.total} answered, "
            f"{self.insufficient} insufficient (no guess).",
            f"citation_rate={self.citation_rate:.2f} "
            f"no_guess_rate={self.no_guess_rate:.2f} "
            f"expected_rate={self.expected_rate:.2f}",
        ]
        for result in self.results:
            status = "answered" if not result.insufficient else "insufficient"
            lines.append(
                f"- [{result.case.kind}] {result.case.question} -> {status} "
                f"({result.citations} citations)"
                + ("" if result.expected_ok else "  UNEXPECTED")
            )
        return "\n".join(lines)


async def run_live_evals(
    provider: LiveSearchProvider,
    cases: tuple[LiveEvalCase, ...] = LIVE_EVAL_QUESTIONS,
    *,
    now: datetime | None = None,
) -> LiveEvalReport:
    """Run the eval set against a provider. Never raises for a provider
    failure — a failed lookup is recorded as an insufficient result."""
    results: list[LiveEvalResult] = []
    for case in cases:
        answer = await answer_live_question(provider, case.question, now=now)
        citations = len(answer.sources)
        no_guess_ok = (
            answer.insufficient
            and citations == 0
            and "[S" not in answer.text
        ) or (not answer.insufficient and citations >= 1)
        results.append(
            LiveEvalResult(
                case=case,
                insufficient=answer.insufficient,
                citations=citations,
                timestamps_ok=all(source.retrieved_at for source in answer.sources),
                no_guess_ok=no_guess_ok,
                expected_ok=(not answer.insufficient) == case.expect_answered,
            )
        )
    answered = sum(1 for result in results if not result.insufficient)
    total = len(results)
    return LiveEvalReport(
        total=total,
        answered=answered,
        insufficient=total - answered,
        citation_rate=(answered / total) if total else 0.0,
        no_guess_rate=(
            sum(1 for result in results if result.no_guess_ok) / total
        )
        if total
        else 0.0,
        expected_rate=(
            sum(1 for result in results if result.expected_ok) / total
        )
        if total
        else 0.0,
        results=tuple(results),
    )
