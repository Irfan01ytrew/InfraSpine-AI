"""Background ingestion and vector-index synchronisation.

Jobs live in PostgreSQL (the `documents` row is the job): workers claim them with
`FOR UPDATE SKIP LOCKED` and a lease, so a crashed worker's job is picked up again.
Text extraction and embedding happen locally; nothing is sent to an external LLM.

Processes (see `RAG_INGESTION_MODE`):
* "worker" (default): `python -m app.worker` claims and processes jobs and writes PostgreSQL only.
  The API process owns the vector index and applies new chunks from PostgreSQL (`IndexSynchronizer.run_sync`).
* "embedded": the API process runs the same worker loop and updates its index directly.
Any number of worker processes may run against one database; each claims different jobs.

Per job, the file is parsed and chunked in a separate, killable child process with time and memory
limits (`app.rag.isolation`); the lease is renewed while it runs, and processing stops as soon as the
job is cancelled, replaced or deleted.

Ordering rule for every index change: commit PostgreSQL first, then update the
vector store under `IndexSynchronizer.lock`. If the second step fails or the process
dies in between, retrieval stays correct (it re-checks PostgreSQL) and the next
reconcile repairs the index.
"""

import asyncio
import contextlib
import logging
import os
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import (
    DOC_FAILED,
    DOC_PENDING,
    DOC_PROCESSING,
    DOC_READY,
    Document,
    DocumentChunk,
    DocumentTable,
    IngestionWorkerHeartbeat,
)
from app.rag.chunking import TextChunk
from app.rag.discovery import DESCRIPTOR_VERSION, build_descriptor, refresh_descriptors
from app.rag.embeddings import Embedder, EmbeddingModelUnavailableError, from_bytes, to_bytes
from app.rag.file_limits import max_ingested_bytes
from app.rag.isolation import ProcessingCancelled, ProcessingCrashed, parse_and_chunk, run_isolated
from app.rag.parsers.base import ParseError
from app.rag.parsers.registry import limits_from_settings
from app.rag.storage import DocumentStorage
from app.rag.vector_store import VectorStore

logger = logging.getLogger(__name__)

_RECONCILE_BATCH = 1000


def _processing_failure(exc: Exception) -> tuple[str, str, bool]:
    """Map known failures to safe, actionable document status; never expose raw exception details."""
    if isinstance(exc, EmbeddingModelUnavailableError):
        return (
            "embedding_model_unavailable",
            "The local search model is unavailable on this installation. Contact an administrator to stage or verify it, then reprocess this document.",
            True,
        )
    return "processing_error", "Processing failed unexpectedly.", False


class StoredFileMissingError(Exception):
    """The document's own file is gone from storage (permanent; distinct from other missing files)."""


class ImageNotDescribed(Exception):
    """An image document (or a PDF with no text, only pictures) got no description, so it has nothing to index.
    `state` is the vision state that applied: describable states (the provider failed) are retried."""

    def __init__(self, state: str) -> None:
        super().__init__(state)
        self.state = state


