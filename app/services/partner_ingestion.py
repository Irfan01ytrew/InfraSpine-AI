"""Durable partner-only document ingestion; workers never write an API process's vector index."""
import asyncio
import hashlib
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import delete, or_, select, update

from app.db.models import PartnerKnowledgeChunk, PartnerKnowledgeSource
from app.rag.chunking import chunk_sections
from app.rag.embeddings import to_bytes
from app.rag.isolation import ProcessingCrashed, parse_and_chunk, run_isolated
from app.rag.parsers.base import Locator, ParseError, Section
from app.rag.parsers.registry import limits_from_settings
from app.rag.storage import DocumentStorage

logger = logging.getLogger(__name__)
_ABANDONED = object()


@dataclass(frozen=True)
class PartnerJob:
    source_id: uuid.UUID
    workspace_id: uuid.UUID
    token: uuid.UUID
    source_format: str
    storage_key: str | None
    content: str
    content_sha256: str
    title: str


class PartnerIngestionWorker:
    def __init__(self, sessionmaker, settings, embedder):
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.embedder = embedder
        self.storage = DocumentStorage(settings.rag_data_dir.resolve() / "partner_uploads")

    async def _claim(self):
        now = datetime.now(UTC)
        async with self.sessionmaker() as db:
            source = (await db.execute(
                select(PartnerKnowledgeSource).where(
                    PartnerKnowledgeSource.status == "active",
                    or_(
                        (PartnerKnowledgeSource.index_status == "pending") & (PartnerKnowledgeSource.next_attempt_at <= now),
                        (PartnerKnowledgeSource.index_status == "processing") & (PartnerKnowledgeSource.locked_until < now),
                        (PartnerKnowledgeSource.index_status == "ready") & or_(
                            PartnerKnowledgeSource.embedding_model.is_distinct_from(self.embedder.model_name),
                            PartnerKnowledgeSource.embedding_dimension.is_distinct_from(self.embedder.dimension),
                        ),
                    ),
                ).order_by(PartnerKnowledgeSource.next_attempt_at, PartnerKnowledgeSource.created_at)
                .limit(1).with_for_update(skip_locked=True)
            )).scalar_one_or_none()
            if source is None:
                return None
            if source.index_status == "ready":
                source.index_attempts = 0
            if source.index_attempts >= self.settings.rag_max_attempts:
                source.index_status = "failed"
                source.index_error_code = "processing_abandoned"
                source.locked_until = None
                source.claim_token = None
                await db.commit()
                return _ABANDONED
            source.index_status = "processing"
            source.index_attempts += 1
            source.claim_token = uuid.uuid4()
            source.locked_until = now + timedelta(seconds=self.settings.rag_processing_lease_seconds)
            job = PartnerJob(source.id, source.workspace_id, source.claim_token, source.source_format,
                             source.storage_key, source.content, source.content_sha256, source.title)
            await db.commit()
            return job

    async def _renew(self, job):
        interval = max(3, self.settings.rag_processing_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            async with self.sessionmaker() as db:
                await db.execute(update(PartnerKnowledgeSource).where(
                    PartnerKnowledgeSource.id == job.source_id, PartnerKnowledgeSource.status == "active",
                    PartnerKnowledgeSource.index_status == "processing", PartnerKnowledgeSource.claim_token == job.token,
                ).values(locked_until=datetime.now(UTC) + timedelta(seconds=self.settings.rag_processing_lease_seconds)))
                await db.commit()

    def _build(self, job):
        if job.source_format == "text":
            if hashlib.sha256(job.content.encode("utf-8")).hexdigest() != job.content_sha256:
                raise ParseError("source_changed", "Source content failed its integrity check.")
            chunks = chunk_sections([Section(job.content, Locator("section", 1, job.title))],
                                    self.settings.rag_chunk_size, self.settings.rag_chunk_overlap)
        else:
            path = self.storage.path_for(job.storage_key or "")
            if not path.is_file():
                raise ParseError("file_missing", "The partner source file is missing.")
            if path.stat().st_size > self.settings.rag_max_upload_bytes:
                raise ParseError("file_too_large", "The partner source file exceeds the processing limit.")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != job.content_sha256:
                raise ParseError("source_changed", "Source content failed its integrity check.")
            args = (str(path), job.source_format, limits_from_settings(self.settings),
                    self.settings.rag_chunk_size, self.settings.rag_chunk_overlap, False, False)
            if self.settings.rag_parse_isolation:
                _, chunks, _, _ = run_isolated(parse_and_chunk, args,
                    timeout_seconds=self.settings.rag_processing_timeout_seconds,
                    memory_bytes=self.settings.rag_parser_memory_mb * 1024 * 1024, cancelled=threading.Event())
            else:
                _, chunks, _, _ = parse_and_chunk(*args)
        if not chunks:
            raise ParseError("no_text", "No extractable text found in the partner source.")
        vectors = np.asarray(self.embedder.embed([chunk.text for chunk in chunks]), dtype=np.float32)
        if vectors.shape != (len(chunks), self.embedder.dimension) or not np.isfinite(vectors).all():
            raise RuntimeError("Partner embedding output is invalid")
        if not np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=0.05):
            raise RuntimeError("Partner embedding output is not normalized")
        return chunks, vectors

    async def _finish(self, job, chunks, vectors):
        async with self.sessionmaker() as db:
            source = await db.scalar(select(PartnerKnowledgeSource).where(
                PartnerKnowledgeSource.id == job.source_id,
                PartnerKnowledgeSource.workspace_id == job.workspace_id,
            ).with_for_update())
            if source is None or source.status != "active" or source.index_status != "processing" or source.claim_token != job.token:
                return
            await db.execute(delete(PartnerKnowledgeChunk).where(PartnerKnowledgeChunk.source_id == job.source_id))
            for chunk, vector in zip(chunks, vectors, strict=True):
                db.add(PartnerKnowledgeChunk(source_id=job.source_id, workspace_id=job.workspace_id,
                    chunk_index=chunk.chunk_index, page_number=chunk.page_number, content=chunk.text,
                    locator=chunk.locator or {"kind": "page", "position": chunk.page_number, "label": f"Page {chunk.page_number}"},
                    embedding=to_bytes(vector)))
            source.index_status = "ready"
            source.index_error_code = None
            source.embedding_model = self.embedder.model_name
            source.embedding_dimension = self.embedder.dimension
            source.locked_until = None
            source.claim_token = None
            await db.commit()

    async def _fail(self, job, code, permanent):
        async with self.sessionmaker() as db:
            source = await db.scalar(select(PartnerKnowledgeSource).where(
                PartnerKnowledgeSource.id == job.source_id,
                PartnerKnowledgeSource.workspace_id == job.workspace_id,
            ).with_for_update())
            if source is None or source.status != "active" or source.index_status != "processing" or source.claim_token != job.token:
                return
            source.index_error_code = code[:64]
            source.locked_until = None
            source.claim_token = None
            if permanent or source.index_attempts >= self.settings.rag_max_attempts:
                source.index_status = "failed"
            else:
                source.index_status = "pending"
                delay = min(3600, self.settings.rag_retry_base_seconds * 2 ** (source.index_attempts - 1))
                source.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
            await db.commit()

    async def process_next(self):
        job = await self._claim()
        if job is None:
            return False
        if job is _ABANDONED:
            return True
        heartbeat = asyncio.create_task(self._renew(job))
        try:
            chunks, vectors = await asyncio.to_thread(self._build, job)
            await self._finish(job, chunks, vectors)
        except ParseError as exc:
            await self._fail(job, exc.code, True)
        except ProcessingCrashed:
            await self._fail(job, "processing_crashed", False)
        except Exception as exc:
            logger.warning("Partner ingestion failed: %s", type(exc).__name__)
            await self._fail(job, "processing_error", False)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        return True

    async def run(self, stop):
        while not stop.is_set():
            try:
                worked = await self.process_next()
            except Exception as exc:
                logger.error("Partner worker error: %s", type(exc).__name__)
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=3)
                except TimeoutError:
                    pass
