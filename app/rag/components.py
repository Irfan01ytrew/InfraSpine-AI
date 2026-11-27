"""Builds the RAG components once per process: `build_rag` for the API (`app.main.lifespan`), which owns
the vector indexes, and `build_worker` for a separate document worker process (`app.worker`), which never
opens them."""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.rag.embeddings import Embedder, SentenceTransformerEmbedder
from app.rag.ingestion import IndexSynchronizer, IngestionWorker
from app.rag.qdrant_store import QdrantVectorStore
from app.rag.storage import DocumentStorage
from app.rag.vector_store import FaissVectorStore, IndexLockedError, IndexOwnerLock, VectorStore
from app.services.outbox import OutboxWorker
from app.services.partner_index import PartnerIndexSynchronizer
from app.services.partner_ingestion import PartnerIngestionWorker


@dataclass
class RagComponents:
    storage: DocumentStorage
    embedder: Embedder
    vector_store: VectorStore
    indexer: IndexSynchronizer
    worker: IngestionWorker
    knowledge_store: VectorStore  # reserved optional vector index; approved knowledge currently uses PostgreSQL FTS
    outbox: OutboxWorker
    partner_store: VectorStore
    partner_indexer: PartnerIndexSynchronizer
    partner_worker: PartnerIngestionWorker
    index_lock: IndexOwnerLock | None = None
    # "owner" writes the index files and runs the background workers; a "replica" serves searches only.
    index_role: str = "owner"

    def close(self) -> None:
        try:
            for store in (self.vector_store, self.knowledge_store, self.partner_store):
                close = getattr(store, "close", None)
                if close is not None:
                    close()
        finally:
            if self.index_lock is not None:
                self.index_lock.release()


def data_dir(settings: Settings) -> Path:
    return settings.rag_data_dir.resolve()


def build_embedder(settings: Settings) -> Embedder:
    models = Path(settings.rag_models_dir).resolve() if settings.rag_models_dir else data_dir(settings) / "models"
    return SentenceTransformerEmbedder(
        settings.rag_embedding_model,
        settings.rag_embedding_dimension,
        models,
        allow_download=settings.rag_embedding_allow_download,
    )


def build_vector_store(settings: Settings, embedder: Embedder, name: str = "vector_index", *, persist: bool = True) -> VectorStore:
    """Build only the explicitly selected backend; FAISS remains the default and rollback option."""
    if settings.rag_vector_store == "faiss":
        return FaissVectorStore(data_dir(settings) / name, embedder.dimension, embedder.model_name, persist=persist)
    if settings.rag_vector_store == "qdrant":
        api_key = settings.rag_qdrant_api_key.get_secret_value() if settings.rag_qdrant_api_key else None
        return QdrantVectorStore(
            url=settings.rag_qdrant_url,
            api_key=api_key,
            directory=data_dir(settings),
            name=name,
            dimension=embedder.dimension,
            model_name=embedder.model_name,
        )
    raise ValueError(f"Unsupported vector store: {settings.rag_vector_store}")


def claim_index_role(directory: Path, requested: str) -> tuple[IndexOwnerLock | None, str]:
    """Only one process may write a data directory's indexes; a second owner fails here, before loading them,
    unless `requested` ("auto"/"replica", from RAG_INDEX_ROLE) lets it run as an in-memory replica instead."""
    if requested == "replica":
        return None, "replica"
    lock = IndexOwnerLock(directory)
    try:
        lock.acquire()
    except IndexLockedError:
        if requested != "auto":
            raise
        return None, "replica"
    return lock, "owner"


def build_rag(settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]) -> RagComponents:
    index_lock, role = claim_index_role(data_dir(settings), settings.rag_index_role)
    persist = role == "owner"
    stores: list[VectorStore] = []
    try:
        storage = DocumentStorage(data_dir(settings) / "uploads")
        embedder = build_embedder(settings)
        store = build_vector_store(settings, embedder, persist=persist)
        stores.append(store)
        indexer = IndexSynchronizer(sessionmaker, store, embedder)
        worker = IngestionWorker(sessionmaker, settings, storage, embedder, indexer)
        knowledge_store = build_vector_store(settings, embedder, "knowledge_index", persist=persist)
        stores.append(knowledge_store)
        outbox = OutboxWorker(sessionmaker, settings, knowledge_store)
        partner_store = build_vector_store(settings, embedder, "partner_index", persist=persist)
        stores.append(partner_store)
        partner_indexer = PartnerIndexSynchronizer(sessionmaker, partner_store, embedder)
        partner_worker = PartnerIngestionWorker(sessionmaker, settings, embedder)
        return RagComponents(storage, embedder, store, indexer, worker, knowledge_store, outbox,
                             partner_store, partner_indexer, partner_worker, index_lock, role)
    except BaseException:
        try:
            for store in stores:
                close = getattr(store, "close", None)
                if close is not None:
                    close()
        finally:
            if index_lock is not None:
                index_lock.release()
        raise


def build_worker(settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]) -> IngestionWorker:
    """A document worker for a separate process: it writes PostgreSQL only and leaves the vector index to
    the API process, so any number of workers can run without touching the index files."""
    storage = DocumentStorage(data_dir(settings) / "uploads")
    return IngestionWorker(sessionmaker, settings, storage, build_embedder(settings), indexer=None)
