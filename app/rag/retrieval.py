"""Permission-filtered semantic retrieval.

Authorization happens in PostgreSQL, with `access.readable_by`, on every request; nothing is
cached, so permission changes apply to the next search. Two equivalent strategies, chosen by
the number of chunks the user may read (an estimate from `documents.chunk_count`):

* Small scopes: the readable chunk IDs are fetched first and the vector search is restricted
  to them, so the vector store cannot surface anything else.
* Large scopes: fetching hundreds of thousands of IDs per question costs more than the search
  itself, so the exact (unfiltered) vector search returns its best candidates and PostgreSQL
  authorizes them in score order, widening the candidate pool until `top_k` are readable. The
  index is exhaustive, so the first `top_k` readable candidates are exactly the pre-filtered
  result. If readable chunks are too rare among the nearest vectors, it falls back to the
  pre-filtered search: results never depend on the pool size, only the latency does.

Either way content is then loaded from PostgreSQL with the same filter again (defence in depth,
it drops chunks deleted since the index was last updated and access revoked mid-request).
"""

import asyncio
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentChunk, KnowledgeEvidence, KnowledgeRevision, ResourceAcl, User
from app.rag import access
from app.rag.embeddings import Embedder
from app.rag.parsers.base import chunk_locator
from app.rag.query_analysis import analyze_query, guarded, lexical_candidates
from app.rag.vector_store import VectorStore


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    document_id: uuid.UUID
    filename: str
    page_number: int
    chunk_index: int
    content: str
    score: float
    locator: dict | None = None  # structured source location; None means a PDF page (`page_number`)

    @property
    def location(self) -> dict:
        return chunk_locator(self.locator, self.page_number)

    @property
    def is_page(self) -> bool:
        return self.location.get("kind") == "page"

    @property
    def location_label(self) -> str:
        return str(self.location.get("label") or f"Page {self.page_number}")

    @property
    def citation_key(self) -> tuple:
        """Passages from the same page (PDF) or the same location (other formats) share one citation."""
        return ("page", self.page_number) if self.is_page else ("location", self.location_label)


def _scope(
    user: User,
    model_name: str,
    document_ids: list[uuid.UUID] | None = None,
    department_group_id: uuid.UUID | None = None,
    file_formats: set[str] | None = None,
) -> list:
    """Readable by this user, indexed with the current model, and optionally shared with one department."""
    conditions = [access.readable_by(user), Document.embedding_model == model_name, Document.indexed_version.is_not(None)]
    if document_ids:
        conditions.append(Document.id.in_(document_ids))
    if file_formats:
        conditions.append(Document.file_format.in_(file_formats))
    if department_group_id is not None:
        conditions.append(
            select(ResourceAcl.id)
            .where(
                ResourceAcl.document_id == Document.id,
                ResourceAcl.principal_group_id == department_group_id,
            )
            .exists()
        )
    return conditions


# Scopes of up to this many readable chunks are searched pre-filtered (fetching that many IDs is cheap);
# larger ones are searched candidate-first. Both return the same chunks.
PREFILTER_MAX_CHUNKS = 20_000
# Successive candidate pools for candidate-first search; past the last, it falls back to pre-filtering.
CANDIDATE_POOLS = (256, 4096)
_AUTHORIZE_BATCH = 4096


async def _readable_chunk_ids(db: AsyncSession, scope: list) -> list[int]:
    return list(
        (
            await db.execute(
                select(DocumentChunk.id).join(Document, Document.id == DocumentChunk.document_id).where(*scope)
            )
        ).scalars()
    )