class IndexSynchronizer:
    """The only component that mutates the vector store (it lives in the API process)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], store: VectorStore, embedder: Embedder) -> None:
        self._sessionmaker = sessionmaker
        self.store = store
        self._embedder = embedder
        self.lock = asyncio.Lock()
        self._seen: tuple[int, int] | None = None  # (chunk count, highest chunk id) at the last sync

    async def apply(self, *, remove_ids: list[int] = (), add_ids: list[int] = (), vectors: np.ndarray | None = None) -> None:
        async with self.lock:
            await asyncio.to_thread(self.store.apply, remove_ids=remove_ids, add_ids=add_ids, vectors=vectors)

    async def remove(self, ids: list[int]) -> None:
        if ids:
            await self.apply(remove_ids=ids)

    async def reconcile(self, *, full: bool = False) -> dict:
        """Make the vector store match PostgreSQL. Incremental unless `full` or the index is invalid."""
        async with self.lock:
            async with self._sessionmaker() as db:
                if full or self.store.needs_rebuild:
                    count = await self._stream_rebuild(db)
                    logger.info("Vector index rebuilt from database (%s vectors)", count)
                    return {"mode": "rebuild", "added": count, "removed": 0, "vectors": count}

                indexed_count = await asyncio.to_thread(self.store.count)
                if indexed_count == 0:
                    database_count = int(
                        (
                            await db.execute(
                                select(func.count(DocumentChunk.id))
                                .join(Document, Document.id == DocumentChunk.document_id)
                                .where(Document.embedding_model == self._embedder.model_name)
                            )
                        ).scalar_one()
                    )
                    if database_count:
                        count = await self._stream_rebuild(db)
                        logger.info("Missing vector index rebuilt from database (%s vectors)", count)
                        return {"mode": "rebuild", "added": count, "removed": 0, "vectors": count}

                added, removed = await self._reconcile_stream(db)
                if removed or added:
                    logger.info("Vector index reconciled (+%s / -%s)", added, removed)
                return {
                    "mode": "incremental",
                    "added": added,
                    "removed": removed,
                    "vectors": self.store.count(),
                }

    async def sync_changes(self) -> bool:
        """Reconcile only when PostgreSQL's chunks changed since the last sync (cheap check otherwise).

        Chunk IDs are never reused, so any insert raises the highest ID and any delete lowers the count:
        (count, max id) changes whenever the set of chunks does."""
        async with self._sessionmaker() as db:
            count, highest = (
                await db.execute(
                    select(func.count(DocumentChunk.id), func.coalesce(func.max(DocumentChunk.id), 0))
                    .join(Document, Document.id == DocumentChunk.document_id)
                    .where(Document.embedding_model == self._embedder.model_name)
                )
            ).one()
        marker = (int(count), int(highest))
        if marker == self._seen and not self.store.needs_rebuild:
            return False
        await self.reconcile()
        self._seen = marker
        return True

    async def run_sync(self, stop: asyncio.Event, interval: float) -> None:
        """Keep the index in step with chunks written by worker processes."""
        while not stop.is_set():
            try:
                await self.sync_changes()
            except Exception as exc:
                logger.error("Vector index sync failed: %s", type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)

    async def _reconcile_stream(self, db: AsyncSession) -> tuple[int, int]:
        """Diff ordered PostgreSQL and vector IDs in bounded batches, without corpus-sized ID sets."""
        result = await db.stream(
            select(DocumentChunk.id, DocumentChunk.embedding)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(Document.embedding_model == self._embedder.model_name)
            .order_by(DocumentChunk.id)
        )
        rows = result.__aiter__()
        row = await anext(rows, None)
        indexed = iter(self.store.iter_ids(_RECONCILE_BATCH))
        vector_id = next(indexed, None)
        add_ids: list[int] = []
        add_vectors: list[np.ndarray] = []
        remove_ids: list[int] = []
        added = removed = 0

        async def next_row():
            return await anext(rows, None)

        async def flush() -> None:
            nonlocal added, removed, add_ids, add_vectors, remove_ids
            if not add_ids and not remove_ids:
                return
            vectors = (
                np.stack(add_vectors).astype(np.float32, copy=False)
                if add_vectors
                else np.zeros((0, self._embedder.dimension), dtype=np.float32)
            )
            await asyncio.to_thread(
                self.store.apply,
                remove_ids=remove_ids,
                add_ids=add_ids,
                vectors=vectors,
            )
            added += len(add_ids)
            removed += len(remove_ids)
            add_ids, add_vectors, remove_ids = [], [], []

        try:
            while row is not None or vector_id is not None:
                if vector_id is None or (row is not None and int(row[0]) < vector_id):
                    chunk_id, embedding = row
                    add_ids.append(int(chunk_id))
                    add_vectors.append(from_bytes(embedding, self._embedder.dimension))
                    row = await next_row()
                elif row is None or vector_id < int(row[0]):
                    remove_ids.append(vector_id)
                    vector_id = next(indexed, None)
                else:
                    row = await next_row()
                    vector_id = next(indexed, None)
                if len(add_ids) + len(remove_ids) >= _RECONCILE_BATCH:
                    await flush()
            await flush()
            return added, removed
        finally:
            await result.close()

    async def _stream_rebuild(self, db: AsyncSession) -> int:
        """Rebuild from PostgreSQL in batches; persistent providers can resume from their saved checkpoint."""
        incompatible = int(
            (
                await db.execute(
                    select(func.count(DocumentChunk.id))
                    .join(Document, Document.id == DocumentChunk.document_id)
                    .where(Document.embedding_model.is_distinct_from(self._embedder.model_name))
                )
            ).scalar_one()
        )
        if incompatible:
            raise RuntimeError(
                "Stored document embeddings use a different model identity. Re-embed those documents before rebuilding."
            )
        try:
            await asyncio.to_thread(self.store.begin_rebuild)
            last_id = int(getattr(self.store, "rebuild_checkpoint", 0))
            while True:
                rows = (
                    await db.execute(
                        select(DocumentChunk.id, DocumentChunk.embedding)
                        .join(Document, Document.id == DocumentChunk.document_id)
                        .where(
                            Document.embedding_model == self._embedder.model_name,
                            DocumentChunk.id > last_id,
                        )
                        .order_by(DocumentChunk.id)
                        .limit(_RECONCILE_BATCH)
                    )
                ).all()
                if not rows:
                    break
                ids = [int(chunk_id) for chunk_id, _ in rows]
                vectors = np.stack(
                    [from_bytes(data, self._embedder.dimension) for _, data in rows]
                ).astype(np.float32, copy=False)
                await asyncio.to_thread(self.store.add_rebuild_batch, ids, vectors)
                last_id = ids[-1]
            await asyncio.to_thread(self.store.finish_rebuild)
            # Catch concurrent inserts/deletes and stale points left by an interrupted resumable build.
            await self._reconcile_stream(db)
            return await asyncio.to_thread(self.store.count)
        except BaseException:
            await asyncio.to_thread(self.store.abort_rebuild)
            raise


@dataclass(frozen=True)
class Job:
    document_id: uuid.UUID
    version: int
    storage_key: str
    file_format: str = "pdf"
    folder_imported: bool = False
    # False after the previous attempt's parser process crashed. Docling can crash natively (e.g. a failed
    # allocation), which kills the process before its own fallback runs; the retry uses the built-in parser.
    use_docling: bool = True


class WorkerHeartbeat:
    """Registers a running worker for status reporting (`ingestion_workers`)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], mode: str, concurrency: int) -> None:
        self._sessionmaker = sessionmaker
        self.id = f"{socket.gethostname()[:80]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._mode = mode
        self._concurrency = concurrency

    async def register(self) -> None:
        async with self._sessionmaker() as db:
            # Rows of workers that stopped without unregistering are only history; keep a day of it.
            await db.execute(
                delete(IngestionWorkerHeartbeat).where(
                    IngestionWorkerHeartbeat.heartbeat_at < datetime.now(UTC) - timedelta(days=1)
                )
            )
            db.add(
                IngestionWorkerHeartbeat(
                    id=self.id, hostname=socket.gethostname()[:255], pid=os.getpid(), mode=self._mode,
                    concurrency=self._concurrency,
                )
            )
            await db.commit()

    async def beat(self, active_jobs: int) -> None:
        async with self._sessionmaker() as db:
            await db.execute(
                update(IngestionWorkerHeartbeat)
                .where(IngestionWorkerHeartbeat.id == self.id)
                .values(heartbeat_at=func.now(), active_jobs=active_jobs)
            )
            await db.commit()

    async def unregister(self) -> None:
        async with self._sessionmaker() as db:
            await db.execute(delete(IngestionWorkerHeartbeat).where(IngestionWorkerHeartbeat.id == self.id))
            await db.commit()


