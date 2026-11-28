"""Step AI slice 4 tests: answer-time citation freshness surfacing.

Pure summary tests run without fixtures; the `answer_question` wiring tests use
the offline `MockLLMClient` (no network, no DB). Freshness is advisory: a
warning labels the sources, the answer is still given from them — never a
guess, never a silent downgrade.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from app.rag.answer import answer_question
from app.rag.freshness import (
    DocumentMeta,
    summarize_answer_freshness,
)
from app.rag.retrieval import RetrievedChunk
from tests.fake_llm import MockLLMClient

NOW = datetime.now(timezone.utc)


def _meta(doc_id: uuid.UUID, filename: str, *, age_days: int = 10, **overrides) -> DocumentMeta:
    created = NOW - timedelta(days=age_days)
    values = {
        "document_id": doc_id,
        "filename": filename,
        "created_at": created,
        "updated_at": created,
        "indexed_at": created,
        "version": 1,
        "indexed_version": 1,
        "status": "ready",
    }
    values.update(overrides)
    return DocumentMeta(**values)


def _chunk(doc_id: uuid.UUID, filename: str, content: str, chunk_id: int = 1) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=doc_id,
        filename=filename,
        page_number=1,
        chunk_index=0,
        content=content,
        score=0.9,
    )


# ---------------------------------------------------------------- pure summary


def test_fresh_citations_have_no_warning():
    doc = uuid.uuid4()
    summary = summarize_answer_freshness(
        [(1, _chunk(doc, "Leave Policy.pdf", "x"))],
        {doc: _meta(doc, "Leave Policy.pdf", age_days=10)},
    )
    assert summary.warning is None
    assert len(summary.citations) == 1
    assert summary.citations[0].status == "fresh"
    assert summary.citations[0].source_number == 1
    assert "Leave Policy.pdf" in summary.citations[0].note


def test_one_stale_among_fresh_is_labelled_but_not_warned():
    fresh_doc, stale_doc = uuid.uuid4(), uuid.uuid4()
    summary = summarize_answer_freshness(
        [
            (1, _chunk(fresh_doc, "Fresh.pdf", "x")),
            (2, _chunk(stale_doc, "Old.pdf", "y")),
        ],
        {
            fresh_doc: _meta(fresh_doc, "Fresh.pdf", age_days=10),
            stale_doc: _meta(stale_doc, "Old.pdf", age_days=400),
        },
    )
    assert summary.warning is None
    assert [c.status for c in summary.citations] == ["fresh", "stale"]


def test_all_stale_citations_warn():
    doc = uuid.uuid4()
    summary = summarize_answer_freshness(
        [(1, _chunk(doc, "Old Policy.pdf", "x"))],
        {doc: _meta(doc, "Old Policy.pdf", age_days=400)},
    )
    assert summary.warning is not None
    assert "stale" in summary.warning.lower()
    assert summary.citations[0].status == "stale"


def test_expired_citation_warns_and_names_the_source():
    doc = uuid.uuid4()
    summary = summarize_answer_freshness(
        [(1, _chunk(doc, "Expired Policy.pdf", "x"))],
        {
            doc: _meta(
                doc,
                "Expired Policy.pdf",
                age_days=10,
                expires_at=NOW - timedelta(days=1),
            )
        },
    )
    assert summary.warning is not None
    assert "Expired Policy.pdf" in summary.warning
    assert "[S1]" in summary.warning
    assert summary.citations[0].status == "expired"


def test_superseded_citation_warns_even_when_others_are_fresh():
    fresh_doc, old_doc = uuid.uuid4(), uuid.uuid4()
    summary = summarize_answer_freshness(
        [
            (1, _chunk(fresh_doc, "Current.pdf", "x")),
            (2, _chunk(old_doc, "Old.pdf", "y")),
        ],
        {
            fresh_doc: _meta(fresh_doc, "Current.pdf", age_days=10),
            old_doc: _meta(old_doc, "Old.pdf", age_days=5, superseded=True),
        },
    )
    assert summary.warning is not None
    assert "Old.pdf" in summary.warning
    assert summary.citations[1].status == "superseded"


def test_citation_without_metadata_is_skipped_not_failed():
    doc, unknown = uuid.uuid4(), uuid.uuid4()
    summary = summarize_answer_freshness(
        [(1, _chunk(doc, "Known.pdf", "x")), (2, _chunk(unknown, "Unknown.pdf", "y"))],
        {doc: _meta(doc, "Known.pdf", age_days=10)},
    )
    assert summary.warning is None
    assert [c.source_number for c in summary.citations] == [1]


def test_empty_citations_have_no_warning():
    assert summarize_answer_freshness([], {}).warning is None
    assert summarize_answer_freshness([], {}).citations == ()


# ------------------------------------------------------- answer_question wiring


def _run_answer(question, chunks, metas):
    return asyncio.run(
        answer_question(
            MockLLMClient(),
            question,
            chunks,
            general_answers=False,
            freshness_metas=metas,
        )
    )


def test_answer_from_stale_source_carries_warning_but_still_answers():
    doc = uuid.uuid4()
    chunk = _chunk(
        doc,
        "Leave Policy.pdf",
        "The leave policy allows 20 days of annual leave per year.",
    )
    answer = _run_answer(
        "How many days of annual leave does the policy allow?",
        [chunk],
        {doc: _meta(doc, "Leave Policy.pdf", age_days=400)},
    )
    assert answer.insufficient_evidence is False
    assert "20" in answer.answer
    assert answer.freshness_warning is not None
    assert "stale" in answer.freshness_warning.lower()
    assert len(answer.citation_freshness) == 1
    assert answer.citation_freshness[0].status == "stale"
    assert answer.citation_freshness[0].source_number == 1


def test_answer_from_fresh_source_has_no_warning():
    doc = uuid.uuid4()
    chunk = _chunk(
        doc,
        "Leave Policy.pdf",
        "The leave policy allows 20 days of annual leave per year.",
    )
    answer = _run_answer(
        "How many days of annual leave does the policy allow?",
        [chunk],
        {doc: _meta(doc, "Leave Policy.pdf", age_days=10)},
    )
    assert answer.insufficient_evidence is False
    assert answer.freshness_warning is None
    assert answer.citation_freshness[0].status == "fresh"


def test_answer_without_freshness_metadata_is_unchanged():
    doc = uuid.uuid4()
    chunk = _chunk(
        doc,
        "Leave Policy.pdf",
        "The leave policy allows 20 days of annual leave per year.",
    )
    answer = asyncio.run(
        answer_question(
            MockLLMClient(),
            "How many days of annual leave does the policy allow?",
            [chunk],
            general_answers=False,
        )
    )
    assert answer.insufficient_evidence is False
    assert answer.freshness_warning is None
    assert answer.citation_freshness == ()
