"""Live-data grounding: cited live answers from a search provider.

Step P slice 1. This module is the seam every later slice uses: a small
provider interface, a mock provider for tests and demos, and an answer
composer that cites every live fact and refuses to guess when no live source
covers the question.

Trust rules (handover §5): web pages and search snippets are untrusted
input. They are quoted as evidence with citations, never treated as
instructions, and they can never grant a permission, change a payee, or
change what an approval covers. Only public HTTPS URLs are ever cited.

Slice 2 adds the real-provider adapter over the existing public web search
(``PublicWebLiveSearchProvider``) plus the live-answer freshness window.
No real external call happens in tests — the adapter is exercised with a
stubbed ``public_web_research``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from app.services.public_web import (
    is_current_cricket_request,
    public_sources_relevant,
    public_web_research,
)

logger = logging.getLogger(__name__)

INSUFFICIENT_LIVE_TEXT = (
    "I couldn't verify a current answer from live sources, so I won't guess. "
    "Please retry shortly or try a more specific question."
)
FAILED_LOOKUP_TEXT = (
    "I couldn't complete a live lookup right now, so I won't present a "
    "changing fact as current. Please retry shortly."
)

_CITATION_IN_SNIPPET = re.compile(r"\[S\d+(?:\s*[,;]\s*S\d+)*\]")


@dataclass(frozen=True)
class LiveSource:
    """One cited live source. ``published_at`` is shown only when the source
    actually published one — a missing date is never invented."""

    title: str
    url: str
    snippet: str
    published_at: str | None = None
    retrieved_at: str = ""
    provider: str = ""


@dataclass(frozen=True)
class LiveAnswer:
    """A live-grounded answer. ``insufficient`` is True exactly when no live
    source could verify a current answer; in that case ``text`` refuses to
    guess and ``sources`` is empty."""

    text: str
    sources: tuple[LiveSource, ...] = ()
    insufficient: bool = False
    retrieved_at: str = ""
    query: str = ""
    followup_actions: tuple["LiveFollowupAction", ...] = ()


@dataclass(frozen=True)
class LiveFollowupAction:
    """A follow-up action a live answer may OFFER (slice 4).

    Proposals only — nothing is executed here. ``requires_approval`` is
    always True: when the user asks for the action, it goes through the
    normal authorization and approval path first.
    """

    kind: str
    label: str
    detail: str
    requires_approval: bool = True


_SCHEDULE_LIKE = re.compile(
    r"\b(?:next|upcoming|agla|agli|aane|aanay)\b.{0,40}\b(?:match|matches|game|games|fixture|fixtures|series|tournament|concert|event|election|launch)\b"
    r"|\b(?:match|matches|fixture|fixtures|series)\b.{0,40}\b(?:kb|kab|when|kis\s+din|kis\s+tarikh)\b",
    re.IGNORECASE,
)


def live_followup_actions(
    question: str, answer: LiveAnswer
) -> tuple[LiveFollowupAction, ...]:
    """Propose follow-up actions for a sufficient live answer.

    A schedule question that was actually answered ("when is the next
    match?") may offer a reminder before the event. Insufficient answers
    propose nothing — there is no event to remind about — and non-schedule
    questions propose nothing. The proposal never creates the reminder.
    """
    if answer.insufficient or not answer.sources:
        return ()
    if _SCHEDULE_LIKE.search(question or ""):
        return (
            LiveFollowupAction(
                kind="reminder",
                label="Remind me before this event",
                detail=(
                    "Ask me to remind you before the event starts. "
                    "I will confirm with you first; no reminder is "
                    "created without your approval."
                ),
            ),
        )
    return ()


class LiveSearchProvider(Protocol):
    """A live search provider. Implementations must return only sources they
    actually retrieved; an empty sequence means "I know nothing about this"."""

    async def search(self, query: str) -> Sequence[LiveSource]:
        ...


def normalize_live_query(question: str) -> str:
    """Normalize a question for provider lookup: trim, lowercase, collapse
    whitespace, drop a trailing question mark or full stop."""
    value = " ".join(str(question or "").strip().lower().split())
    return value.rstrip("?.!").strip()


def _is_public_https_url(url: str) -> bool:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname)


def _clean_snippet(snippet: str) -> str:
    """Neutralize citation-looking tokens inside untrusted snippet text so a
    source can never forge our own [S#] citations."""
    value = _CITATION_IN_SNIPPET.sub(
        lambda match: match.group().replace("S", "W"), str(snippet or "")
    )
    return " ".join(value.split())


def _usable_sources(
    sources: Sequence[LiveSource], retrieved_at: str
) -> list[LiveSource]:
    """Keep only citable sources: non-empty snippet, public HTTPS URL.
    Stamps ``retrieved_at`` on sources that lack one. Deduplicates by URL,
    preserving order."""
    seen: set[str] = set()
    usable: list[LiveSource] = []
    for source in sources or []:
        snippet = _clean_snippet(source.snippet)
        if not snippet:
            continue
        if not _is_public_https_url(source.url):
            continue
        if source.url in seen:
            continue
        seen.add(source.url)
        usable.append(
            LiveSource(
                title=(source.title or "Live source").strip()[:200],
                url=source.url,
                snippet=snippet,
                published_at=(source.published_at or "").strip() or None,
                retrieved_at=source.retrieved_at or retrieved_at,
                provider=source.provider,
            )
        )
    return usable


def format_live_answer(
    query: str, sources: Sequence[LiveSource], *, retrieved_at: str
) -> LiveAnswer:
    """Compose a cited live answer from retrieved sources only.

    Every fact comes from a source snippet and carries an [S#] citation.
    With no usable source the answer is insufficient and refuses to guess.
    """
    usable = _usable_sources(sources, retrieved_at)
    if not usable:
        return LiveAnswer(
            text=INSUFFICIENT_LIVE_TEXT,
            sources=(),
            insufficient=True,
            retrieved_at=retrieved_at,
            query=query,
        )
    lines = ["Live findings:"]
    for index, source in enumerate(usable, 1):
        lines.append(f"- [S{index}] {source.snippet}")
    lines.append("")
    lines.append("### Live sources")
    for index, source in enumerate(usable, 1):
        timestamp = (
            f" · Published: {source.published_at}"
            if source.published_at
            else f" · Retrieved: {source.retrieved_at}"
        )
        lines.append(f"- [S{index}] [{source.title}](<{source.url}>){timestamp}")
    lines.append("")
    lines.append(f"Checked live at {retrieved_at}.")
    return LiveAnswer(
        text="\n".join(lines),
        sources=tuple(usable),
        insufficient=False,
        retrieved_at=retrieved_at,
        query=query,
    )


class MockLiveSearchProvider:
    """In-memory provider for tests and demos. ``answers`` maps a normalized
    query to the sources that answer it. Anything unmapped returns no
    sources — the mock never invents an answer."""

    def __init__(self, answers: Mapping[str, Sequence[LiveSource]] | None = None):
        self._answers: list[tuple[str, tuple[LiveSource, ...]]] = [
            (normalize_live_query(key), tuple(value))
            for key, value in (answers or {}).items()
        ]

    async def search(self, query: str) -> Sequence[LiveSource]:
        normalized = normalize_live_query(query)
        if not normalized:
            return []
        query_tokens = set(normalized.split())
        for key, sources in self._answers:
            if not key:
                continue
            # Match when every word of the key appears in the question
            # (order-independent), or when one string contains the other.
            # The mock answers only what it was told; anything else is [].
            if set(key.split()) <= query_tokens or key in normalized or normalized in key:
                return list(sources)
        return []


class FailingLiveSearchProvider:
    """Provider that always fails, for tests: a lookup failure must become an
    insufficient answer, never an exception to the caller."""

    async def search(self, query: str) -> Sequence[LiveSource]:
        raise RuntimeError("mock provider unavailable")


_SNIPPET_CITATION_GROUP = re.compile(r"\[S\d+(?:\s*[,;]\s*S\d+)*\]")
_DEFAULT_SNIPPET_CHARS = 600


def _snippets_by_source(answer: str) -> dict[int, list[str]]:
    """Split a composed public-web answer into per-source evidence segments.

    ``public_web_research`` returns sources without page text; the evidence
    lives in the composed answer, where each cited line carries an ``[S#]``
    token (cricket snapshot lines, extractive excerpts, or synthesized prose).
    A line citing several sources (``[S1, S2]``) contributes its text to
    each of them. Lines with no citation token (headings, caveats) carry no
    evidence.
    """
    segments: dict[int, list[str]] = {}
    for line in str(answer or "").splitlines():
        groups = _SNIPPET_CITATION_GROUP.findall(line)
        if not groups:
            continue
        tokens = {
            int(digit)
            for group in groups
            for digit in re.findall(r"S(\d+)", group)
        }
        text = " ".join(_SNIPPET_CITATION_GROUP.sub("", line).strip(" -*#\t").split())
        if not text:
            continue
        for token in tokens:
            segments.setdefault(token, []).append(text)
    return segments


def _parse_published_at(value: str | None) -> datetime | None:
    """Parse a source's published timestamp; unparseable means undated, never
    an invented date."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def apply_freshness_window(
    sources: Sequence[LiveSource],
    freshness_days: int,
    *,
    now: datetime | None = None,
) -> list[LiveSource]:
    """Prefer sources published within the freshness window.

    When at least one source is dated within the window, dated sources older
    than the window are dropped; undated sources are always kept (their
    missing date is shown as missing, never invented). When nothing is
    fresh, everything is kept — a stale citable source beats guessing.
    """
    ordered = list(sources)
    if freshness_days <= 0:
        return ordered
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = reference - timedelta(days=freshness_days)
    dated = [(source, _parse_published_at(source.published_at)) for source in ordered]
    if not any(parsed is not None and parsed >= cutoff for _, parsed in dated):
        return ordered
    return [
        source
        for source, parsed in dated
        if parsed is None or parsed >= cutoff
    ]


class PublicWebLiveSearchProvider:
    """Real-provider adapter over the existing public web search (slice 2).

    Bound to one pre-built search prompt (the ``_web_context`` output from
    ``run_chat``) and to the caller's explicit public URLs. ``search(query)``
    sends the bound prompt to ``public_web_research`` and uses ``query`` —
    the normalized user question — only for the relevance gate, so follow-up
    context built into the prompt is preserved while off-topic sources are
    dropped before formatting.

    Relevance gate: the same ``public_sources_relevant`` check the native
    grounded path applies in ``run_chat`` (title/URL evidence, same inputs).
    It is skipped for cricket questions, exactly as the native path never
    applies it there — cricket results carry their own relevance proof in
    the explicit match-state extraction, and short Roman Urdu queries
    ("pak a ka next match") share no literal tokens with scorecard titles.

    A lookup failure from ``public_web_research`` propagates to
    ``answer_live_question``, which converts it into an insufficient answer;
    it never raises to the caller and never guesses. ``model``/``provider``
    record the underlying result for the chat response metadata.
    """

    def __init__(
        self,
        llm,
        settings,
        *,
        prompt: str,
        urls: Sequence[str] | None = None,
        freshness_days: int | None = None,
        snippet_chars: int = _DEFAULT_SNIPPET_CHARS,
    ):
        self._llm = llm
        self._settings = settings
        self._prompt = prompt
        self._urls = list(urls or [])
        if freshness_days is None:
            freshness_days = getattr(settings, "rag_live_freshness_days", 14)
        self._freshness_days = freshness_days
        self._snippet_chars = snippet_chars
        self.model: str | None = None
        self.provider: str | None = None

    async def search(self, query: str) -> Sequence[LiveSource]:
        result = await public_web_research(
            self._llm, self._settings, self._prompt, urls=self._urls
        )
        self.model = result.get("model")
        self.provider = result.get("provider")
        raw_sources = result.get("sources") or []
        snippets = _snippets_by_source(result.get("answer") or "")
        sources: list[LiveSource] = []
        for index, raw in enumerate(raw_sources, 1):
            snippet = " ".join(" ".join(snippets.get(index, [])).split())[
                : self._snippet_chars
            ]
            if not snippet:
                continue
            sources.append(
                LiveSource(
                    title=str(raw.get("title") or "Live source"),
                    url=str(raw.get("url") or ""),
                    snippet=snippet,
                    published_at=str(raw.get("published_at") or "") or None,
                    retrieved_at=str(raw.get("retrieved_at") or ""),
                    provider=str(self.provider or "public web search"),
                )
            )
        if sources and not is_current_cricket_request(self._prompt):
            relevance_sources = [
                {"title": source.title, "url": source.url} for source in sources
            ]
            if not public_sources_relevant(query, relevance_sources):
                return []
        return apply_freshness_window(sources, self._freshness_days)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


async def answer_live_question(
    provider: LiveSearchProvider,
    question: str,
    *,
    now: datetime | None = None,
) -> LiveAnswer:
    """Answer a live-data question from a provider, with citations.

    Raises ``ValueError`` for an empty question. A provider failure becomes
    an insufficient answer; it never raises to the caller and never guesses.
    """
    if not question or not str(question).strip():
        raise ValueError("A live-data question is required.")
    query = normalize_live_query(question)
    retrieved_at = (now or datetime.now(UTC)).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    try:
        sources = await provider.search(query)
    except Exception as exc:  # noqa: BLE001 - a lookup failure must not escape
        logger.info("Live lookup failed (%s); returning insufficient answer", type(exc).__name__)
        return LiveAnswer(
            text=FAILED_LOOKUP_TEXT,
            sources=(),
            insufficient=True,
            retrieved_at=retrieved_at,
            query=query,
        )
    formatted = format_live_answer(query, sources, retrieved_at=retrieved_at)
    return replace(
        formatted,
        followup_actions=live_followup_actions(question, formatted),
    )