async def _authorized_candidates(
    db: AsyncSession, store: VectorStore, scope: list, query_vector: np.ndarray, top_k: int, min_similarity: float
) -> list[tuple[int, float]] | None:
    """The `top_k` best readable chunks found among the nearest vectors, or None when readable chunks are too
    rare there to be sure of them (the caller then searches pre-filtered)."""
    seen: set[int] = set()
    hits: list[tuple[int, float]] = []
    for pool in CANDIDATE_POOLS:
        candidates = await asyncio.to_thread(store.search_all, query_vector, pool)
        fresh = [(i, s) for i, s in candidates if i not in seen]  # by ID: the index may change between pools
        for start in range(0, len(fresh), _AUTHORIZE_BATCH):
            batch = fresh[start : start + _AUTHORIZE_BATCH]
            rows = await db.execute(
                select(DocumentChunk.id)
                .join(Document, Document.id == DocumentChunk.document_id)
                .where(DocumentChunk.id.in_([i for i, _ in batch]), *scope)
            )
            readable = set(rows.scalars())
            hits += [(i, s) for i, s in batch if i in readable]
        seen.update(i for i, _ in fresh)
        hits.sort(key=lambda hit: hit[1], reverse=True)
        # Complete when top_k are readable, when every vector was a candidate, or when the remaining vectors
        # all score below the threshold (none of them could be returned).
        if len(hits) >= top_k or len(candidates) < pool or (candidates and candidates[-1][1] < min_similarity):
            return hits[:top_k]
    return None


async def searchable_documents(
    db: AsyncSession,
    user: User,
    model_name: str,
    candidate_ids: list[uuid.UUID] | None = None,
    *,
    department_group_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """IDs of searchable documents the user may read. Keeps `candidate_ids` order (untrusted input is filtered)."""
    rows = await db.execute(
        select(Document.id).where(*_scope(user, model_name, candidate_ids, department_group_id))
    )
    allowed = set(rows.scalars())
    if candidate_ids:
        return [i for i in dict.fromkeys(candidate_ids) if i in allowed]
    return sorted(allowed)


async def searchable_media_documents(
    db: AsyncSession,
    user: User,
    model_name: str,
    file_formats: set[str],
    *,
    candidate_ids: list[uuid.UUID] | None = None,
    department_group_id: uuid.UUID | None = None,
    limit: int = 5,
) -> list[uuid.UUID]:
    """Return a bounded, permission-filtered set of indexed media documents in recency order.

    This is a database fallback for media queries when the vector index has no candidate (for example, a
    previously indexed transcript whose vector was skipped during an index rebuild). The query never loads the
    library's full document list and every returned ID is restricted by the same live PostgreSQL ACL as retrieval.
    Duplicate uploads with the same filename collapse to the latest indexed copy.
    """
    if not file_formats or limit <= 0:
        return []
    scope = _scope(user, model_name, candidate_ids, department_group_id, file_formats)
    rows = (
        await db.execute(
            select(Document.id, Document.indexed_filename, Document.filename)
            .where(*scope, Document.status == "ready", Document.chunk_count > 0)
            .order_by(Document.created_at.desc(), Document.id.desc())
            .limit(max(limit * 4, limit))
        )
    ).all()
    result: list[uuid.UUID] = []
    seen_names: set[str] = set()
    for document_id, indexed_filename, filename in rows:
        name = (indexed_filename or filename or "").casefold()
        if name in seen_names:
            continue
        seen_names.add(name)
        result.append(document_id)
        if len(result) >= limit:
            break
    return result


async def media_documents_named_in_question(
    db: AsyncSession,
    user: User,
    model_name: str,
    question: str,
    file_formats: set[str],
    *,
    candidate_ids: list[uuid.UUID] | None = None,
    department_group_id: uuid.UUID | None = None,
    limit: int = 5,
) -> list[uuid.UUID]:
    """Resolve an explicitly typed media filename through an ACL-filtered filename match."""
    if not file_formats:
        return []
    question_text = " ".join(question.split()).casefold()
    scope = _scope(user, model_name, candidate_ids, department_group_id, file_formats)
    rows = (
        await db.execute(
            select(Document.id, Document.indexed_filename, Document.filename)
            .where(
                *scope,
                Document.status == "ready",
                Document.chunk_count > 0,
                func.strpos(
                    question_text,
                    func.lower(func.coalesce(Document.indexed_filename, Document.filename)),
                ) > 0,
            )
            .order_by(Document.created_at.desc(), Document.id.desc())
            .limit(max(limit * 4, limit))
        )
    ).all()
    result: list[uuid.UUID] = []
    seen_names: set[str] = set()
    for document_id, indexed_filename, filename in rows:
        display_name = " ".join((indexed_filename or filename or "").split()).casefold()
        if not display_name or display_name in seen_names:
            continue
        seen_names.add(display_name)
        result.append(document_id)
        if len(result) >= limit:
            break
    return result


async def document_catalog(db: AsyncSession, user: User, model_name: str, *, limit: int = 100):
    """Return authorized searchable document count, total stored bytes, and bounded metadata."""
    from sqlalchemy import func

    scope = [*_scope(user, model_name), Document.status == "ready"]
    count, total_bytes = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(Document.size_bytes), 0))
            .select_from(Document)
            .where(*scope)
        )
    ).one()
    rows = await db.execute(
        select(Document.filename, Document.file_format, Document.created_at, Document.status)
        .where(*scope)
        .order_by(Document.created_at.desc(), Document.id)
        .limit(limit)
    )
    return int(count or 0), int(total_bytes or 0), list(rows)


