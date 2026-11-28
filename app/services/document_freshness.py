"""Document-level freshness on read (Step AI, slice 2).

Thin adapter over the pure scorer in `app.rag.freshness`: a `Document` row becomes
a `DocumentMeta` (with `superseded` derived from `superseded_by`), and the score is
computed for display. Nothing here writes, deletes, or hides a document — flagging
is advisory only. Review dates and expiry are owner-set metadata, never derived
from document text.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select

from app.db.models import Document, DocumentChunk, DocumentConflictCandidate
from app.rag.freshness import (
    DocumentMeta,
    FreshnessPolicy,
    FreshnessResult,
    citation_freshness_note,
    score_freshness,
)


def document_meta(doc: Document) -> DocumentMeta:
    """Build the scorer input from a `Document` row.

    A document is superseded exactly when an owner has pointed it at the document
    that replaces it; ingestion never sets `superseded_by`.
    """
    return DocumentMeta(
        document_id=doc.id,
        filename=doc.filename,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        indexed_at=doc.indexed_at,
        version=doc.version,
        indexed_version=doc.indexed_version,
        status=doc.status,
        review_date=doc.review_date,
        expires_at=doc.expires_at,
        superseded=doc.superseded_by is not None,
    )


def freshness_for(
    doc: Document,
    *,
    as_of: datetime | None = None,
    policy: FreshnessPolicy | None = None,
) -> FreshnessResult:
    """Score one document's freshness for display. `as_of` defaults to now (UTC);
    the scorer does whole-day arithmetic, so the org-timezone day boundary only
    matters within a few hours of midnight Asia/Karachi."""
    return score_freshness(document_meta(doc), as_of=as_of or datetime.now(UTC), policy=policy)


def freshness_note(
    doc: Document,
    *,
    as_of: datetime | None = None,
    policy: FreshnessPolicy | None = None,
) -> str:
    """One display line pairing a document with its freshness, e.g.
    "Source: Leave Policy.pdf v3, indexed 2026-03-02, freshness: aging
    (review was due on 2026-09-01)"."""
    meta = document_meta(doc)
    result = score_freshness(meta, as_of=as_of or datetime.now(UTC), policy=policy)
    return citation_freshness_note(meta, result)


# ------------------------------------------------------------- review queue (slice 5)

#: A document joins the review queue when it is stale, expired, or
#: superseded, or when it has at least one open contradiction candidate.
#: Severity is only a sort order for humans — it changes nothing about
#: whether the document is cited or searchable.
_SEVERITY = {"expired": 0, "superseded": 1, "stale": 2}


@dataclass(frozen=True)
class ReviewQueueEntry:
    """One document needing a human freshness review."""

    document: Document
    freshness: FreshnessResult
    open_conflicts: int
    severity: int  # 0 = most urgent


async def open_conflict_counts(db, doc_ids: list[UUID]) -> dict[UUID, int]:
    """Open contradiction-candidate counts per document.

    A candidate counts for every document it touches (one for each side of
    the pair), because both sides need a human to look. A pair whose two
    chunks sit in the same document counts once for that document.
    """
    if not doc_ids:
        return {}
    rows = (
        await db.execute(
            select(
                DocumentChunk.document_id,
                func.count(func.distinct(DocumentConflictCandidate.id)),
            )
            .join(
                DocumentConflictCandidate,
                or_(
                    DocumentConflictCandidate.left_chunk_id == DocumentChunk.id,
                    DocumentConflictCandidate.right_chunk_id == DocumentChunk.id,
                ),
            )
            .where(
                DocumentConflictCandidate.status == "open",
                DocumentChunk.document_id.in_(doc_ids),
            )
            .group_by(DocumentChunk.document_id)
        )
    ).all()
    return {row[0]: int(row[1]) for row in rows}


async def review_queue_entries(
    db,
    documents: list[Document],
    *,
    as_of: datetime | None = None,
    policy: FreshnessPolicy | None = None,
) -> list[ReviewQueueEntry]:
    """Build the review queue from documents the caller may already read.

    The caller is responsible for the ACL filter — only readable documents
    may be passed in. Entries are ordered most urgent first (expired,
    superseded, stale, then fresh/aging documents with open conflicts),
    then by the earliest review date.
    """
    as_of = as_of or datetime.now(UTC)
    counts = await open_conflict_counts(db, [doc.id for doc in documents])
    entries: list[ReviewQueueEntry] = []
    for doc in documents:
        result = score_freshness(document_meta(doc), as_of=as_of, policy=policy)
        conflicts = counts.get(doc.id, 0)
        if result.status not in _SEVERITY and conflicts == 0:
            continue
        entries.append(
            ReviewQueueEntry(
                document=doc,
                freshness=result,
                open_conflicts=conflicts,
                severity=_SEVERITY.get(result.status, 3),
            )
        )
    entries.sort(key=lambda e: (e.severity, e.document.review_date or e.document.created_at))
    return entries
