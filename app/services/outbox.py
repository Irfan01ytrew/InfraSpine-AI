"""PostgreSQL transactional outbox for the derived knowledge index (ADR-004 WP4).

Events are written in the same transaction as the knowledge or document change (database triggers).
This consumer claims them with SKIP LOCKED, retries with backoff, and is safe to run twice:

* `knowledge.index` never writes FAISS. Drafts are skipped; approved statements use PostgreSQL's
  ACL-filtered full-text index rather than this vector outbox.
* `knowledge.unindex` removes any derived vectors for that revision (a no-op when there are none).
* `knowledge.reconcile` rebuilds the optional vector store from published revision vectors. This
  workflow does not create revision vectors, so reconciliation clears that store.

PostgreSQL remains the authority. The document FAISS index is untouched.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import (
    KNOWLEDGE_PUBLISHED,
    OUTBOX_COMPLETED,
    OUTBOX_FAILED,
    OUTBOX_INDEX,
    OUTBOX_PENDING,
    OUTBOX_PROCESSING,
    OUTBOX_RECONCILE,
    OUTBOX_UNINDEX,
    IndexOutbox,
    KnowledgeIndexVector,
    KnowledgeRevision,
)
from app.rag.embeddings import from_bytes
from app.rag.vector_store import VectorStore

logger = logging.getLogger(__name__)

_LEASE = timedelta(seconds=60)


class OutboxWorker:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, store: VectorStore) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self.store = store
        self._dimension = store.dimension
        self._lock = asyncio.Lock()
        self.fail_next = 0  # tests inject failures; production leaves this at 0

    async def run(self, stop: asyncio.Event) -> None:
        try:
            await self.reconcile()
        except Exception as exc:
            logger.error("Knowledge index reconcile failed: %s", type(exc).__name__)
        while not stop.is_set():
            worked = False
            try:
                worked = await self.process_next()
            except Exception as exc:
                logger.error("Knowledge outbox iteration failed: %s", type(exc).__name__)
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._settings.rag_worker_poll_seconds)
                except TimeoutError:
                    pass

    async def process_next(self) -> bool:
        """Claim and finish one due event. Returns False when nothing is due."""
        event_id = await self._claim()
        if event_id is None:
            return False
        try:
            result = await self._apply(event_id)
        except Exception as exc:
            logger.warning("Outbox event %s failed: %s", event_id, type(exc).__name__)
            await self._fail(event_id, type(exc).__name__)
        else:
            await self._complete(event_id, result)
        return True

    async def recover_failed(self) -> int:
        """Put failed events back on the queue. Completed events stay completed."""
        async with self._sessionmaker() as db:
            result = await db.execute(
                update(IndexOutbox)
                .where(IndexOutbox.status == OUTBOX_FAILED)
                .values(status=OUTBOX_PENDING, available_at=datetime.now(UTC), locked_until=None)
            )
            await db.commit()
            return result.rowcount or 0

    async def reconcile(self) -> dict:
        """Make the knowledge FAISS index match published revisions. Drafts contribute nothing."""
        async with self._lock:
            async with self._sessionmaker() as db:
                rows = (
                    await db.execute(
                        select(KnowledgeIndexVector.id, KnowledgeIndexVector.embedding)
                        .join(KnowledgeRevision, KnowledgeRevision.id == KnowledgeIndexVector.revision_id)
                        .where(KnowledgeRevision.status == KNOWLEDGE_PUBLISHED)
                        .order_by(KnowledgeIndexVector.id)
                    )
                ).all()
            before = await asyncio.to_thread(self.store.count)
            if not rows:
                empty = np.zeros((0, self._dimension), dtype=np.float32)
                await asyncio.to_thread(self.store.rebuild, [], empty)
                return {"vectors": 0, "removed": before}
            ids = [row.id for row in rows]
            vectors = np.stack([from_bytes(row.embedding, self._dimension) for row in rows]).astype(np.float32)
            await asyncio.to_thread(self.store.rebuild, ids, vectors)
            return {"vectors": len(ids), "removed": max(0, before - len(ids))}

    async def _claim(self) -> int | None:
        now = datetime.now(UTC)
        async with self._sessionmaker() as db:
            event = (
                await db.execute(
                    select(IndexOutbox)
                    .where(
                        or_(
                            (IndexOutbox.status == OUTBOX_PENDING) & (IndexOutbox.available_at <= now),
                            (IndexOutbox.status == OUTBOX_PROCESSING) & (IndexOutbox.locked_until < now),
                        )
                    )
                    .order_by(IndexOutbox.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if event is None:
                return None
            if event.status == OUTBOX_PROCESSING and event.attempts >= self._settings.rag_max_attempts:
                event.status = OUTBOX_FAILED
                event.locked_until = None
                event.last_error = "lease_expired"
                await db.commit()
                return None
            event.status = OUTBOX_PROCESSING
            event.attempts += 1
            event.locked_until = now + _LEASE
            event_id = event.id
            await db.commit()
            return event_id

    async def _apply(self, event_id: int) -> str:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("injected outbox failure")
        async with self._sessionmaker() as db:
            event = await db.get(IndexOutbox, event_id)
            if event is None:
                return "missing"
            if event.event_type == OUTBOX_INDEX:
                revision = await db.get(KnowledgeRevision, event.aggregate_id)
                # PostgreSQL full text is the active knowledge search index; revisions do not create FAISS vectors.
                if revision is None or revision.status != KNOWLEDGE_PUBLISHED:
                    return "skipped_unpublished"
                return "skipped_no_vector_index"
            if event.event_type == OUTBOX_UNINDEX:
                await self._remove_revision(db, event.aggregate_id)
                await db.commit()
                return "unindexed"
            if event.event_type == OUTBOX_RECONCILE:
                await self.reconcile()
                return "reconciled"
            raise RuntimeError("unknown outbox event")

    async def _remove_revision(self, db: AsyncSession, revision_id: uuid.UUID) -> None:
        rows = list(
            (
                await db.execute(select(KnowledgeIndexVector).where(KnowledgeIndexVector.revision_id == revision_id))
            ).scalars()
        )
        if not rows:
            return
        ids = [row.id for row in rows]
        for row in rows:
            await db.delete(row)
        await db.flush()
        await self.store_remove(ids)

    async def store_remove(self, ids: list[int]) -> None:
        async with self._lock:
            await asyncio.to_thread(self.store.apply, remove_ids=ids)

    async def _complete(self, event_id: int, result: str) -> None:
        async with self._sessionmaker() as db:
            event = (
                await db.execute(select(IndexOutbox).where(IndexOutbox.id == event_id).with_for_update())
            ).scalar_one()
            event.status = OUTBOX_COMPLETED
            event.result = result
            event.completed_at = datetime.now(UTC)
            event.locked_until = None
            event.last_error = None
            await db.commit()
        logger.info("Outbox event %s completed result=%s", event_id, result)

    async def _fail(self, event_id: int, code: str) -> None:
        delay = self._settings.rag_retry_base_seconds
        async with self._sessionmaker() as db:
            event = (
                await db.execute(select(IndexOutbox).where(IndexOutbox.id == event_id).with_for_update())
            ).scalar_one()
            event.last_error = code[:255]
            event.locked_until = None
            if event.attempts >= self._settings.rag_max_attempts:
                event.status = OUTBOX_FAILED
            else:
                event.status = OUTBOX_PENDING
                event.available_at = datetime.now(UTC) + timedelta(seconds=delay * (2 ** (event.attempts - 1)))
            await db.commit()