async def document_passages(
    db: AsyncSession,
    user: User,
    model_name: str,
    document_id: uuid.UUID,
    max_chars: int,
    *,
    department_group_id: uuid.UUID | None = None,
) -> list[RetrievedChunk]:
    """The whole document in page order, for overview questions. If it exceeds the budget,
    passages are sampled evenly across the document so every part is represented."""
    rows = (
        await db.execute(
            select(DocumentChunk, Document.indexed_filename)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.document_id == document_id,
                *_scope(user, model_name, department_group_id=department_group_id),
            )
            .order_by(DocumentChunk.chunk_index)
        )
    ).all()
    chunks = [
        RetrievedChunk(
            c.id, c.document_id, name or "document.pdf", c.page_number, c.chunk_index, c.content, 1.0, c.locator
        )
        for c, name in rows
    ]
    total = sum(len(c.content) for c in chunks)
    if total <= max_chars:
        return chunks
    keep = max(1, int(len(chunks) * max_chars / total))
    step = len(chunks) / keep
    return [chunks[int(i * step)] for i in range(keep)]


async def retrieve_approved_knowledge(
    db: AsyncSession,
    user: User,
    question: str,
    *,
    top_k: int,
    document_ids: list[uuid.UUID] | None = None,
    department_group_id: uuid.UUID | None = None,
) -> list[RetrievedChunk]:
    """PostgreSQL full-text search over published statements, with live source ACL checks before loading text."""
    from app.rag.access import knowledge_readable_by

    terms = list(dict.fromkeys(
        term for term in re.findall(r"[^\W_]+", question.casefold(), flags=re.UNICODE)
        if len(term) >= 3 and term not in _KNOWLEDGE_QUERY_STOPWORDS
    ))[:32]
    if not terms:
        return []
    # Terms are extracted to alphanumeric lexemes before building the OR query, so user text cannot
    # inject PostgreSQL tsquery operators and ordinary question words do not block a useful match.
    query = func.to_tsquery("simple", " | ".join(terms))
    vector = func.to_tsvector("simple", KnowledgeRevision.statement)
    rank = func.ts_rank_cd(vector, query)
    conditions = [knowledge_readable_by(user), vector.op("@@")(query)]
    if department_group_id is not None:
        conditions.append(KnowledgeRevision.scope_group_id == department_group_id)
    if document_ids:
        conditions.append(
            select(KnowledgeEvidence.id)
            .where(
                KnowledgeEvidence.revision_id == KnowledgeRevision.id,
                KnowledgeEvidence.document_id.in_(document_ids),
            )
            .exists()
        )
    rows = (
        await db.execute(
            select(KnowledgeRevision.id, KnowledgeRevision.statement, rank.label("rank"))
            .where(*conditions)
            .order_by(rank.desc(), KnowledgeRevision.created_at.desc())
            .limit(top_k)
        )
    ).all()
    if not rows:
        return []
    revision_ids = [row.id for row in rows]
    evidence_rows = (
        await db.execute(
            select(KnowledgeEvidence, Document)
            .join(Document, Document.id == KnowledgeEvidence.document_id)
            .where(
                KnowledgeEvidence.revision_id.in_(revision_ids),
                KnowledgeEvidence.orphan_reason.is_(None),
                Document.indexed_version == KnowledgeEvidence.document_version,
                Document.version == KnowledgeEvidence.document_version,
                Document.sha256 == KnowledgeEvidence.document_sha256,
                access.readable_by(user),
                *([KnowledgeEvidence.document_id.in_(document_ids)] if document_ids else []),
            )
            .order_by(KnowledgeEvidence.revision_id, KnowledgeEvidence.id)
        )
    ).all()
    evidence_by_revision = {}
    for evidence, document in evidence_rows:
        evidence_by_revision.setdefault(evidence.revision_id, (evidence, document))
    chunks = []
    for row in rows:
        source = evidence_by_revision.get(row.id)
        if source is None:
            continue  # ACL or source version changed between the candidate and provenance queries.
        evidence, document = source
        raw_rank = max(0.0, float(row.rank or 0.0))
        score = raw_rank / (raw_rank + 0.05) if raw_rank else 0.0
        content = (
            "Approved organizational knowledge:\n"
            f"{row.statement}\n"
            "Verbatim supporting passage:\n"
            f"{evidence.quote}"
        )
        chunks.append(
            RetrievedChunk(
                chunk_id=-int(evidence.id),
                document_id=document.id,
                filename=document.indexed_filename or evidence.filename,
                page_number=evidence.page_number,
                chunk_index=-int(evidence.id),
                content=content,
                score=score,
            )
        )
    return chunks


