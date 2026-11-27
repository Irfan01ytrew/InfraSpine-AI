"""A separate derived vector index for partner chunks, authorized by PostgreSQL on every search."""
import asyncio

import numpy as np
from sqlalchemy import func, select

from app.db.models import PartnerKnowledgeChunk, PartnerKnowledgeSource
from app.rag.embeddings import from_bytes


class PartnerIndexSynchronizer:
    def __init__(self, sessionmaker, store, embedder):
        self._sessionmaker = sessionmaker
        self.store = store
        self.embedder = embedder
        self._seen = None
        self._lock = asyncio.Lock()

    def _eligible(self):
        return (
            PartnerKnowledgeSource.status == "active",
            PartnerKnowledgeSource.index_status == "ready",
            PartnerKnowledgeSource.embedding_model == self.embedder.model_name,
            PartnerKnowledgeSource.embedding_dimension == self.embedder.dimension,
        )

    async def search(self, vector, allowed_ids: list[int], limit: int):
        """Rebuild from authoritative chunks when needed, then search only caller-authorized IDs."""
        if not allowed_ids:
            return []
        async with self._lock:
            async with self._sessionmaker() as db:
                count, highest = (await db.execute(
                    select(func.count(PartnerKnowledgeChunk.id), func.coalesce(func.max(PartnerKnowledgeChunk.id), 0))
                    .join(PartnerKnowledgeSource, PartnerKnowledgeSource.id == PartnerKnowledgeChunk.source_id)
                    .where(*self._eligible())
                )).one()
                marker = (int(count), int(highest))
                if marker != self._seen or self.store.needs_rebuild:
                    await asyncio.to_thread(self.store.begin_rebuild)
                    try:
                        result = await db.stream(
                            select(PartnerKnowledgeChunk.id, PartnerKnowledgeChunk.embedding)
                            .join(PartnerKnowledgeSource, PartnerKnowledgeSource.id == PartnerKnowledgeChunk.source_id)
                            .where(*self._eligible()).order_by(PartnerKnowledgeChunk.id)
                        )
                        try:
                            async for batch in result.partitions(256):
                                ids = [int(row.id) for row in batch]
                                vectors = np.stack([from_bytes(row.embedding, self.embedder.dimension) for row in batch])
                                await asyncio.to_thread(self.store.add_rebuild_batch, ids, vectors)
                        finally:
                            await result.close()
                        await asyncio.to_thread(self.store.finish_rebuild)
                        self._seen = marker
                    except BaseException:
                        await asyncio.to_thread(self.store.abort_rebuild)
                        raise
            return await asyncio.to_thread(self.store.search, vector, limit, allowed_ids)
