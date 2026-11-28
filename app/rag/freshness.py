"""Knowledge freshness scoring (Step AI, slice 1).

Pure functions over document metadata: a freshness score + status + plain-language
reasons, a contradiction candidate wrapper, and a one-line citation note. Stdlib
only, no DB, no network, no model call — so this runs anywhere, including on every
document in a review queue.

Flagging is advisory only. Nothing here deletes, rewrites, or hides a document;
removing or replacing a document stays a human action through the existing
document APIs. Document text is untrusted: review dates and expiry come from
metadata / owner actions, never from document content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from app.rag.conflict_detection import potential_conflict

FRESH = "fresh"
AGING = "aging"
STALE = "stale"
EXPIRED = "expired"
SUPERSEDED = "superseded"

STATUSES = (FRESH, AGING, STALE, EXPIRED, SUPERSEDED)

# Scores for the age/review bands. Penalties (e.g. unindexed content) subtract
# from the band score and never go below 0.
_BAND_SCORES = {FRESH: 100, AGING: 60, STALE: 20}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class FreshnessPolicy:
    """Thresholds for freshness bands. Days are whole days, Asia/Karachi dates."""

    review_after_days: int = 180
    stale_after_days: int = 365
    unindexed_penalty: int = 25

    def __post_init__(self) -> None:
        if self.review_after_days < 1:
            raise ValueError("review_after_days must be at least 1")
        if self.stale_after_days <= self.review_after_days:
            raise ValueError("stale_after_days must be greater than review_after_days")
        if not 0 <= self.unindexed_penalty <= 100:
            raise ValueError("unindexed_penalty must be between 0 and 100")


@dataclass(frozen=True)
class DocumentMeta:
    """The metadata the freshness scorer needs. `review_date` / `expires_at` are
    owner-set metadata (slice 2 adds the columns); `superseded` means a newer
    version of the same document exists."""

    document_id: UUID
    filename: str
    created_at: datetime
    updated_at: datetime
    indexed_at: datetime | None
    version: int
    indexed_version: int | None
    status: str  # pending | processing | ready | failed
    review_date: datetime | None = None
    expires_at: datetime | None = None
    superseded: bool = False


@dataclass(frozen=True)
class FreshnessResult:
    score: int  # 0-100
    status: str  # one of STATUSES
    reasons: tuple[str, ...] = ()


def score_freshness(
    meta: DocumentMeta,
    *,
    as_of: datetime,
    policy: FreshnessPolicy | None = None,
) -> FreshnessResult:
    """Score one document's freshness.

    Decision order (first match wins for the status; penalties only subtract):
    superseded -> expired -> age/review band -> unindexed-content penalty.
    A document that is not indexed yet scores 0 with an explicit reason.
    """
    policy = policy or FreshnessPolicy()
    now = _as_utc(as_of)
    reasons: list[str] = []

    if meta.superseded:
        return FreshnessResult(
            score=0,
            status=SUPERSEDED,
            reasons=(f"A newer version of {meta.filename} exists; this version is superseded.",),
        )

    if meta.expires_at is not None and now > _as_utc(meta.expires_at):
        return FreshnessResult(
            score=0,
            status=EXPIRED,
            reasons=(f"{meta.filename} expired on {_as_utc(meta.expires_at).date().isoformat()}.",),
        )

    # Review date takes precedence over raw age: a document reviewed recently
    # is fresh even if it is old; an overdue review ages it.
    if meta.review_date is not None:
        review = _as_utc(meta.review_date)
        if now <= review:
            status = FRESH
        else:
            overdue_days = (now - review).days
            grace = policy.stale_after_days - policy.review_after_days
            status = STALE if overdue_days > grace else AGING
            reasons.append(
                f"Review was due on {review.date().isoformat()} "
                f"({overdue_days} day(s) overdue)."
            )
    else:
        age_days = max(0, (now - _as_utc(meta.created_at)).days)
        if age_days > policy.stale_after_days:
            status = STALE
            reasons.append(
                f"Document is {age_days} days old and has never been reviewed "
                f"(stale after {policy.stale_after_days} days)."
            )
        elif age_days > policy.review_after_days:
            status = AGING
            reasons.append(
                f"Document is {age_days} days old and has never been reviewed "
                f"(review due after {policy.review_after_days} days)."
            )
        else:
            status = FRESH

    score = _BAND_SCORES[status]

    if meta.status == "failed":
        return FreshnessResult(
            score=0,
            status=status,
            reasons=tuple(reasons) + ("Indexing failed — this document is not searchable.",),
        )
    if meta.status in ("pending", "processing"):
        return FreshnessResult(
            score=0,
            status=status,
            reasons=tuple(reasons) + (f"Indexing not finished yet (status: {meta.status}).",),
        )

    if meta.indexed_version is not None and meta.indexed_version != meta.version:
        score = max(0, score - policy.unindexed_penalty)
        reasons.append(
            f"Searchable content is from version {meta.indexed_version}, "
            f"but the document is now version {meta.version}."
        )

    return FreshnessResult(score=score, status=status, reasons=tuple(reasons))


@dataclass(frozen=True)
class CitedPassage:
    document_id: UUID
    filename: str
    text: str


@dataclass(frozen=True)
class ContradictionFlag:
    left: CitedPassage
    right: CitedPassage
    needs_human_review: bool = True
    reason: str = (
        "These two passages may contradict each other. "
        "A human must compare the cited sources; similarity never decides truth."
    )


def detect_contradiction(
    left: CitedPassage,
    right: CitedPassage,
    *,
    semantic_similarity: float | None = None,
) -> ContradictionFlag | None:
    """Return a human-review candidate when two cited passages may contradict.

    Wraps the conservative `potential_conflict` triage: it only ever produces a
    candidate, never a verdict. `None` means "no candidate" — not "no contradiction".
    """
    if left.document_id == right.document_id and left.text == right.text:
        return None
    if not potential_conflict(left.text, right.text, semantic_similarity=semantic_similarity):
        return None
    return ContradictionFlag(left=left, right=right)


def citation_freshness_note(meta: DocumentMeta, result: FreshnessResult) -> str:
    """One display line pairing a citation with its source's freshness, e.g.
    "Source: Leave Policy.pdf v3, indexed 2026-03-02, freshness: aging
    (review was due on 2026-09-01)"."""
    indexed = (
        _as_utc(meta.indexed_at).date().isoformat() if meta.indexed_at is not None else "not yet indexed"
    )
    note = (
        f"Source: {meta.filename} v{meta.version}, indexed {indexed}, "
        f"freshness: {result.status}"
    )
    if result.reasons:
        note += f" ({'; '.join(result.reasons)})"
    return note