_KNOWLEDGE_QUERY_STOPWORDS = {
    "what", "where", "which", "when", "who", "does", "with", "from", "about", "tell", "show", "explain",
    "give", "please", "summarize", "how", "why", "can", "our", "you", "was", "were", "this", "that",
    "into", "have", "has", "had", "the", "and", "for", "are", "is", "not", "but", "all", "any",
}


async def retrieve(
    db: AsyncSession,
    user: User,
    embedder: Embedder,
    store: VectorStore,
    question: str,
    *,
    top_k: int,
    min_similarity: float,
    max_context_chars: int,
    document_ids: list[uuid.UUID] | None = None,
    department_group_id: uuid.UUID | None = None,
    query_transform: Callable[[str], str | list[str]] | None = None,
    hybrid: bool = False,
    correct: bool = False,
    max_per_document: int | None = None,
    lexical_limit: int = 50,
    hierarchical: bool = False,
    max_documents: int = 3,
    small_document_chars: int = 0,
    file_formats: set[str] | None = None,
) -> list[RetrievedChunk]:
    """The chunks to answer from, best first. Semantic search alone by default; with `hybrid`, PostgreSQL lexical
    matches are fused in and at most `max_per_document` chunks of one document are kept while other documents
    have eligible chunks; with `correct`, words the corpus does not contain are also searched in corrected form
    (see `app.rag.query_analysis` and docs/hybrid-retrieval.md); with `hierarchical`, documents the question names
    are resolved from their descriptors, documents are ranked before chunks, and the top documents arrive whole
    (up to `small_document_chars`) or with the chunks around their hits (docs/document-discovery.md). Every stage
    is restricted to `scope`."""
    scope = _scope(user, embedder.model_name, document_ids, department_group_id, file_formats)
    # `query_transform` returns the text to search, or several texts (e.g. an English expansion and the original
    # Urdu question, see `query_expansion.retrieval_queries`). Every text is searched within the same scope, the
    # embeddings come from one batch, and each chunk keeps its best score. One text means one search.
    transformed = query_transform(question) if query_transform is not None else question
    texts = [transformed] if isinstance(transformed, str) else list(transformed)
    queries = list(dict.fromkeys(text for text in texts if text)) or [question]
    analysis = None
    if hybrid or correct:
        analysis = await guarded(db, lambda: analyze_query(db, [question, *queries], correct=correct), None)
        if analysis is None:
            hybrid = False  # lexical search unavailable: semantic search as before
        elif analysis.corrected_text and analysis.corrected_text not in queries:
            queries.append(analysis.corrected_text)  # the corrected wording is searched semantically too
    pool = max(top_k * 4, 20) if hybrid else top_k  # fusion and diversification need more candidates

    readable = (await db.execute(select(func.coalesce(func.sum(Document.chunk_count), 0)).where(*scope))).scalar_one()
    allowed_ids = None
    if readable <= PREFILTER_MAX_CHUNKS:  # pre-filtered: a small scope
        allowed_ids = await _readable_chunk_ids(db, scope)
        if not allowed_ids:
            if file_formats:
                return []
            return await retrieve_approved_knowledge(
                db,
                user,
                question,
                top_k=top_k,
                document_ids=document_ids,
                department_group_id=department_group_id,
            )
    vectors = await asyncio.to_thread(embedder.embed, queries)  # one batch for both forms
    merged: dict[int, float] = {}
    for query_vector in vectors:
        hits = None
        if allowed_ids is None:  # candidate-first
            floor = min_similarity - (_LEXICAL_MARGIN if hybrid else 0.0)
            hits = await _authorized_candidates(db, store, scope, query_vector, pool, floor)
            if hits is None:  # readable candidates too rare: pre-filter after all
                allowed_ids = await _readable_chunk_ids(db, scope)
                if not allowed_ids:
                    if file_formats:
                        return []
                    return await retrieve_approved_knowledge(
                        db,
                        user,
                        question,
                        top_k=top_k,
                        document_ids=document_ids,
                        department_group_id=department_group_id,
                    )
        if hits is None:
            hits = await asyncio.to_thread(store.search, query_vector, pool, allowed_ids)
        for chunk_id, score in hits:
            if score > merged.get(chunk_id, float("-inf")):
                merged[chunk_id] = score
    best = sorted(merged.items(), key=lambda hit: hit[1], reverse=True)[:pool]
    if hybrid:
        ranked = await _fuse(db, scope, analysis, vectors, best, min_similarity, lexical_limit)
    else:
        ranked = [(chunk_id, score) for chunk_id, score in best[:top_k] if score >= min_similarity]
    if not ranked and not hierarchical:
        if file_formats:
            return []
        return await retrieve_approved_knowledge(
            db,
            user,
            question,
            top_k=top_k,
            document_ids=document_ids,
            department_group_id=department_group_id,
        )
    scores = dict(ranked)
    order = {chunk_id: index for index, (chunk_id, _) in enumerate(ranked)}

    chunks: list[RetrievedChunk] = []
    if scores:
        rows = await db.execute(
            select(DocumentChunk, Document.indexed_filename)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.id.in_(scores), *scope)
        )
        chunks = [_chunk(chunk, filename, scores[chunk.id]) for chunk, filename in rows]
        chunks.sort(key=lambda c: order[c.chunk_id])
    if hybrid:
        chunks = _diversify(chunks, top_k, max_per_document or top_k)
    if hierarchical:
        chunks = await _hierarchical(
            db, scope, store, vectors, question, chunks, max_documents=max_documents, small_chars=small_document_chars,
            min_similarity=min_similarity, chosen=document_ids,
        )
    knowledge_chunks = [] if file_formats else await retrieve_approved_knowledge(
        db,
        user,
        question,
        top_k=top_k,
        document_ids=document_ids,
        department_group_id=department_group_id,
    )
    chunks = _merge_ranked_sources(chunks, knowledge_chunks, top_k)
    if not chunks:
        return []

    # Only the most relevant chunks that fit the context budget are sent to the LLM.
    selected, used = [], 0
    for chunk in chunks:
        if selected and used + len(chunk.content) > max_context_chars:
            break
        selected.append(chunk)
        used += len(chunk.content)
    return selected


