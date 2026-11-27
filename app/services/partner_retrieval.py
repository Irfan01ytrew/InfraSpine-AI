"""Partner-scoped lexical and local vector retrieval with database authorization."""
import asyncio
import logging
import re

import numpy as np
from sqlalchemy import and_, func, or_, select

from app.db.models import PartnerKnowledgeChunk, PartnerKnowledgeSource
from app.rag.retrieval import RetrievedChunk

logger = logging.getLogger(__name__)
_STOPWORDS = {"about", "all", "and", "are", "can", "does", "for", "from", "give", "had", "has", "have",
              "how", "into", "is", "it", "our", "please", "show", "summarize", "tell", "that", "the",
              "this", "to", "was", "were", "what", "when", "where", "which", "who", "why", "with", "you"}


async def retrieve_partner_chunks(db, workspace_id, question, indexer, embedder, min_similarity=0.2):
    terms = list(dict.fromkeys(term for term in re.findall(r"[^\W_]+", question.casefold(), flags=re.UNICODE)
                               if len(term) >= 3 and term not in _STOPWORDS))[:32]
    scope = (
        PartnerKnowledgeChunk.workspace_id == workspace_id,
        PartnerKnowledgeSource.workspace_id == workspace_id,
        PartnerKnowledgeSource.status == "active",
        PartnerKnowledgeSource.index_status == "ready",
    )
    join_on = and_(PartnerKnowledgeSource.id == PartnerKnowledgeChunk.source_id,
                   PartnerKnowledgeSource.workspace_id == PartnerKnowledgeChunk.workspace_id)
    lexical = []
    if terms:
        query = func.to_tsquery("simple", " | ".join(terms))
        rank = func.ts_rank(PartnerKnowledgeChunk.search_vector, query)
        lexical = list((await db.execute(select(PartnerKnowledgeChunk.id).join(PartnerKnowledgeSource, join_on)
            .where(*scope, PartnerKnowledgeChunk.search_vector.op("@@")(query))
            .order_by(rank.desc(), PartnerKnowledgeChunk.id.desc()).limit(50))).scalars())

    vector_results = []
    if indexer is not None and embedder is not None:
        allowed = list((await db.execute(select(PartnerKnowledgeChunk.id).join(PartnerKnowledgeSource, join_on)
            .where(*scope, PartnerKnowledgeSource.embedding_model == embedder.model_name,
                   PartnerKnowledgeSource.embedding_dimension == embedder.dimension))).scalars())
        if allowed:
            try:
                vector = np.asarray(await asyncio.to_thread(embedder.embed, [question]), dtype=np.float32)
                if vector.shape != (1, embedder.dimension) or not np.isfinite(vector).all():
                    raise ValueError("Invalid query embedding")
                vector_results = [(chunk_id, score) for chunk_id, score in
                                  await indexer.search(vector[0], allowed, 50)
                                  if score >= max(0.35, min_similarity)]
            except Exception as exc:
                logger.warning("Partner semantic retrieval unavailable: %s", type(exc).__name__)

    scores: dict[int, float] = {}
    for position, chunk_id in enumerate(lexical, 1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (60 + position)
    for position, (chunk_id, _) in enumerate(vector_results, 1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (60 + position)
    rows = []
    if scores:
        rows = (await db.execute(select(PartnerKnowledgeChunk, PartnerKnowledgeSource)
            .join(PartnerKnowledgeSource, join_on).where(*scope, PartnerKnowledgeChunk.id.in_(scores)))).all()
    rows.sort(key=lambda row: (-scores[row[0].id], row[0].id))
    chunks = []
    used_chars = 0
    for chunk, source in rows[:8]:
        remaining = 12_000 - used_chars
        if remaining <= 0:
            break
        content = chunk.content[:remaining]
        used_chars += len(content)
        chunks.append(RetrievedChunk(chunk_id=chunk.id, document_id=source.id, filename=source.title,
            page_number=chunk.page_number, chunk_index=chunk.chunk_index, content=content,
            score=scores[chunk.id], locator=chunk.locator))

    # Existing text remains searchable while queued or reindexed; uploaded documents require ready chunks.
    if terms and len(chunks) < 8 and used_chars < 12_000:
        query = func.to_tsquery("simple", " | ".join(terms))
        rank = func.ts_rank(PartnerKnowledgeSource.search_vector, query)
        legacy = (await db.execute(select(PartnerKnowledgeSource, rank.label("rank")).where(
            PartnerKnowledgeSource.workspace_id == workspace_id, PartnerKnowledgeSource.status == "active",
            PartnerKnowledgeSource.source_format == "text", PartnerKnowledgeSource.index_status != "ready",
            PartnerKnowledgeSource.search_vector.op("@@")(query),
        ).order_by(rank.desc(), PartnerKnowledgeSource.created_at.desc()).limit(8 - len(chunks)))).all()
        for source, score in legacy:
            remaining = 12_000 - used_chars
            if remaining <= 0:
                break
            content = source.content[:remaining]
            used_chars += len(content)
            chunks.append(RetrievedChunk(chunk_id=-(len(chunks) + 1), document_id=source.id, filename=source.title,
                page_number=1, chunk_index=0, content=content, score=float(score or 0),
                locator={"kind": "section", "position": 1, "label": source.title}))
    return chunks
