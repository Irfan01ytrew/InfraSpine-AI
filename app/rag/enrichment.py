"""Step AB slice 2 — Knowledge enrichment: ingestion hook + backfill.

Runs the pure-function core in ``app/rag/auto_enrich.py`` over a document's
parsed text and stores the result as metadata on the ``Document`` row
(tags, summary, expiry dates). Derived metadata only: it describes the
document, never changes the stored file or the indexed chunks, and can never
grant permissions, change payees, or change what an approval covers.

Enrichment is best-effort: if it fails for a document, ingestion still
completes and the document still becomes ready — the metadata is simply
missing until the backfill runs again.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import DOC_READY, Document, DocumentChunk
from app.rag.auto_enrich import extract_expiry_dates, extract_tags, summarize_text

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ENRICH_TEXT_CHARS",
    "apply_enrichment",
    "backfill_enrichment",
    "enrichment_metadata",
]

# Enrichment is keyword/sentence work over the text; capping keeps a very
# large document cheap. Tags/summary/dates found in the head of the text are
# the ones that matter (introductions state the point; contracts put the
# expiry clause up front).
MAX_ENRICH_TEXT_CHARS = 200_000


def enrichment_metadata(text: str) -> dict:
    """Derive metadata from parsed document text (pure function).

    Returns ``tags`` (list), ``summary`` (str, may be empty), ``expiry_date``
    (ISO date string of the earliest extracted expiry, or None) and
    ``expiry_dates`` (list of ``{date, label, context}`` dicts).
    """
    dates = extract_expiry_dates(text or "")
    return {
        "tags": extract_tags(text or ""),
        "summary": summarize_text(text or ""),
        "expiry_date": dates[0].date.isoformat() if dates else None,
        "expiry_dates": [item.as_dict() for item in dates],
    }


def apply_enrichment(doc: Document, text: str, *, now: datetime | None = None) -> dict:
    """Write derived metadata onto a ``Document`` row (in-memory only).

    The caller commits. Returns the metadata dict that was applied.
    """
    meta = enrichment_metadata(text)
    doc.tags = meta["tags"]
    doc.summary = meta["summary"] or None
    doc.expiry_date = date.fromisoformat(meta["expiry_date"]) if meta["expiry_date"] else None
    doc.expiry_dates = meta["expiry_dates"] or None
    doc.enriched_at = now or datetime.now(UTC)
    return meta


async def backfill_enrichment(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    limit: int = 100,
    only_missing: bool = True,
) -> dict:
    """Enrich already-ready documents from their stored chunk text.

    No re-parsing and no file access: chunk text already lives in the
    database. By default only documents with no metadata yet are touched, so
    re-running is safe. Bounded by ``limit``; returns counts.
    """
    examined = enriched = skipped = errors = 0
    async with sessionmaker() as db:
        query = select(Document.id).where(Document.status == DOC_READY)
        if only_missing:
            query = query.where(Document.enriched_at.is_(None))
        query = query.order_by(Document.created_at).limit(limit)
        doc_ids = list((await db.execute(query)).scalars())
        for doc_id in doc_ids:
            examined += 1
            try:
                doc = await db.get(Document, doc_id)
                if doc is None:
                    skipped += 1
                    continue
                rows = await db.execute(
                    select(DocumentChunk.content)
                    .where(DocumentChunk.document_id == doc_id)
                    .order_by(DocumentChunk.chunk_index)
                )
                text = "\n".join(content for (content,) in rows if content)
                apply_enrichment(doc, text[:MAX_ENRICH_TEXT_CHARS])
                enriched += 1
            except Exception:
                errors += 1
                logger.warning("Backfill enrichment failed for document %s", doc_id, exc_info=True)
        await db.commit()
    return {"examined": examined, "enriched": enriched, "skipped": skipped, "errors": errors}