def _merge_ranked_sources(
    document_chunks: list[RetrievedChunk], knowledge_chunks: list[RetrievedChunk], top_k: int
) -> list[RetrievedChunk]:
    """Merge already-ranked document and approved-knowledge results without comparing unlike scores.

    Document `score` is cosine similarity (the hybrid order is reciprocal-rank fusion); approved knowledge uses a
    normalized PostgreSQL text rank. Sorting those raw values together discards hybrid ranking and can let a
    repetitive source outrank a stronger lexical match. Reciprocal-rank fusion keeps both orders meaningful while
    leaving the scores intact for traces and UI display. Curated knowledge wins exact cross-source rank ties.
    """
    candidates = [
        (1.0 / (_RRF_K + rank), source, rank, chunk)
        for source, chunks in enumerate((knowledge_chunks, document_chunks))
        for rank, chunk in enumerate(chunks, start=1)
    ]
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [chunk for _, _, _, chunk in candidates[:top_k]]


# Lexical evidence: a chunk that matches at least half of the question's known words, one of them distinctive
# (in few documents), is eligible below the similarity floor, as long as it is not dissimilar (cosine above 0):
# exact rare words are evidence that a short, broad or misspelt question's embedding misses. Semantic candidates
# are gathered down to `_LEXICAL_MARGIN` below the floor so both lists overlap.
_LEXICAL_MARGIN = 0.1
_LEXICAL_COVERAGE = 0.5
_RRF_K = 60  # reciprocal rank fusion constant (the usual value; rank, not raw score, is fused)


