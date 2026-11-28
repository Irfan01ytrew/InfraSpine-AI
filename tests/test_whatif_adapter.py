"""Tests for the what-if twin snapshot adapter (Step AL slice 2).

Pure functions over synthetic snapshots/projections; no database rows
touched. Run with ``--noconftest`` in a minimal venv like the slice 1
tests, or under the full suite (this file imports only stdlib +
``app.whatif``).
"""

import asyncio
import importlib.util

import pytest

from app.whatif import adapter
from app.whatif.simulator import build_scenario, simulate

NOW = "2026-11-28T00:00:00+00:00"


def twin_snapshot():
    """A twin-shaped snapshot in Step S build_snapshot output shape."""
    return {
        "entities": [
            {"id": "erp", "kind": "system", "name": "ERP",
             "attributes": {"restorable_from_backup": True, "source": "company_graph"}},
            {"id": "mgr", "kind": "person", "name": "Finance Manager",
             "attributes": {"skills": ["approvals"], "source": "company_graph"}},
            {"id": "payroll", "kind": "process", "name": "Payroll",
             "attributes": {"source": "company_graph"}},
            {"id": "fin-batch:b1", "kind": "invoice_batch", "name": "March batch",
             "attributes": {"amount": 1500.0, "currency": "PKR", "source": "finance"}},
            {"id": "bc:node-9", "kind": "system", "name": "Backup server",
             "attributes": {"source": "continuity"}},
            {"id": "wf-run:r1", "kind": "workflow", "name": "Payroll run",
             "attributes": {"status": "running", "source": "workflow"}},
        ],
        "relationships": [
            {"id": "e1", "source_id": "payroll", "target_id": "erp",
             "kind": "depends_on", "confidence": 1.0, "evidence": []},
            {"id": "e2", "source_id": "mgr", "target_id": "payroll",
             "kind": "owns", "confidence": 1.0, "evidence": []},
            {"id": "e3", "source_id": "fin-batch:b1", "target_id": "payroll",
             "kind": "depends_on", "confidence": 1.0, "evidence": []},
            {"id": "e4", "source_id": "wf-run:r1", "target_id": "fin-batch:b1",
             "kind": "depends_on", "confidence": 1.0, "evidence": []},
        ],
        "as_of": NOW,
        "truncated": False,
        "fingerprint": "deadbeef",
        "sources": [{"name": "company_graph", "kind": "company_graph",
                     "status": "fresh", "last_synced_at": NOW, "stale_after_days": None}],
        "stale_sources": [],
    }


def projection_record(record_id, kind, name, *, record_type="entity", **extra):
    record = {"id": record_id, "record_type": record_type, "external_key": name,
              "kind": kind, "name": name, "visibility": "tenant",
              "attributes": {}, "confidence": 1.0, "evidence": []}
    record.update(extra)
    return record


def graph_projection():
    """A synthetic authorized company-graph projection."""
    return {
        "entities": [
            projection_record("erp", "system", "ERP",
                              attributes={"restorable_from_backup": True}),
            projection_record("mgr", "employee", "Finance Manager",
                              attributes={"skills": ["approvals"]}),
            projection_record("payroll", "process", "Payroll"),
            projection_record("it", "department", "IT"),
            projection_record("batch1", "invoice_batch", "March batch",
                              attributes={"amount": 1500.0, "currency": "PKR"}),
        ],
        "relationships": [
            projection_record("e1", "depends_on", "x", record_type="relationship",
                              source_id="payroll", target_id="erp"),
            projection_record("e2", "owns", "x", record_type="relationship",
                              source_id="mgr", target_id="payroll"),
            projection_record("e3", "member_of", "x", record_type="relationship",
                              source_id="mgr", target_id="it"),
            projection_record("e4", "depends_on", "x", record_type="relationship",
                              source_id="batch1", target_id="payroll"),
        ],
        "as_of": NOW,
        "truncated": False,
        "fingerprint": "cafef00d",
    }


# ---------------------------------------------------------------- adapt_snapshot

def test_adapt_passes_twin_snapshot_through_and_simulates():
    snapshot = adapter.adapt_snapshot(twin_snapshot())
    assert snapshot["fingerprint"] == "deadbeef"
    assert snapshot["as_of"] == NOW
    assert snapshot["sources"][0]["name"] == "company_graph"
    assert snapshot["stale_sources"] == []
    assert snapshot["truncated"] is False
    result = simulate(snapshot, build_scenario("server_down", ["erp"]))
    assert {a["entity"]["id"] for a in result["affected"]} == {
        "erp", "payroll", "fin-batch:b1", "wf-run:r1"}
    assert result["money_at_risk"]["totals"] == {"PKR": 1500.0}


def test_adapt_rejects_non_dict():
    with pytest.raises(ValueError, match="must be a dict"):
        adapter.adapt_snapshot(["not", "a", "dict"])


def test_adapt_rejects_missing_lists():
    with pytest.raises(ValueError):
        adapter.adapt_snapshot({"entities": []})


def test_adapt_rejects_duplicate_ids():
    snap = twin_snapshot()
    snap["entities"].append(dict(snap["entities"][0]))
    with pytest.raises(ValueError, match="Duplicate entity id"):
        adapter.adapt_snapshot(snap)


