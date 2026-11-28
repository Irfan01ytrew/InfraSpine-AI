"""Step AI slice 1 tests: pure-function document freshness scoring,
contradiction candidates, and citation notes. No DB, no network, no model call.

Note: `tests/test_knowledge_freshness.py` already covers the *knowledge
revision* freshness system (review_due_at, stale endpoint). This file covers
the new *document-level* scorer in `app/rag/freshness.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.rag.freshness import (
    AGING,
    EXPIRED,
    FRESH,
    STALE,
    SUPERSEDED,
    CitedPassage,
    DocumentMeta,
    FreshnessPolicy,
    citation_freshness_note,
    detect_contradiction,
    score_freshness,
)

NOW = datetime(2026, 11, 28, tzinfo=timezone.utc)


def _meta(**overrides):
    base = {
        "document_id": uuid4(),
        "filename": "Leave Policy.pdf",
        "created_at": NOW - timedelta(days=10),
        "updated_at": NOW - timedelta(days=10),
        "indexed_at": NOW - timedelta(days=10),
        "version": 3,
        "indexed_version": 3,
        "status": "ready",
    }
    base.update(overrides)
    return DocumentMeta(**base)


def test_fresh_document_scores_100_with_no_reasons():
    result = score_freshness(_meta(), as_of=NOW)
    assert result.status == FRESH
    assert result.score == 100
    assert result.reasons == ()


def test_recent_review_keeps_old_document_fresh():
    meta = _meta(
        created_at=NOW - timedelta(days=400),
        indexed_at=NOW - timedelta(days=5),
        review_date=NOW + timedelta(days=90),
    )
    result = score_freshness(meta, as_of=NOW)
    assert result.status == FRESH
    assert result.score == 100


def test_overdue_review_is_aging_and_names_the_date():
    meta = _meta(review_date=NOW - timedelta(days=30))
    result = score_freshness(meta, as_of=NOW)
    assert result.status == AGING
    assert result.score == 60
    assert any("2026-10-29" in reason for reason in result.reasons)


def test_review_overdue_beyond_grace_is_stale():
    # Grace = stale_after (365) - review_after (180) = 185 days.
    meta = _meta(review_date=NOW - timedelta(days=200))
    result = score_freshness(meta, as_of=NOW)
    assert result.status == STALE
    assert result.score == 20


def test_never_reviewed_old_document_is_stale():
    meta = _meta(created_at=NOW - timedelta(days=400), indexed_at=NOW - timedelta(days=400))
    result = score_freshness(meta, as_of=NOW)
    assert result.status == STALE
    assert result.score == 20
    assert any("never been reviewed" in reason for reason in result.reasons)


def test_never_reviewed_middle_aged_document_is_aging():
    meta = _meta(created_at=NOW - timedelta(days=200), indexed_at=NOW - timedelta(days=200))
    result = score_freshness(meta, as_of=NOW)
    assert result.status == AGING
    assert result.score == 60


def test_expired_document_scores_zero_even_if_recently_reviewed():
    meta = _meta(expires_at=NOW - timedelta(days=1), review_date=NOW + timedelta(days=30))
    result = score_freshness(meta, as_of=NOW)
    assert result.status == EXPIRED
    assert result.score == 0
    assert any("expired" in reason for reason in result.reasons)


def test_superseded_document_scores_zero_even_if_one_day_old():
    meta = _meta(created_at=NOW - timedelta(days=1), indexed_at=NOW - timedelta(days=1), superseded=True)
    result = score_freshness(meta, as_of=NOW)
    assert result.status == SUPERSEDED
    assert result.score == 0


def test_unindexed_newer_version_penalises_score_and_explains():
    meta = _meta(version=4, indexed_version=3)
    result = score_freshness(meta, as_of=NOW)
    assert result.status == FRESH
    assert result.score == 75  # 100 - 25 unindexed penalty
    assert any("version 3" in reason and "version 4" in reason for reason in result.reasons)


def test_failed_document_scores_zero_and_is_not_searchable():
    meta = _meta(status="failed")
    result = score_freshness(meta, as_of=NOW)
    assert result.score == 0
    assert any("not searchable" in reason for reason in result.reasons)


def test_pending_document_scores_zero_until_indexing_finishes():
    meta = _meta(status="pending", indexed_at=None, indexed_version=None)
    result = score_freshness(meta, as_of=NOW)
    assert result.score == 0
    assert any("not finished" in reason for reason in result.reasons)


def test_policy_override_changes_the_bands():
    policy = FreshnessPolicy(review_after_days=30, stale_after_days=60)
    meta = _meta(created_at=NOW - timedelta(days=45), indexed_at=NOW - timedelta(days=45))
    assert score_freshness(meta, as_of=NOW, policy=policy).status == AGING
    assert score_freshness(meta, as_of=NOW).status == FRESH  # default policy: 45 < 180


def test_invalid_policy_rejected():
    with pytest.raises(ValueError):
        FreshnessPolicy(review_after_days=365, stale_after_days=180)
    with pytest.raises(ValueError):
        FreshnessPolicy(unindexed_penalty=101)


def test_contradicting_policy_statements_flagged_with_both_sources():
    left = CitedPassage(
        uuid4(), "Leave Policy.pdf", "Employees must keep all leave records for five years."
    )
    right = CitedPassage(
        uuid4(), "Records Policy.pdf", "Employees must delete all leave records after one year."
    )
    flag = detect_contradiction(left, right)
    assert flag is not None
    assert flag.needs_human_review is True
    assert flag.left is left
    assert flag.right is right


def test_same_statement_is_not_a_contradiction():
    doc_id = uuid4()
    text = "Employees must keep all leave records for five years."
    assert detect_contradiction(
        CitedPassage(doc_id, "Leave Policy.pdf", text),
        CitedPassage(doc_id, "Leave Policy.pdf", text),
    ) is None


def test_unrelated_statements_are_not_a_contradiction():
    left = CitedPassage(uuid4(), "Leave Policy.pdf", "Annual leave is twenty days per year.")
    right = CitedPassage(uuid4(), "IT Policy.pdf", "Laptops must be returned when employment ends.")
    assert detect_contradiction(left, right) is None


def test_citation_note_shows_filename_version_date_and_status():
    meta = _meta(review_date=NOW - timedelta(days=30))
    result = score_freshness(meta, as_of=NOW)
    note = citation_freshness_note(meta, result)
    assert "Leave Policy.pdf" in note
    assert "v3" in note
    assert "2026-11-18" in note  # indexed date
    assert "aging" in note
    assert "2026-10-29" in note  # review due date from the reason


def test_citation_note_for_unindexed_document():
    meta = _meta(status="pending", indexed_at=None, indexed_version=None)
    result = score_freshness(meta, as_of=NOW)
    note = citation_freshness_note(meta, result)
    assert "not yet indexed" in note