async def _fuse(
    db: AsyncSession,
    scope: list,
    analysis,
    vectors: np.ndarray,
    semantic: list[tuple[int, float]],
    min_similarity: float,
    lexical_limit: int,
) -> list[tuple[int, float]]:
    """Semantic and lexical candidates fused by reciprocal rank, kept when eligible, as (chunk id, cosine)."""
    lexical = (
        await guarded(db, lambda: lexical_candidates(db, scope, analysis, lexical_limit), [])
        if analysis is not None else []
    )
    cosine = dict(semantic)
    missing = [chunk_id for chunk_id, _, _, _ in lexical if chunk_id not in cosine]
    if missing:  # lexical-only candidates: their similarity from the stored embeddings (readable ones only)
        rows = await db.execute(
            select(DocumentChunk.id, DocumentChunk.embedding)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.id.in_(missing), *scope)
        )
        for chunk_id, blob in rows:
            vector = np.frombuffer(blob, dtype="<f4")
            if vector.shape[0] == vectors.shape[1]:
                cosine[chunk_id] = float(np.max(vectors @ vector))
    supported = {chunk_id for chunk_id, _, share, distinctive in lexical if share >= _LEXICAL_COVERAGE and distinctive}
    fused: dict[int, float] = {}
    # Semantic ranks over every candidate (lexical ones included, by their cosine): a nearest-neighbour pool that
    # one repetitive document fills would otherwise give a relevant lexical match no semantic credit at all.
    for rank, chunk_id in enumerate(sorted(cosine, key=lambda i: cosine[i], reverse=True), start=1):
        fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
    for rank, (chunk_id, _, share, _) in enumerate(lexical, start=1):
        # Weighted by the share of the question's known words the chunk contains: one shared common word is
        # weaker evidence than all of them.
        fused[chunk_id] = fused.get(chunk_id, 0.0) + share / (_RRF_K + rank)
    eligible = [
        (chunk_id, cosine[chunk_id])
        for chunk_id in sorted(fused, key=lambda i: fused[i], reverse=True)
        if chunk_id in cosine
        and (cosine[chunk_id] >= min_similarity or (chunk_id in supported and cosine[chunk_id] > 0.0))
    ]
    return eligible


def _diversify(chunks: list[RetrievedChunk], top_k: int, per_document: int) -> list[RetrievedChunk]:
    """The first `top_k` chunks in order, taking at most `per_document` from one document while others have
    eligible chunks; free places are then filled from the capped documents, in order."""
    picked: list[RetrievedChunk] = []
    held: list[RetrievedChunk] = []
    counts: dict[uuid.UUID, int] = {}
    for chunk in chunks:
        if len(picked) == top_k:
            break
        if counts.get(chunk.document_id, 0) < per_document:
            picked.append(chunk)
            counts[chunk.document_id] = counts.get(chunk.document_id, 0) + 1
        else:
            held.append(chunk)
    picked += held[: top_k - len(picked)]
    position = {chunk.chunk_id: index for index, chunk in enumerate(chunks)}
    return sorted(picked, key=lambda chunk: position[chunk.chunk_id])


