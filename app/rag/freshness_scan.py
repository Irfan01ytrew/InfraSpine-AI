"""Nightly contradiction scan over newly indexed document passages (Step AI, slice 3).

`scan_new_chunks` takes every chunk indexed since `since` (default: the last 24
hours) from ready documents, prefilters other ready passages with the same
lexical prefilter the knowledge service uses (`conflict_search_query`), and
triages each pair with the conservative `potential_conflict` detector. A pair
that trips the detector is stored as a `DocumentConflictCandidate` for human
review — chunk ids only, never passage text, so detection alone exposes no
document content; a reviewer re-checks document ACL before reading either
passage (slice 5 queue). The scan never resolves, deletes, rewrites, or hides
anything: similarity never decides truth.

Run nightly, e.g. `python -m app.rag.freshness_scan`. Re-runs are safe: pairs are
stored in canonical (smaller, larger) chunk order under a unique constraint,
so an already-stored pair is skipped. `--since` backfills an older window.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentChunk, DocumentConflictCandidate
from app.rag.conflict_detection import (
    conflict_search_query,
    potential_conflict,
)

logger = logging.getLogger(__name__)

_SCAN_WINDOW_HOURS = 24
_MAX_NEW_CHUNKS_PER_RUN = 200
_MAX_CANDIDATES_PER_CHUNK = 20


@dataclass(frozen=True)
class ScanResult:
    chunks_scanned: int
    candidates_created: int
    # Distinct candidate pairs triaged this run (a pair seen from both of its
    # chunks counts once); `candidates_created` is the subset newly stored.
    candidates_seen: int
    started_at: datetime
    finished_at: datetime


def ordered_pair(left_id: int, right_id: int) -> tuple[int, int]:
    """Canonical (smaller, larger) chunk order, so one pair is stored once."""
    return (left_id, right_id) if left_id < right_id else (right_id, left_id)


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


def _semantic_conflict(embedder, left_text: str, right_text: str) -> bool:
    """Optional embedding fallback, mirroring the knowledge service: a semantically
    close pair is a candidate only when its polarity is opposed or its normalized
    measurements differ. Local embeddings are best-effort; when they are
    unavailable the lexical triage stands on its own."""
    if embedder is None or not getattr(embedder, "available_locally", False):
        return False
    try:
        vectors = embedder.embed_local([left_text, right_text])
        similarity = _cosine(
            [float(v) for v in vectors[0]], [float(v) for v in vectors[1]]
        )
    except (RuntimeError, OSError):
        logger.warning(
            "Optional local conflict embeddings unavailable; retaining lexical candidates only"
        )
        return False
    return potential_conflict(left_text, right_text, semantic_similarity=similarity)


async def scan_new_chunks(
    db: AsyncSession,
    *,
    since: datetime | None = None,
    limit: int = _MAX_NEW_CHUNKS_PER_RUN,
    embedder=None,
) -> ScanResult:
    """Scan chunks indexed since `since` for contradiction candidates.

    Only chunks of `ready` documents are scanned (their content is what answers
    are built from); detection never changes any document, chunk, or candidate
    status. Commits once at the end.
    """
    started = datetime.now(UTC)
    since = since or (started - timedelta(hours=_SCAN_WINDOW_HOURS))
    new_chunks = list(
        (
            await db.execute(
                select(DocumentChunk)
                .join(Document, Document.id == DocumentChunk.document_id)
                .where(Document.status == "ready", DocumentChunk.created_at > since)
                .order_by(DocumentChunk.created_at, DocumentChunk.id)
                .limit(limit)
            )
        ).scalars()
    )
    created = 0
    seen = 0
    seen_pairs: set[tuple[int, int]] = set()
    for chunk in new_chunks:
        query = conflict_search_query(chunk.content)
        if not query:
            continue
        others = list(
            (
                await db.execute(
                    select(DocumentChunk)
                    .join(Document, Document.id == DocumentChunk.document_id)
                    .where(
                        Document.status == "ready",
                        DocumentChunk.id != chunk.id,
                        func.to_tsvector("simple", DocumentChunk.content).op("@@")(
                            func.to_tsquery("simple", query)
                        ),
                    )
                    .order_by(DocumentChunk.created_at.desc(), DocumentChunk.id)
                    .limit(_MAX_CANDIDATES_PER_CHUNK)
                )
            ).scalars()
        )
        for other in others:
            if other.content == chunk.content:
                continue
            if not potential_conflict(chunk.content, other.content) and not _semantic_conflict(
                embedder, chunk.content, other.content
            ):
                continue
            pair = ordered_pair(chunk.id, other.id)
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                seen += 1
            left_id, right_id = pair
            result = await db.execute(
                pg_insert(DocumentConflictCandidate)
                .values(left_chunk_id=left_id, right_chunk_id=right_id)
                .on_conflict_do_nothing(constraint="uq_document_conflict_candidate_pair")
            )
            if result.rowcount:
                created += 1
    await db.commit()
    return ScanResult(
        chunks_scanned=len(new_chunks),
        candidates_created=created,
        candidates_seen=seen,
        started_at=started,
        finished_at=datetime.now(UTC),
    )


async def _run_cli(args: argparse.Namespace) -> int:
    from app.core.config import get_settings
    from app.db.session import create_engine, create_sessionmaker

    settings = get_settings()
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        sessionmaker = create_sessionmaker(engine)
        since = datetime.fromisoformat(args.since) if args.since else None
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        async with sessionmaker() as db:
            result = await scan_new_chunks(db, since=since, limit=args.limit)
    finally:
        await engine.dispose()
    print(
        f"contradiction scan: scanned={result.chunks_scanned} "
        f"seen={result.candidates_seen} created={result.candidates_created}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.rag.freshness_scan",
        description="Nightly contradiction scan over newly indexed document passages.",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="ISO timestamp; scan chunks indexed after it (default: last 24 hours).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_MAX_NEW_CHUNKS_PER_RUN,
        help="Maximum new chunks to scan in one run.",
    )
    return asyncio.run(_run_cli(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