async def workers_online(db: AsyncSession, settings: Settings) -> int:
    """Workers whose heartbeat is recent (three missed beats = stopped)."""
    cutoff = datetime.now(UTC) - timedelta(seconds=3 * settings.rag_worker_heartbeat_seconds)
    return int(
        (await db.execute(select(func.count()).where(IngestionWorkerHeartbeat.heartbeat_at >= cutoff))).scalar_one()
    )


class IngestionWorker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        storage: DocumentStorage,
        embedder: Embedder,
        indexer: IndexSynchronizer | None,
        *,
        isolate: bool | None = None,
    ) -> None:
        """`indexer` is None in a separate worker process: results go to PostgreSQL only and the API
        process applies them to its index."""
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._storage = storage
        self._embedder = embedder
        self._indexer = indexer
        self.isolate = settings.rag_parse_isolation if isolate is None else isolate
        self.target = parse_and_chunk  # what the parser child process runs (tests substitute misbehaving parsers)
        self._active: dict[uuid.UUID, threading.Event] = {}
        self._shutting_down = False
        self._llm_runtime = None  # created on the first picture to describe

    @property
    def active_jobs(self) -> int:
        return len(self._active)

    # ------------------------------------------------------------ lifecycle

    async def run(self, stop: asyncio.Event, *, concurrency: int = 1, mode: str = "embedded") -> None:
        """Process jobs with `concurrency` parallel slots until `stop` is set, then shut down gracefully:
        running jobs get `rag_worker_shutdown_grace_seconds` to finish, the rest go back to the queue."""
        await self.startup()
        heartbeat = WorkerHeartbeat(self._sessionmaker, mode, concurrency)
        try:
            await heartbeat.register()
        except Exception as exc:
            logger.error("Worker heartbeat registration failed: %s", type(exc).__name__)
        beats = asyncio.create_task(self._heartbeats(heartbeat, stop), name="rag-worker-heartbeat")
        slots = [asyncio.create_task(self._slot(stop), name=f"rag-worker-slot-{i}") for i in range(concurrency)]
        logger.info("Document worker started (mode=%s, concurrency=%s, isolation=%s)", mode, concurrency, self.isolate)
        try:
            await stop.wait()
            _, pending = await asyncio.wait(slots, timeout=self._settings.rag_worker_shutdown_grace_seconds)
            if pending:
                self.release_active()
                await asyncio.wait(pending, timeout=30)
        finally:
            for task in (*slots, beats):
                task.cancel()
            await asyncio.gather(*slots, beats, return_exceptions=True)
            with contextlib.suppress(Exception):
                await heartbeat.unregister()
            if self._llm_runtime is not None:
                with contextlib.suppress(Exception):
                    await self._llm_runtime.close()
            logger.info("Document worker stopped")

    def release_active(self) -> None:
        """Stop the jobs in progress and put them back on the queue (graceful shutdown)."""
        self._shutting_down = True
        for cancelled in list(self._active.values()):
            cancelled.set()

    async def _slot(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            worked = False
            try:
                if self._indexer is not None and self._indexer.store.needs_rebuild:
                    await self._indexer.reconcile()
                worked = await self.process_next()
            except Exception as exc:  # keep the worker alive; details are type-only (no content)
                logger.error("RAG worker iteration failed: %s", type(exc).__name__)
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._settings.rag_worker_poll_seconds)

    async def _heartbeats(self, heartbeat: WorkerHeartbeat, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._settings.rag_worker_heartbeat_seconds)
            try:
                await heartbeat.beat(self.active_jobs)
            except Exception as exc:
                logger.warning("Worker heartbeat failed: %s", type(exc).__name__)

    async def startup(self) -> None:
        try:
            await asyncio.to_thread(self._embedder.warmup)
        except Exception as exc:
            logger.error("Embedding model unavailable (%s); documents will be retried", type(exc).__name__)
        await self.requeue_stale_embeddings()
        try:
            await self.refresh_descriptors()
        except Exception as exc:  # discovery degrades to chunk search for these documents; retried next start
            logger.error("Document descriptors were not refreshed (%s)", type(exc).__name__)
        if self._indexer is None:
            return
        try:
            await self._indexer.reconcile()
        except Exception as exc:
            logger.error("Vector index reconcile failed: %s", type(exc).__name__)

    async def refresh_descriptors(self) -> int:
        """Write missing or outdated document descriptors from stored chunks (no re-parsing), in batches."""
        total = 0
        while True:
            async with self._sessionmaker() as db:
                done = await refresh_descriptors(db, date_order=self._settings.rag_date_order)
            total += done
            if not done:
                break
        if total:
            logger.info("Refreshed %s document descriptors", total)
        return total

    async def requeue_stale_embeddings(self) -> int:
        """Re-embed documents indexed with a different embedding model (e.g. after a model change)."""
        async with self._sessionmaker() as db:
            result = await db.execute(
                update(Document)
                .where(
                    Document.status == DOC_READY,
                    Document.indexed_version.is_not(None),
                    Document.embedding_model.is_distinct_from(self._embedder.model_name),
                )
                .values(status=DOC_PENDING, attempts=0, next_attempt_at=datetime.now(UTC), error_code=None, error_message=None)
            )
            await db.commit()
            if result.rowcount:
                logger.info("Queued %s documents for re-embedding", result.rowcount)
            return result.rowcount

    # ------------------------------------------------------------ jobs

    async def process_next(self) -> bool:
        """Process one document if any is due. Returns False when the queue is empty."""
        job = await self._claim()
        if job is None:
            return False
        cancelled = threading.Event()
        self._active[job.document_id] = cancelled
        lease = asyncio.create_task(self._keep_lease(job, cancelled), name="rag-job-lease")
        try:
            try:
                pictures = job.file_format == "pdf" and await self._can_describe_images()
                page_count, chunks, vectors, images, tables = await asyncio.to_thread(self._build, job, cancelled, pictures)
                vision = "none"
                if images:  # described while the lease is still renewed
                    chunks, vectors, vision = await self._with_image_descriptions(
                        job, chunks, vectors, images, cancelled
                    )
                    if cancelled.is_set():
                        raise ProcessingCancelled()
                if not chunks:
                    if job.file_format == "pdf" and vision == "none":
                        raise ParseError("no_text", "No extractable text found.")
                    raise ImageNotDescribed(vision)
            finally:
                lease.cancel()
                await asyncio.gather(lease, return_exceptions=True)
        except ProcessingCancelled:
            if self._shutting_down:
                await self._release(job)
            else:
                logger.info("Document %s processing stopped (cancelled, replaced or deleted)", job.document_id)
        except ParseError as exc:  # malformed, encrypted, oversized, too slow or too large in memory: permanent
            await self._fail(job, exc.code, exc.message, permanent=True)
        except StoredFileMissingError:
            await self._fail(job, "file_missing", "The stored file is missing. Upload it again.", permanent=True)
        except ImageNotDescribed as exc:
            from app.rag.image_descriptions import DESCRIBABLE

            if exc.state in DESCRIBABLE or exc.state == "error":  # the provider failed: try again later
                await self._fail(job, "image_not_described", "The AI provider could not describe the image.", permanent=False)
            else:
                await self._fail(
                    job, "vision_unavailable",
                    "Images are read by the AI provider. Activate a provider that accepts images, authorize image "
                    "sharing for it, then reprocess.",
                    permanent=True,
                )
        except ProcessingCrashed:
            logger.warning("Document %s processor stopped unexpectedly", job.document_id)
            await self._fail(
                job, "processing_crashed",
                "The document processor stopped unexpectedly (for example out of memory).", permanent=False,
            )
        except Exception as exc:
            code, message, permanent = _processing_failure(exc)
            logger.warning("Document %s processing error: %s", job.document_id, code)
            await self._fail(job, code, message, permanent=permanent)
        else:
            await self._complete(job, page_count, chunks, vectors, tables)
        finally:
            self._active.pop(job.document_id, None)
        return True

    async def _claim(self) -> Job | None:
        now = datetime.now(UTC)
        async with self._sessionmaker() as db:
            doc = (
                await db.execute(
                    select(Document)
                    .where(
                        ((Document.status == DOC_PENDING) & (Document.next_attempt_at <= now))
                        | ((Document.status == DOC_PROCESSING) & (Document.locked_until < now))
                    )
                    .order_by(Document.next_attempt_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if doc is None:
                return None
            if doc.status == DOC_PROCESSING and doc.attempts >= self._settings.rag_max_attempts:
                # Workers repeatedly stopped while holding this job (lease ran out): stop retrying.
                self._mark_failed(
                    doc, "processing_abandoned",
                    f"Processing was interrupted {doc.attempts} times (the document worker stopped or was "
                    "restarted). Use Reprocess to try again.",
                )
                await db.commit()
                return None
            doc.status = DOC_PROCESSING
            doc.attempts += 1
            doc.locked_until = now + timedelta(seconds=self._settings.rag_processing_lease_seconds)
            job = Job(
                document_id=doc.id, version=doc.version, storage_key=doc.storage_key, file_format=doc.file_format,
                folder_imported=doc.source_uri is not None,
                use_docling=doc.error_code != "processing_crashed",
            )
            if not job.use_docling:
                logger.info("Document %s: the previous parse crashed; retrying with the built-in parser", doc.id)
            await db.commit()
            return job

    async def _keep_lease(self, job: Job, cancelled: threading.Event) -> None:
        """Renew the lease while the job runs. When the job is no longer ours to finish (cancelled,
        replaced, deleted or reclaimed), signal the processing to stop."""
        lease = self._settings.rag_processing_lease_seconds
        interval = max(0.05, min(self._settings.rag_worker_poll_seconds, lease / 3))
        while not cancelled.is_set():
            await asyncio.sleep(interval)
            try:
                async with self._sessionmaker() as db:
                    result = await db.execute(
                        update(Document)
                        .where(
                            Document.id == job.document_id,
                            Document.version == job.version,
                            Document.status == DOC_PROCESSING,
                        )
                        .values(locked_until=datetime.now(UTC) + timedelta(seconds=lease))
                    )
                    await db.commit()
            except Exception as exc:  # a transient DB problem must not stop the job; the lease is generous
                logger.warning("Lease renewal failed for %s: %s", job.document_id, type(exc).__name__)
                continue
            if result.rowcount == 0:
                cancelled.set()

    def _build(
        self, job: Job, cancelled: threading.Event, pictures: bool = False
    ) -> tuple[int | None, list[TextChunk], np.ndarray | None, list, list]:
        settings = self._settings
        path = self._storage.path_for(job.storage_key)
        if not path.is_file():
            raise StoredFileMissingError()
        maximum_bytes = max_ingested_bytes(settings, job.file_format, imported=job.folder_imported)
        if path.stat().st_size > maximum_bytes:
            limit_mb = maximum_bytes // (1024 * 1024)
            raise ParseError("file_too_large", f"The file exceeds the {limit_mb} MB processing limit.")
        args = (
            str(path), job.file_format, limits_from_settings(settings), settings.rag_chunk_size, settings.rag_chunk_overlap,
            job.use_docling, pictures,
        )
        if self.isolate:
            result = run_isolated(
                self.target, args,
                timeout_seconds=settings.rag_processing_timeout_seconds,
                memory_bytes=settings.rag_parser_memory_mb * 1024 * 1024,
                cancelled=cancelled,
            )
        else:
            result = self.target(*args)
        page_count, chunks, *rest = result  # (page count, chunks[, pictures[, tables]])
        images = rest[0] if rest else []
        tables = rest[1] if len(rest) > 1 else []
        if cancelled.is_set():
            raise ProcessingCancelled()
        if not chunks and not images:
            raise ParseError("no_text", "No extractable text found.")
        # Embedding is trusted, bounded work (text size is capped) and keeps the model loaded once per worker.
        # Any error here (e.g. the embedding model failing to load) is transient and retried.
        vectors = self._embedder.embed([c.text for c in chunks]) if chunks else None
        if cancelled.is_set():
            raise ProcessingCancelled()
        return page_count, chunks, vectors, images, tables

    async def _can_describe_images(self) -> bool:
        """Whether pictures would be described now (authorized, vision-capable provider); PDFs skip extracting
        them otherwise."""
        from app.rag.image_descriptions import DESCRIBABLE, vision_status

        try:
            async with self._sessionmaker() as db:
                return (await vision_status(db, self._settings)).state in DESCRIBABLE
        except Exception as exc:
            logger.warning("Image understanding status unavailable: %s", type(exc).__name__)
            return False

    async def _with_image_descriptions(
        self, job: Job, chunks: list[TextChunk], vectors: np.ndarray | None, images: list, cancelled: threading.Event
    ) -> tuple[list[TextChunk], np.ndarray | None, str]:
        """Append descriptions of the document's pictures (see `app.rag.image_descriptions`) as chunks of their
        page (or of the image, for an uploaded image), with the vision state that applied. The worker, not the
        parser process, calls the provider; a failure leaves the text as it is."""
        from app.rag.chunking import chunk_sections
        from app.rag.image_descriptions import describe_images

        try:
            sections, state = await describe_images(
                self._sessionmaker, self._settings, self._llm_client, images, cancelled
            )
        except Exception as exc:  # never fail a document over its pictures
            logger.warning("Document %s pictures were not described: %s", job.document_id, type(exc).__name__)
            return chunks, vectors, "error"
        logger.info(
            "Document %s: %s pictures, %s described (vision %s)", job.document_id, len(images), len(sections), state
        )
        if not sections:
            return chunks, vectors, state
        extra = chunk_sections(sections, self._settings.rag_chunk_size, self._settings.rag_chunk_overlap)
        extra = [TextChunk(len(chunks) + i, c.page_number, c.text, c.locator) for i, c in enumerate(extra)]
        extra_vectors = await asyncio.to_thread(self._embedder.embed, [c.text for c in extra])
        return chunks + extra, (extra_vectors if vectors is None else np.vstack([vectors, extra_vectors])), state

    async def _llm_client(self, db: AsyncSession):
        """The active provider's client, from a runtime this worker creates on first use."""
        if self._llm_runtime is None:
            from app.core.crypto import rotating_cipher_from_settings
            from app.services.llm_runtime import LLMRuntime

            self._llm_runtime = LLMRuntime(
                self._settings,
                rotating_cipher_from_settings(self._settings),
            )
        return await self._llm_runtime.get_client(db)

    async def _lock_current(self, db: AsyncSession, job: Job) -> Document | None:
        """Lock the document if it still exists and still refers to this job's file version."""
        doc = (
            await db.execute(
                select(Document)
                .where(Document.id == job.document_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if doc is None or doc.version != job.version or doc.status != DOC_PROCESSING:
            logger.info("Document %s changed during processing; result discarded", job.document_id)
            return None
        return doc

    async def _complete(
        self, job: Job, page_count: int | None, chunks: list[TextChunk], vectors: np.ndarray, tables: list | None = None
    ) -> None:
        async with self._sessionmaker() as db:
            doc = await self._lock_current(db, job)
            if doc is None:
                return
            old_ids = list(
                (await db.execute(select(DocumentChunk.id).where(DocumentChunk.document_id == doc.id))).scalars()
            )
            await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
            # A document's tables are replaced with its chunks, so both always describe the same version.
            await db.execute(delete(DocumentTable).where(DocumentTable.document_id == doc.id))
            db.add_all(
                DocumentTable(
                    document_id=doc.id,
                    table_index=table.index,
                    title=table.title[:300],
                    locator_kind=table.kind,
                    columns=list(table.columns),
                    rows=[list(row) for row in table.rows],
                    row_positions=list(table.row_positions),
                    summary_rows=[list(row) for row in table.summary_rows],
                    structured=table.structured,
                    note=table.note[:200],
                )
                for table in tables or []
            )
            rows = [
                DocumentChunk(
                    document_id=doc.id,
                    chunk_index=c.chunk_index,
                    page_number=c.page_number,
                    locator=c.locator,
                    content=c.text,
                    embedding=to_bytes(vectors[i]),
                )
                for i, c in enumerate(chunks)
            ]
            db.add_all(rows)
            await db.flush()  # assigns the new, never-reused chunk IDs
            new_ids = [r.id for r in rows]
            now = datetime.now(UTC)
            doc.status = DOC_READY
            doc.locked_until = None
            doc.error_code = doc.error_message = None
            if page_count is not None:
                doc.page_count = page_count
            doc.chunk_count = len(rows)
            doc.indexed_version = doc.version
            doc.indexed_filename = doc.filename
            doc.descriptor = build_descriptor(
                doc.filename, [(c.text, c.locator) for c in chunks], [table.title for table in tables or []],
                date_order=self._settings.rag_date_order,
            )
            doc.descriptor_version = DESCRIPTOR_VERSION
            doc.embedding_model = self._embedder.model_name
            doc.indexed_at = now
            await db.commit()

        if self._indexer is not None:  # a separate worker process leaves the index to the API process
            try:
                await self._indexer.apply(remove_ids=old_ids, add_ids=new_ids, vectors=vectors)
            except Exception as exc:
                logger.error("Vector index update failed for %s (%s); will reconcile", job.document_id, type(exc).__name__)
        logger.info(
            "Document %s (%s) indexed: %s chunks, %s tables (%s structured)", job.document_id, job.file_format,
            len(new_ids), len(tables or []), sum(1 for table in tables or [] if table.structured),
        )

    async def _release(self, job: Job) -> None:
        """Hand an unfinished job back to the queue without counting the attempt (worker shutdown)."""
        async with self._sessionmaker() as db:
            await db.execute(
                update(Document)
                .where(Document.id == job.document_id, Document.version == job.version, Document.status == DOC_PROCESSING)
                .values(
                    status=DOC_PENDING, attempts=func.greatest(Document.attempts - 1, 0),
                    locked_until=None, next_attempt_at=datetime.now(UTC),
                )
            )
            await db.commit()
        logger.info("Document %s returned to the queue (worker shutting down)", job.document_id)

    def _mark_failed(self, doc: Document, code: str, message: str) -> None:
        if doc.indexed_version is not None:
            message = f"{message} The previously indexed version is still searchable."
        doc.status = DOC_FAILED
        doc.error_code = code
        doc.error_message = message[:255]
        doc.locked_until = None

    async def _fail(self, job: Job, code: str, message: str, *, permanent: bool) -> None:
        async with self._sessionmaker() as db:
            doc = await self._lock_current(db, job)
            if doc is None:
                return
            if permanent or doc.attempts >= self._settings.rag_max_attempts:
                self._mark_failed(doc, code, message)
            else:
                delay = self._settings.rag_retry_base_seconds * (2 ** (doc.attempts - 1))
                doc.status = DOC_PENDING
                doc.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
                doc.locked_until = None
                doc.error_code = code
                doc.error_message = f"{message} Retrying (attempt {doc.attempts} of {self._settings.rag_max_attempts})."[:255]
            await db.commit()
        logger.info("Document %s ingestion failed code=%s permanent=%s", job.document_id, code, permanent)
