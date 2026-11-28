"""Twin version helpers (Step K v2 slice 2): pure tests, no database.

Run with ``--noconftest`` (no database needed):

    .venv/bin/python -m pytest tests/test_digital_twin_versions_pure.py --noconftest -q
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.digital_twin import drift, versions


def _snapshot(**overrides):
    base = {
        "entities": [
            {"id": "e-ayesha", "kind": "employee", "name": "Ayesha",
             "attributes": {"department": "Finance"}},
            {"id": "e-erp", "kind": "system", "name": "ERP", "attributes": {}},
            {"id": "e-batch", "kind": "invoice_batch", "name": "Batch 1",
             "attributes": {"amount": 100000, "currency": "PKR"}},
        ],
        "relationships": [
            {"id": "r-1", "source_id": "e-ayesha", "target_id": "e-erp",
             "kind": "owns", "confidence": 1.0, "evidence": []},
        ],
        "as_of": "2026-11-28T00:00:00Z",
        "stale_sources": [],
    }
    base.update(overrides)
    return base


def test_validate_snapshot_accepts_valid_snapshot():
    assert drift.validate_snapshot(_snapshot()) is None


def test_validate_snapshot_rejects_bad_snapshots():
    bad = _snapshot(entities=[{"id": "x", "kind": "employee", "name": "X", "attributes": {}},
                              {"id": "x", "kind": "employee", "name": "Y", "attributes": {}}])
    with pytest.raises(ValueError, match="Duplicate entity id"):
        drift.validate_snapshot(bad)
    bad = _snapshot(relationships=[{"id": "r-9", "source_id": "e-ayesha", "target_id": "ghost",
                                    "kind": "owns", "confidence": 1.0, "evidence": []}])
    with pytest.raises(ValueError, match="dangling target"):
        drift.validate_snapshot(bad)
    bad = _snapshot(entities=[{"id": "e-batch", "kind": "invoice_batch", "name": "B",
                               "attributes": {"amount": "lots", "currency": "PKR"}}])
    with pytest.raises(ValueError, match="Non-numeric money amount"):
        drift.validate_snapshot(bad)
    with pytest.raises(ValueError, match="must be a dict"):
        drift.validate_snapshot(["not", "a", "dict"])


def test_content_fingerprint_ignores_stored_fingerprint():
    snapshot = _snapshot(fingerprint="0" * 64)
    computed = drift.snapshot_fingerprint({k: v for k, v in snapshot.items() if k != "fingerprint"})
    assert versions._content_fingerprint(snapshot) == computed
    assert versions._content_fingerprint(snapshot) != "0" * 64


def test_parse_as_of():
    now = datetime(2026, 11, 28, 7, 0, tzinfo=UTC)
    assert versions._parse_as_of(None, now=now) == now
    assert versions._parse_as_of("2026-11-27T10:00:00Z", now=now) == datetime(2026, 11, 27, 10, 0, tzinfo=UTC)
    assert versions._parse_as_of("2026-11-27T10:00:00+05:00", now=now).utcoffset().total_seconds() == 5 * 3600
    naive = datetime(2026, 11, 27, 10, 0)
    assert versions._parse_as_of(naive, now=now).tzinfo == UTC
    assert versions._parse_as_of(datetime(2026, 11, 27, 10, 0, tzinfo=UTC), now=now).tzinfo == UTC
    with pytest.raises(ValueError, match="Invalid as_of"):
        versions._parse_as_of("not-a-date", now=now)
    with pytest.raises(ValueError, match="Invalid as_of"):
        versions._parse_as_of(42, now=now)


def test_normalize_source_freshness():
    assert versions._normalize_source_freshness(None) == []
    assert versions._normalize_source_freshness([{"name": "company_graph", "status": "fresh"}]) == [
        {"name": "company_graph", "status": "fresh"}
    ]
    with pytest.raises(ValueError, match="must be a list"):
        versions._normalize_source_freshness("company_graph")
    with pytest.raises(ValueError, match="non-empty string 'name'"):
        versions._normalize_source_freshness([{"status": "fresh"}])
    with pytest.raises(ValueError, match="must be dicts"):
        versions._normalize_source_freshness(["company_graph"])


def _drift_report():
    return {
        "fingerprint_old": "a" * 64,
        "fingerprint_new": "b" * 64,
        "drift_score": 12,
        "trust": "trustworthy",
        "money": {"old": {"totals": {"PKR": 100000.0}, "batch_count": 1, "payment_count": 0},
                  "new": {"totals": {"PKR": 120000.0}, "batch_count": 1, "payment_count": 0},
                  "delta": {"totals": {"PKR": 20000.0}, "batch_count": 0, "payment_count": 0}},
        "counts": {"entities_old": 3, "entities_new": 4,
                   "relationships_old": 1, "relationships_new": 1},
        "stale_sources": [],
    }


def test_redact_money_for_viewer():
    report = _drift_report()
    redacted = versions.redact_money_for_viewer(report)
    assert redacted["money"] is None
    assert redacted["money_redacted"] is True
    assert redacted["drift_score"] == 12
    assert redacted["trust"] == "trustworthy"
    # The original report is untouched.
    assert report["money"]["delta"]["totals"] == {"PKR": 20000.0}
    assert "money_redacted" not in report
    assert versions.redact_money_for_viewer(None) is None


def _row(**overrides):
    base = {
        "id": uuid4(),
        "seq": 7,
        "tenant_id": uuid4(),
        "fingerprint": "c" * 64,
        "as_of": datetime(2026, 11, 28, 7, 0, tzinfo=UTC),
        "created_at": datetime(2026, 11, 28, 7, 0, 5, tzinfo=UTC),
        "snapshot": _snapshot(),
        "drift_report": None,
        "source_freshness": [{"name": "company_graph", "status": "fresh"}],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_version_summary_first_version():
    summary = versions.version_summary(_row())
    assert summary["drift_score"] is None
    assert summary["trust"] is None
    assert summary["is_first_version"] is True
    assert summary["seq"] == 7
    assert summary["entity_count"] == 3
    assert summary["relationship_count"] == 1
    assert summary["stale_sources"] == []
    assert summary["source_freshness"] == [{"name": "company_graph", "status": "fresh"}]
    assert "snapshot" not in summary
    assert "money" not in summary


def test_version_summary_with_drift():
    summary = versions.version_summary(_row(drift_report=_drift_report()))
    assert summary["drift_score"] == 12
    assert summary["trust"] == "trustworthy"
    assert summary["is_first_version"] is False
    assert summary["entity_count"] == 4
    assert summary["relationship_count"] == 1
