"""Ranking metrics for one retrieved list.

Relevance is binary: a chunk matches the expected document (when given) and contains the
expected passage (when given). Recall, MRR and nDCG use that label. Citation precision is
the fraction of returned chunks that match, which is the candidate set chat would cite.
"""

from __future__ import annotations

import math
import re
import unicodedata

from app.rag.evaluation.dataset import EvalCase


def normalize(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()
    return collapsed.casefold()


def passage_matches(content: str, passage: str) -> bool:
    """True when the passage appears in the chunk, or the chunk is a slice of a longer passage."""
    left = normalize(content)
    right = normalize(passage)
    if not left or not right:
        return False
    return right in left or left in right


def chunk_is_relevant(filename: str, content: str, case: EvalCase) -> bool:
    if case.expected_document and normalize(filename) != normalize(case.expected_document):
        return False
    if case.expected_passage and not passage_matches(content, case.expected_passage):
        return False
    return bool(case.expected_document or case.expected_passage)


def recall_at_k(ranked_relevant: list[bool], k: int, relevant_total: int) -> float:
    if k <= 0 or relevant_total <= 0:
        return 0.0
    found = sum(1 for relevant in ranked_relevant[:k] if relevant)
    return found / relevant_total


def reciprocal_rank(ranked_relevant: list[bool]) -> float:
    for index, relevant in enumerate(ranked_relevant, start=1):
        if relevant:
            return 1.0 / index
    return 0.0


def ndcg_at_k(ranked_relevant: list[bool], k: int, relevant_total: int) -> float:
    """Binary nDCG. The ideal list places every gold chunk at the top ranks."""
    if k <= 0 or relevant_total <= 0:
        return 0.0
    dcg = sum(1.0 / math.log2(index + 1) for index, relevant in enumerate(ranked_relevant[:k], start=1) if relevant)
    ideal = min(k, relevant_total)
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal + 1))
    return dcg / idcg if idcg else 0.0


def citation_precision(ranked_relevant: list[bool]) -> float:
    """Fraction of returned chunks that match the gold passage. An empty list cites nothing."""
    if not ranked_relevant:
        return 0.0
    return sum(1 for relevant in ranked_relevant if relevant) / len(ranked_relevant)


def first_relevant_rank(ranked_relevant: list[bool]) -> int | None:
    for index, relevant in enumerate(ranked_relevant, start=1):
        if relevant:
            return index
    return None


def mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(percent / 100 * len(ordered)) - 1
    return ordered[max(0, min(rank, len(ordered) - 1))]