@dataclass(frozen=True)
class CitationFreshness:
    """Freshness of one citation in an answer: the [S<n>] source number plus
    the scored status of the document it cites. Advisory only — it labels the
    source, never hides it."""

    source_number: int
    document_id: UUID
    filename: str
    status: str  # one of STATUSES
    score: int  # 0-100
    note: str  # citation_freshness_note line for this source


@dataclass(frozen=True)
class AnswerFreshness:
    """Per-citation freshness for one answer plus a visible warning, if any.

    The warning is set when the answer leans on dead or stale sources: any
    cited source that is expired or superseded, or every cited source stale
    (or worse). An answer that still answers from its sources — never a guess,
    never a silent downgrade — and the warning tells the reader to verify
    against the current version before acting on it."""

    citations: tuple[CitationFreshness, ...] = ()
    warning: str | None = None


# Statuses that mean the cited source must not be trusted as current.
_DEAD_STATUSES = (EXPIRED, SUPERSEDED)


def summarize_answer_freshness(
    cited: list[tuple[int, object]],
    metas: dict[UUID, DocumentMeta],
    *,
    as_of: datetime | None = None,
    policy: FreshnessPolicy | None = None,
) -> AnswerFreshness:
    """Attach freshness to the citations of one answer.

    `cited` is the validated citation list — (source number, chunk) pairs where
    each chunk carries `document_id` and `filename`. `metas` maps document ids
    to the scorer input (built from the `Document` rows by the caller). A
    citation whose document has no metadata gets no freshness entry and never
    fails the answer: freshness is display-only.
    """
    policy = policy or FreshnessPolicy()
    now = _as_utc(as_of or datetime.now(timezone.utc))
    entries: list[CitationFreshness] = []
    for number, chunk in cited:
        meta = metas.get(getattr(chunk, "document_id"))
        if meta is None:
            continue
        result = score_freshness(meta, as_of=now, policy=policy)
        entries.append(
            CitationFreshness(
                source_number=number,
                document_id=meta.document_id,
                filename=meta.filename,
                status=result.status,
                score=result.score,
                note=citation_freshness_note(meta, result),
            )
        )
    if not entries:
        return AnswerFreshness()
    dead = [e for e in entries if e.status in _DEAD_STATUSES]
    stale = [e for e in entries if e.status == STALE]
    if dead:
        names = ", ".join(f"{e.filename} [S{e.source_number}]" for e in dead)
        warning = (
            "This answer cites a source that is no longer current: "
            f"{names}. Verify against the current version before acting on it."
        )
    elif len(stale) == len(entries):
        warning = (
            "Every source for this answer is stale. "
            "Verify against the current version before acting on it."
        )
    else:
        warning = None
    return AnswerFreshness(citations=tuple(entries), warning=warning)