# ------------------------------------------------------------------ hierarchical retrieval (docs/document-discovery.md)

# Besides the documents a question names, the top documents of the chunk ranking are expanded too: a small one
# arrives whole (its header, table and total are separate chunks), a large one with its hits' neighbours.
_EXPAND_CONTENT_DOCUMENTS = 2
_IN_DOCUMENT_HITS = 3  # a named large document with no hit of its own: its best chunks for the question
_RRF_DOCUMENTS = 60


def search_options(settings) -> dict:
    """The production retrieval options for `retrieve()`, from settings. Chat and the evaluation gate both use this,
    so the gate measures the retrieval users get (top_k, the similarity floor and the budget are passed apart)."""
    from app.rag.query_expansion import retrieval_queries

    return {
        "query_transform": retrieval_queries,
        "hybrid": settings.rag_hybrid_retrieval,
        "correct": settings.rag_query_analysis,
        "max_per_document": settings.rag_max_chunks_per_document,
        "lexical_limit": settings.rag_lexical_candidates,
        "hierarchical": settings.rag_hierarchical_retrieval,
        "max_documents": settings.rag_discovery_max_documents,
        "small_document_chars": settings.rag_small_document_chars,
    }


async def named_documents(
    db: AsyncSession,
    user: User,
    model_name: str,
    question: str,
    limit: int = 3,
    *,
    department_group_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Readable documents the question names by filename, title, headings or dates (best first)."""
    from app.rag.discovery import resolve_documents

    scope = _scope(user, model_name, department_group_id=department_group_id)
    matches = await guarded(db, lambda: resolve_documents(db, scope, question, limit=limit), [])
    return [match.document_id for match in matches]


def _chunk(chunk: DocumentChunk, filename: str | None, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        filename=filename or "document.pdf",
        page_number=chunk.page_number,
        chunk_index=chunk.chunk_index,
        content=chunk.content,
        score=score,
        locator=chunk.locator,
    )


def _similarity(vectors: np.ndarray, blob: bytes | None) -> float:
    if blob is None:
        return 0.0
    vector = np.frombuffer(blob, dtype="<f4")
    return float(np.max(vectors @ vector)) if vector.shape[0] == vectors.shape[1] else 0.0


async def _hierarchical(
    db: AsyncSession,
    scope: list,
    store: VectorStore,
    vectors: np.ndarray,
    question: str,
    hits: list[RetrievedChunk],
    *,
    max_documents: int,
    small_chars: int,
    min_similarity: float,
    chosen: list[uuid.UUID] | None = None,
) -> list[RetrievedChunk]:
    """Documents first, then chunks. Documents are ranked by reciprocal-rank fusion of the descriptor match (the
    question names them) and the chunk ranking (their best chunk's place). A named document is expanded: whole when
    small, otherwise its best chunks with their neighbours. A small top document of the chunk ranking adds only its
    fragments that clear `min_similarity` themselves. Every row is loaded under `scope`; added chunks keep their own
    cosine as `score`. Documents the search was restricted to (`chosen`: picked by the user, or a follow-up's
    resolved documents) were chosen explicitly, so the best of them are expanded as named ones are."""
    from app.rag.discovery import resolve_documents

    named = await guarded(db, lambda: resolve_documents(db, scope, question, limit=max_documents), [])
    content_order = list(dict.fromkeys(chunk.document_id for chunk in hits))
    # Dense ranks: documents whose names match equally well share a rank, so the chunk ranking decides between them.
    levels = sorted({round(match.score, 6) for match in named}, reverse=True)
    name_rank = {match.document_id: levels.index(round(match.score, 6)) + 1 for match in named}
    content_rank = {document_id: rank for rank, document_id in enumerate(content_order, start=1)}
    if chosen:
        for document_id in sorted(dict.fromkeys(chosen), key=lambda d: content_rank.get(d, 10**6))[:max_documents]:
            name_rank.setdefault(document_id, len(levels) + 1)
    fused = {
        document_id: (1.0 / (_RRF_DOCUMENTS + name_rank[document_id]) if document_id in name_rank else 0.0)
        + (1.0 / (_RRF_DOCUMENTS + content_rank[document_id]) if document_id in content_rank else 0.0)
        for document_id in {*name_rank, *content_rank}
    }
    # Ties go to the name: a document the question names outranks one that only reads alike.
    ordered = sorted(fused, key=lambda document_id: (
        -fused[document_id], name_rank.get(document_id, 10**6), content_rank.get(document_id, 10**6),
    ))
    expand = set(name_rank) | set(content_order[:_EXPAND_CONTENT_DOCUMENTS])
    sizes = dict(
        (
            await db.execute(
                select(DocumentChunk.document_id, func.sum(func.length(DocumentChunk.content)))
                .join(Document, Document.id == DocumentChunk.document_id)
                .where(DocumentChunk.document_id.in_(expand), *scope)
                .group_by(DocumentChunk.document_id)
            )
        ).all()
    ) if expand else {}

    out: list[RetrievedChunk] = []
    for document_id in ordered:
        own = [chunk for chunk in hits if chunk.document_id == document_id]
        if document_id not in expand or document_id not in sizes:  # not expanded, or no longer readable
            out += own
            continue
        small = sizes[document_id] <= small_chars
        have = {chunk.chunk_id for chunk in own}
        if document_id in name_rank:
            # The question names this document: all of it when small, else its best chunks and their neighbours.
            if not small and not own:
                own = await _best_in_document(db, scope, store, vectors, document_id)
                have = {chunk.chunk_id for chunk in own}
            wanted = None if small else {chunk.chunk_index + step for chunk in own for step in (-1, 1)}
            out += own + await _siblings(db, scope, vectors, document_id, have, wanted)
        elif small:
            # A top document of the chunk ranking: its other fragments only when they are relevant themselves (a
            # table row about the asked thing joins its header; an unrelated page of the same file does not).
            siblings = await _siblings(db, scope, vectors, document_id, have, None)
            out += own + [chunk for chunk in siblings if chunk.score >= min_similarity]
        else:
            out += own  # large and not named: the chunk ranking's per-document cap stands
    return out


async def _siblings(
    db: AsyncSession, scope: list, vectors: np.ndarray, document_id: uuid.UUID, have: set[int], indexes: set[int] | None
) -> list[RetrievedChunk]:
    """The document's other chunks (all of them, or those at `indexes`), in reading order."""
    query = (
        select(DocumentChunk, Document.indexed_filename)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.document_id == document_id, *scope)
        .order_by(DocumentChunk.chunk_index)
    )
    if indexes is not None:
        query = query.where(DocumentChunk.chunk_index.in_(indexes))
    rows = (await db.execute(query)).all()
    return [
        _chunk(chunk, filename, _similarity(vectors, chunk.embedding))
        for chunk, filename in rows
        if chunk.id not in have
    ]


async def _best_in_document(
    db: AsyncSession, scope: list, store: VectorStore, vectors: np.ndarray, document_id: uuid.UUID
) -> list[RetrievedChunk]:
    """A named document's best chunks for the question, with no similarity floor: the name already chose it."""
    ids = list(
        (
            await db.execute(
                select(DocumentChunk.id)
                .join(Document, Document.id == DocumentChunk.document_id)
                .where(DocumentChunk.document_id == document_id, *scope)
            )
        ).scalars()
    )
    if not ids:
        return []
    best: dict[int, float] = {}
    for vector in vectors:
        for chunk_id, score in await asyncio.to_thread(store.search, vector, _IN_DOCUMENT_HITS, ids):
            best[chunk_id] = max(score, best.get(chunk_id, float("-inf")))
    top = sorted(best.items(), key=lambda hit: hit[1], reverse=True)[:_IN_DOCUMENT_HITS]
    if not top:
        return []
    rows = await db.execute(
        select(DocumentChunk, Document.indexed_filename)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.id.in_([chunk_id for chunk_id, _ in top]), *scope)
    )
    scores = dict(top)
    return sorted((_chunk(chunk, filename, scores[chunk.id]) for chunk, filename in rows), key=lambda c: -c.score)