def test_adapt_rejects_dangling_edge():
    snap = twin_snapshot()
    snap["relationships"].append({"id": "bad", "source_id": "payroll",
                                  "target_id": "ghost", "kind": "depends_on",
                                  "confidence": 1.0, "evidence": []})
    with pytest.raises(ValueError, match="unknown target_id"):
        adapter.adapt_snapshot(snap)


def test_adapt_rejects_negative_amount():
    snap = twin_snapshot()
    snap["entities"][3]["attributes"]["amount"] = -5
    with pytest.raises(ValueError, match="must be >= 0"):
        adapter.adapt_snapshot(snap)


def test_adapt_rejects_money_without_amount():
    snap = twin_snapshot()
    del snap["entities"][3]["attributes"]["amount"]
    with pytest.raises(ValueError, match="needs attributes.amount"):
        adapter.adapt_snapshot(snap)


def test_adapt_deep_copies():
    original = twin_snapshot()
    adapted = adapter.adapt_snapshot(original)
    adapted["entities"][0]["attributes"]["tampered"] = True
    adapted["relationships"].append({"id": "x", "source_id": "erp",
                                     "target_id": "payroll", "kind": "depends_on",
                                     "confidence": 1.0, "evidence": []})
    assert "tampered" not in original["entities"][0]["attributes"]
    assert len(original["relationships"]) == 4


# --------------------------------------------------- snapshot_from_projection

def test_projection_maps_kinds_and_attributes():
    snapshot = adapter.snapshot_from_projection(graph_projection())
    by_id = {e["id"]: e for e in snapshot["entities"]}
    assert by_id["mgr"]["kind"] == "person"  # employee -> person
    assert by_id["it"]["kind"] == "team"  # department -> team
    assert by_id["erp"]["kind"] == "system"
    assert by_id["erp"]["attributes"]["source"] == "company_graph"
    assert by_id["erp"]["attributes"]["external_key"] == "ERP"
    assert snapshot["fingerprint"] == "cafef00d"
    assert snapshot["as_of"] == NOW


def test_projection_snapshot_simulates():
    snapshot = adapter.snapshot_from_projection(graph_projection())
    result = simulate(snapshot, build_scenario("server_down", ["erp"]))
    assert {a["entity"]["id"] for a in result["affected"]} == {
        "erp", "payroll", "batch1"}
    assert result["money_at_risk"]["totals"] == {"PKR": 1500.0}
    assert result["money_at_risk"]["batch_count"] == 1


def test_projection_drops_dangling_relationship():
    projection = graph_projection()
    projection["relationships"].append(
        projection_record("ghost-edge", "depends_on", "x", record_type="relationship",
                          source_id="payroll", target_id="ghost"))
    snapshot = adapter.snapshot_from_projection(projection)
    assert {e["id"] for e in snapshot["relationships"]} == {"e1", "e2", "e3", "e4"}


def test_projection_duplicate_entity_raises():
    projection = graph_projection()
    projection["entities"].append(projection["entities"][0])
    with pytest.raises(ValueError, match="Duplicate entity id"):
        adapter.snapshot_from_projection(projection)


def test_projection_money_without_amount_raises():
    projection = graph_projection()
    projection["entities"][4]["attributes"] = {}
    with pytest.raises(ValueError, match="needs attributes.amount"):
        adapter.snapshot_from_projection(projection)


def test_projection_ignores_non_entity_records():
    projection = graph_projection()
    projection["entities"].append({"id": "noise", "record_type": "change",
                                   "kind": "system", "name": "Noise"})
    snapshot = adapter.snapshot_from_projection(projection)
    assert "noise" not in {e["id"] for e in snapshot["entities"]}


def test_projection_order_is_deterministic():
    first = adapter.snapshot_from_projection(graph_projection())
    shuffled = graph_projection()
    shuffled["entities"] = list(reversed(shuffled["entities"]))
    shuffled["relationships"] = list(reversed(shuffled["relationships"]))
    second = adapter.snapshot_from_projection(shuffled)
    assert first == second


def test_projection_rejects_non_dict():
    with pytest.raises(ValueError, match="must be a dict"):
        adapter.snapshot_from_projection(None)


# --------------------------------------------------------- snapshot_summary

def test_summary_counts_money_and_freshness():
    summary = adapter.snapshot_summary(twin_snapshot())
    assert summary["entity_count"] == 6
    assert summary["relationship_count"] == 4
    assert summary["by_kind"] == {"system": 2, "person": 1, "process": 1,
                                  "invoice_batch": 1, "workflow": 1}
    assert summary["money_at_risk"]["totals"] == {"PKR": 1500.0}
    assert summary["stale_sources"] == []
    assert summary["fingerprint"] == "deadbeef"
    assert summary["as_of"] == NOW


def test_summary_rejects_invalid_snapshot():
    with pytest.raises(ValueError):
        adapter.snapshot_summary({"entities": "nope", "relationships": []})


# ------------------------------------------- build_simulation_snapshot (lazy)

def test_build_simulation_snapshot_without_twin_builder_raises():
    try:
        present = importlib.util.find_spec("app.digital_twin.snapshot") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        present = False
    if present:
        pytest.skip("twin builder is present on this branch; lazy path untestable")
    with pytest.raises(RuntimeError, match="app.digital_twin.snapshot"):
        asyncio.run(adapter.build_simulation_snapshot(None, None))
