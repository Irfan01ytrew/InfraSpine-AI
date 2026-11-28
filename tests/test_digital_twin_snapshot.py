"""Twin snapshot builder (slice 2): read-only adapter tests.

Pure-function tests over synthetic, already-authorized inputs. No
database rows are created by these tests themselves; the repo conftest
session fixture still migrates the dedicated *_test database, as for
every test module. The async collector is tested with a stub session
(no real database) plus a stubbed company-graph projection.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import asyncio

import pytest

from app.digital_twin.snapshot import (
    build_snapshot,
    collect_snapshot_inputs,
    source_freshness,
)


def graph_entity(eid, kind, name, **extra):
    record = {"id": eid, "record_type": "entity", "external_key": f"ext:{eid}",
              "kind": kind, "name": name, "source_id": None, "target_id": None,
              "department_id": None, "visibility": "tenant", "readers": [],
              "attributes": {}, "evidence": [], "confidence": 1.0,
              "inferred": False, "retired": False}
    record.update(extra)
    return record


def graph_edge(eid, source, target, kind, confidence=1.0):
    return {"id": eid, "record_type": "relationship", "external_key": f"ext:{eid}",
            "kind": kind, "name": kind, "source_id": source, "target_id": target,
            "department_id": None, "visibility": "tenant", "readers": [],
            "attributes": {}, "evidence": [], "confidence": confidence,
            "inferred": False, "retired": False}


def by_id(snapshot, eid):
    return next(e for e in snapshot["entities"] if e["id"] == eid)


def edge_kinds(snapshot):
    return {(e["source_id"], e["target_id"], e["kind"]) for e in snapshot["relationships"]}


# --- source_freshness -------------------------------------------------------

def test_freshness_never_synced_is_unknown():
    info = source_freshness(None, 90)
    assert info == {"status": "unknown", "last_synced_at": None, "stale_after_days": 90}


def test_freshness_recent_sync_is_fresh():
    now = datetime.now(UTC)
    info = source_freshness(now - timedelta(days=1), 90, now=now)
    assert info["status"] == "fresh"
    assert info["last_synced_at"] == (now - timedelta(days=1)).isoformat()


def test_freshness_old_sync_is_stale():
    now = datetime.now(UTC)
    assert source_freshness(now - timedelta(days=91), 90, now=now)["status"] == "stale"
    assert source_freshness(now - timedelta(days=90), 90, now=now)["status"] == "stale"
    assert source_freshness(now - timedelta(days=89), 90, now=now)["status"] == "fresh"


def test_freshness_iso_string_and_no_limit():
    info = source_freshness("2026-11-28T00:00:00+00:00", None)
    assert info["status"] == "fresh"
    assert info["last_synced_at"] == "2026-11-28T00:00:00+00:00"


def test_freshness_rejects_bad_inputs():
    with pytest.raises(ValueError):
        source_freshness("not-a-date", 90)
    with pytest.raises(ValueError):
        source_freshness(datetime.now(UTC), "90")
    with pytest.raises(ValueError):
        source_freshness(datetime.now(UTC), True)


# --- build_snapshot: company graph ------------------------------------------

def test_graph_entities_mapped_person_and_team():
    snap = build_snapshot(
        graph_entities=[graph_entity("g-1", "employee", "Ayesha"),
                        graph_entity("g-2", "department", "Finance")],
        graph_relationships=[graph_edge("e-1", "g-1", "g-2", "member_of")],
    )
    assert by_id(snap, "g-1")["kind"] == "person"
    assert by_id(snap, "g-2")["kind"] == "team"
    assert ("g-1", "g-2", "member_of") in edge_kinds(snap)
    assert snap["stale_sources"] == []
    assert snap["truncated"] is False
    assert len(snap["fingerprint"]) == 64


def test_graph_relationship_with_hidden_endpoint_is_dropped():
    snap = build_snapshot(
        graph_entities=[graph_entity("g-1", "employee", "Ayesha")],
        graph_relationships=[graph_edge("e-1", "g-1", "g-hidden", "owns")],
    )
    assert snap["relationships"] == []


def test_duplicate_entity_id_raises():
    with pytest.raises(ValueError, match="Duplicate entity id"):
        build_snapshot(graph_entities=[graph_entity("g-1", "employee", "A"),
                                       graph_entity("g-1", "employee", "B")])


# --- build_snapshot: continuity merge ---------------------------------------

def test_continuity_node_folds_into_graph_entity_by_external_key():
    snap = build_snapshot(
        graph_entities=[graph_entity("g-1", "employee", "Ayesha")],
        continuity_nodes=[{"id": "bc-1", "kind": "employee", "name": "Ayesha (bc)",
                           "external_key": "ext:g-1",
                           "data": {"bia_score": 9, "skills": ["approvals"]}}],
    )
    assert [e["id"] for e in snap["entities"]] == ["g-1"]
    merged = by_id(snap, "g-1")
    assert merged["attributes"]["bia_score"] == 9
    assert merged["attributes"]["source"] == "company_graph"  # graph wins


def test_continuity_node_without_match_gets_bc_prefix():
    snap = build_snapshot(
        continuity_nodes=[{"id": "bc-9", "kind": "system", "name": "ERP", "data": {}}],
        continuity_links=[],
    )
    assert by_id(snap, "bc:bc-9")["kind"] == "system"
    assert by_id(snap, "bc:bc-9")["attributes"]["source"] == "continuity"


def test_continuity_link_uses_folded_ids_and_drops_unknown_endpoints():
    snap = build_snapshot(
        graph_entities=[graph_entity("g-1", "employee", "Ayesha")],
        continuity_nodes=[{"id": "bc-1", "kind": "employee", "name": "Ayesha",
                           "external_key": "ext:g-1", "data": {}},
                          {"id": "bc-2", "kind": "system", "name": "ERP", "data": {}}],
        continuity_links=[{"id": "l-1", "source_id": "bc-1", "target_id": "bc-2",
                           "kind": "operates", "data": {}},
                          {"id": "l-2", "source_id": "bc-2", "target_id": "bc-nope",
                           "kind": "depends_on", "data": {}}],
    )
    assert ("g-1", "bc:bc-2", "operates") in edge_kinds(snap)
    assert all(e["target_id"] != "bc:bc-nope" for e in snap["relationships"])


# --- build_snapshot: finance + workflow -------------------------------------

def test_finance_batch_payment_and_run_edges():
    snap = build_snapshot(
        graph_entities=[graph_entity("g-1", "employee", "Ayesha")],
        invoice_batches=[{"id": "b-1", "name": "March", "status": "awaiting_approval",
                          "currency": "PKR", "total_amount": Decimal("1500.00"),
                          "approver_ids": ["g-1"], "wf_run_id": "r-1"}],
        payments=[{"id": "p-1", "batch_id": "b-1", "amount": 1500,
                   "currency": "PKR", "status": "approved", "approver_id": "g-1"}],
        workflow_runs=[{"id": "r-1", "status": "running", "phase": "execute",
                        "origin": "system", "batch_id": "b-1"}],
    )
    batch = by_id(snap, "fin-batch:b-1")
    assert batch["kind"] == "invoice_batch"
    assert batch["attributes"] == {"amount": 1500.0, "currency": "PKR",
                                   "status": "awaiting_approval", "source": "finance"}
    assert by_id(snap, "fin-pay:p-1")["attributes"]["amount"] == 1500.0
    kinds = edge_kinds(snap)
    assert ("g-1", "fin-batch:b-1", "approves") in kinds
    assert ("g-1", "fin-pay:p-1", "approves") in kinds
    assert ("fin-pay:p-1", "fin-batch:b-1", "depends_on") in kinds
    assert ("wf-run:r-1", "fin-batch:b-1", "depends_on") in kinds
    assert by_id(snap, "wf-run:r-1")["attributes"]["status"] == "running"


def test_finance_negative_or_missing_amount_raises():
    with pytest.raises(ValueError):
        build_snapshot(invoice_batches=[{"id": "b-1", "name": "x", "status": "open",
                                         "currency": "PKR", "total_amount": -5}])
    with pytest.raises(ValueError):
        build_snapshot(payments=[{"id": "p-1", "batch_id": "b-1", "amount": "lots",
                                  "currency": "PKR", "status": "approved"}])


def test_batch_without_run_row_drops_dangling_run_edge():
    snap = build_snapshot(
        invoice_batches=[{"id": "b-1", "name": "March", "status": "open",
                          "currency": "PKR", "total_amount": 100, "wf_run_id": "r-missing"}])
    assert all(e["source_id"] != "wf-run:r-missing" for e in snap["relationships"])


def test_snapshot_is_consumable_by_slice_1_simulation():
    from app.digital_twin.impact import simulate_absence
    snap = build_snapshot(
        graph_entities=[graph_entity("g-1", "employee", "Ayesha"),
                        graph_entity("g-2", "process", "Payroll")],
        graph_relationships=[graph_edge("e-1", "g-1", "g-2", "owns")],
        invoice_batches=[{"id": "b-1", "name": "March", "status": "awaiting_approval",
                          "currency": "PKR", "total_amount": 2500,
                          "approver_ids": ["g-1"]}],
    )
    result = simulate_absence(snap, ["g-1"])
    affected = [entry["entity"]["id"] for entry in result["affected"]]
    assert "g-2" in affected and "fin-batch:b-1" in affected
    assert result["money_at_risk"]["totals"] == {"PKR": 2500.0}


# --- snapshot metadata ------------------------------------------------------

def test_sources_freshness_and_fingerprint_determinism():
    now = datetime.now(UTC)
    kwargs = dict(
        graph_entities=[graph_entity("g-1", "employee", "Ayesha")],
        sources=[{"name": "company_graph", "kind": "company_graph",
                  "last_synced_at": now - timedelta(days=1), "stale_after_days": 90},
                 {"name": "old-erp", "kind": "connector",
                  "last_synced_at": now - timedelta(days=100), "stale_after_days": 30},
                 {"name": "never", "kind": "connector",
                  "last_synced_at": None, "stale_after_days": 30}])
    first = build_snapshot(**kwargs)
    second = build_snapshot(**kwargs)
    assert first["fingerprint"] == second["fingerprint"]
    assert first["stale_sources"] == ["old-erp"]
    assert [s["status"] for s in first["sources"]] == ["fresh", "stale", "unknown"]


def test_truncated_flag_passes_through():
    snap = build_snapshot(graph_entities=[], truncated=True)
    assert snap["truncated"] is True
    assert snap["entities"] == [] and snap["relationships"] == []


# --- collect_snapshot_inputs (stub session, no real DB) ----------------------

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeSession:
    """Returns canned rows in call order: nodes, links, invoices, payments, runs."""

    def __init__(self, rows_in_order):
        self._queue = list(rows_in_order)

    async def scalars(self, _statement):
        return _FakeResult(self._queue.pop(0))


class _Row:
    def __init__(self, **fields):
        self.__dict__.update(fields)


def test_collect_maps_rows_to_plain_inputs(monkeypatch):
    return asyncio.run(_test_collect_maps_rows_to_plain_inputs(monkeypatch))


async def _test_collect_maps_rows_to_plain_inputs(monkeypatch):
    tenant_id, group_id = uuid4(), uuid4()
    batch_id, run_id = uuid4(), uuid4()
    node_id, link_id = uuid4(), uuid4()
    node2_id = uuid4()
    monkeypatch.setattr(
        "app.digital_twin.snapshot.graph_service.projection",
        lambda db, scope, limit=500: _fake_projection())
    db = _FakeSession([
        [_Row(id=node_id, kind="system", name="ERP", external_key="ext:erp", data={"critical": True}),
         _Row(id=node2_id, kind="process", name="Payroll", external_key="ext:pay", data={})],
        [_Row(id=link_id, source_id=node_id, target_id=node2_id, kind="depends_on", data={})],
        [_Row(id=batch_id, group_id=group_id, name="March", status="verifying",
              currency="PKR", wf_run_id=run_id)],
        [_Row(batch_id=batch_id, amount=Decimal("700")), _Row(batch_id=batch_id, amount=Decimal("300"))],
        [_Row(id=uuid4(), batch_id=batch_id, amount=Decimal("1000"), currency="PKR", status="approved")],
        [_Row(id=run_id, status="running", phase="execute", origin="system")],
    ])
    inputs = await collect_snapshot_inputs(
        db, object(), continuity_tenant_id=tenant_id, group_id=group_id)
    assert inputs["graph_entities"][0]["kind"] == "employee"
    assert inputs["continuity_nodes"][0]["name"] == "ERP"
    assert inputs["continuity_links"][0]["kind"] == "depends_on"
    # Batch total is the sum of its invoice amounts (700 + 300).
    assert inputs["invoice_batches"][0]["total_amount"] == Decimal("1000")
    assert inputs["payments"][0]["amount"] == Decimal("1000")
    assert inputs["workflow_runs"][0]["status"] == "running"
    assert inputs["truncated"] is False
    assert {s["name"] for s in inputs["sources"]} == {"company_graph", "continuity", "finance"}


async def _fake_projection():
    return {"entities": [graph_entity("g-1", "employee", "Ayesha")],
            "relationships": [], "truncated": False,
            "as_of": datetime.now(UTC).isoformat()}


def test_collect_without_optional_ids_skips_those_sources(monkeypatch):
    return asyncio.run(_test_collect_without_optional_ids_skips_those_sources(monkeypatch))


async def _test_collect_without_optional_ids_skips_those_sources(monkeypatch):
    monkeypatch.setattr(
        "app.digital_twin.snapshot.graph_service.projection",
        lambda db, scope, limit=500: _fake_projection())
    db = _FakeSession([])
    inputs = await collect_snapshot_inputs(db, object())
    assert inputs["continuity_nodes"] == [] and inputs["invoice_batches"] == []
    assert [s["name"] for s in inputs["sources"]] == ["company_graph"]
