"""Run a golden dataset through the application's permission-aware retriever.

This does not embed with a different model and does not search a different index. `retrieve`
applies the same PostgreSQL visibility rule, FAISS restriction, similarity floor and context
budget as a normal question. K is passed as that function's `top_k`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Document, DocumentChunk, User
from app.rag.embeddings import Embedder
from app.rag.evaluation.dataset import Dataset, EvalCase, EvaluationError
from app.rag.evaluation.metrics import (
    chunk_is_relevant,
    citation_precision,
    first_relevant_rank,
    mean,
    ndcg_at_k,
    percentile,
    recall_at_k,
    reciprocal_rank,
)
from app.rag.evaluation.report import fingerprint, rounding
from app.rag.retrieval import _scope, retrieve
from app.rag.vector_store import VectorStore
from app.services.auth import normalize_email


async def evaluate(
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    store: VectorStore,
    dataset: Dataset,
    personas: dict[str, str],
    *,
    ks: Sequence[int],
    min_similarity: float,
    max_context_chars: int,
    query_transform: Callable[[str], str | list[str]] | None = None,
    retrieval_options: dict | None = None,
) -> dict:
    """`retrieval_options`: extra `retrieve()` options, such as production's `search_options(settings)` (hybrid
    search, query analysis, document discovery); none means semantic search alone."""
    ranks = _ks(ks)
    async with sessionmaker() as db:
        users = await _users(db, dataset, personas)
        rows = [
            await _case(
                db, users[case.persona], embedder, store, case, ranks, min_similarity, max_context_chars,
                query_transform, retrieval_options,
            )
            for case in dataset.cases
        ]
    report = _report(dataset, embedder, ranks, min_similarity, max_context_chars, rows)
    report["parameters"]["query_transform"] = query_transform.__name__ if query_transform else "none"
    if retrieval_options:  # recorded only when used, so semantic-only reports keep their fingerprints
        report["parameters"]["retrieval_options"] = sorted(
            key for key, value in retrieval_options.items() if value not in (None, False, 0)
        )
    report["fingerprint"] = fingerprint(_stable(report))
    return report


async def _users(db: AsyncSession, dataset: Dataset, personas: dict[str, str]) -> dict[str, User]:
    missing = sorted({case.persona for case in dataset.cases} - set(personas))
    if missing:
        raise EvaluationError(f"Persona map is missing: {', '.join(missing)}.")
    found: dict[str, User] = {}
    for name in sorted({case.persona for case in dataset.cases}):
        email = normalize_email(personas[name])
        user = (
            await db.execute(select(User).where(User.email == email, User.is_active.is_(True)))
        ).scalar_one_or_none()
        if user is None:
            raise EvaluationError(f"No active user for persona {name!r}.")
        found[name] = user
    return found


async def _case(
    db: AsyncSession,
    user: User,
    embedder: Embedder,
    store: VectorStore,
    case: EvalCase,
    ks: tuple[int, ...],
    min_similarity: float,
    max_context_chars: int,
    query_transform: Callable[[str], str | list[str]] | None,
    retrieval_options: dict | None = None,
) -> dict:
    labelled = (
        await db.execute(
            select(DocumentChunk.id, DocumentChunk.content, Document.indexed_filename)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(*_scope(user, embedder.model_name))
        )
    ).all()
    gold = {
        chunk_id
        for chunk_id, content, filename in labelled
        if chunk_is_relevant(filename or "", content, case)
    }
    started = time.perf_counter()
    retrieved = await retrieve(
        db,
        user,
        embedder,
        store,
        case.question,
        top_k=ks[-1],
        min_similarity=min_similarity,
        max_context_chars=max_context_chars,
        **({"query_transform": query_transform} | (retrieval_options or {})),
    )
    latency_ms = (time.perf_counter() - started) * 1000
    ranked = [chunk.chunk_id in gold for chunk in retrieved]
    return {
        "id": case.id,
        "question": case.question,
        "language": case.language,
        "persona": case.persona,
        "expected_document": case.expected_document,
        "expected_passage": case.expected_passage,
        "expect_abstain": case.expect_abstain,
        "abstained": not retrieved,
        "abstention_correct": (not retrieved) if case.expect_abstain else None,
        "gold_chunks": len(gold),
        "retrieved": len(retrieved),
        "first_relevant_rank": first_relevant_rank(ranked),
        "recall": {str(k): rounding(recall_at_k(ranked, k, len(gold))) for k in ks},
        "mrr": rounding(reciprocal_rank(ranked)),
        "ndcg": {str(k): rounding(ndcg_at_k(ranked, k, len(gold))) for k in ks},
        "citation_precision": None if case.expect_abstain else rounding(citation_precision(ranked)),
        "latency_ms": rounding(latency_ms),
    }


def _report(
    dataset: Dataset,
    embedder: Embedder,
    ks: tuple[int, ...],
    min_similarity: float,
    max_context_chars: int,
    rows: list[dict],
) -> dict:
    answerable = [row for row in rows if not row["expect_abstain"]]
    abstentions = [row for row in rows if row["expect_abstain"]]
    metrics: dict = {
        "mrr": rounding(mean([row["mrr"] for row in answerable])),
        "citation_precision": rounding(mean([row["citation_precision"] for row in answerable])),
        "abstention_accuracy": rounding(mean([1.0 if row["abstention_correct"] else 0.0 for row in abstentions])),
        "answerable_cases": len(answerable),
        "abstention_cases": len(abstentions),
        "latency_ms": {
            "mean": rounding(mean([row["latency_ms"] for row in rows])),
            "p50": rounding(percentile([row["latency_ms"] for row in rows], 50)),
            "p95": rounding(percentile([row["latency_ms"] for row in rows], 95)),
        },
    }
    for k in ks:
        metrics[f"recall@{k}"] = rounding(mean([row["recall"][str(k)] for row in answerable]))
        metrics[f"ndcg@{k}"] = rounding(mean([row["ndcg"][str(k)] for row in answerable]))
    return {
        "schema_version": 1,
        "baseline": {
            "vector_index": "faiss",
            "embedding_model": embedder.model_name,
            "embedding_dimension": embedder.dimension,
        },
        "dataset": {
            "name": dataset.name,
            "sha256": dataset.sha256,
            "cases": len(dataset.cases),
            "description": dataset.description,
        },
        "parameters": {
            "k": list(ks),
            "min_similarity": min_similarity,
            "max_context_chars": max_context_chars,
            "top_k": ks[-1],
        },
        "metrics": metrics,
        "by_language": _by_language(rows, ks),
        "cases": rows,
    }


def _by_language(rows: list[dict], ks: tuple[int, ...]) -> dict:
    headline = str(ks[-1])
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["language"], []).append(row)
    summary = {}
    for language in sorted(grouped):
        group = grouped[language]
        answerable = [row for row in group if not row["expect_abstain"]]
        abstentions = [row for row in group if row["expect_abstain"]]
        summary[language] = {
            "cases": len(group),
            "recall": rounding(mean([row["recall"][headline] for row in answerable])),
            "mrr": rounding(mean([row["mrr"] for row in answerable])),
            "citation_precision": rounding(mean([row["citation_precision"] for row in answerable])),
            "abstention_accuracy": rounding(
                mean([1.0 if row["abstention_correct"] else 0.0 for row in abstentions])
            ),
        }
    return summary


def _stable(report: dict) -> dict:
    cases = []
    for row in report["cases"]:
        stable = {key: value for key, value in row.items() if key != "latency_ms"}
        cases.append(stable)
    metrics = {key: value for key, value in report["metrics"].items() if key != "latency_ms"}
    return {
        "baseline": report["baseline"],
        "by_language": report["by_language"],
        "cases": cases,
        "dataset": {key: report["dataset"][key] for key in ("cases", "name", "sha256")},
        "metrics": metrics,
        "parameters": report["parameters"],
        "schema_version": report["schema_version"],
    }


def _ks(ks: Sequence[int]) -> tuple[int, ...]:
    if not ks:
        raise EvaluationError("Choose at least one K.")
    cleaned = tuple(sorted(set(ks)))
    if any(not isinstance(k, int) or isinstance(k, bool) or k < 1 or k > 20 for k in cleaned):
        raise EvaluationError("K must be an integer from 1 to 20.")
    return cleaned
