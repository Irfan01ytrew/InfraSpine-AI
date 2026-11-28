"""Tests for Step AH slice 3: app/resilience/snapshot.py.

Pure-function tests over synthetic rows — no database, no live data, no
secrets. Fake people, teams, systems, suppliers and amounts only.
"""

import pytest

from app.resilience.impact import rank_risks
from app.resilience.snapshot import (
    build_snapshot,
    from_company_graph,
    from_continuity,
    from_finance,
    validate_snapshot,
)
from app.resilience.watch import evaluate_watch


def _cg_entity(row_id, kind, name=None, **extra):
    row = {
        "id": row_id,
        "record_type": "entity",
        "external_key": f"ext-{row_id}",
        "kind": kind,
        "name": name or row_id,
        "attributes": {},
        "confidence": 1.0,
        "retired": False,
        "visibility": "tenant",
    }
    row.update(extra)
    return row


def _cg_rel(row_id, source_id, target_id, kind):
    return {
        "id": row_id,
        "record_type": "relationship",
        "external_key": f"ext-{row_id}",
        "kind": kind,
        "name": row_id,
        "source_id": source_id,
        "target_id": target_id,
        "attributes": {},
        "confidence": 1.0,
        "retired": False,
        "visibility": "tenant",
    }


def _bc_node(node_id, kind, name=None, **data):
    return {
        "id": node_id,
        "external_key": f"ext-{node_id}",
        "kind": kind,
        "name": name or node_id,
        "data": dict(data),
    }


def _bc_link(source_id, target_id, kind):
    return {"source_id": source_id, "target_id": target_id, "kind": kind, "data": {}}


# ---------------------------------------------------------------- company graph

def test_company_graph_entity_mapping():
    part = from_company_graph([
        _cg_entity("u1", "employee", "Ayesha", attributes={"skills": ["approvals"]}),
    ])
    assert len(part["entities"]) == 1
    entity = part["entities"][0]
    assert entity["id"] == "cg:u1"
    assert entity["kind"] == "person"  # employee -> person alias
    assert entity["name"] == "Ayesha"
    assert entity["attributes"]["skills"] == ["approvals"]
    assert entity["attributes"]["external_key"] == "ext-u1"
    assert entity["attributes"]["source_graph"] == "company_graph"
    assert entity["attributes"]["visibility"] == "tenant"
    assert part["skipped"] == []


def test_company_graph_relationship_mapping():
    part = from_company_graph([
        _cg_entity("u1", "person", "Ayesha"),
        _cg_entity("b1", "invoice_batch", "Batch 1"),
        _cg_rel("r1", "u1", "b1", "approves"),
    ])
    assert len(part["relationships"]) == 1
    rel = part["relationships"][0]
    assert rel["source_id"] == "cg:u1"
    assert rel["target_id"] == "cg:b1"
    assert rel["kind"] == "approves"
    assert rel["confidence"] == 1.0


def test_company_graph_retired_and_unknown_rows_skipped():
    part = from_company_graph([
        _cg_entity("u1", "person", "Ayesha"),
        _cg_entity("u2", "person", "Bilal", retired=True),
        {"id": "u3", "record_type": "weird", "kind": "person", "name": "X"},
    ])
    assert [e["id"] for e in part["entities"]] == ["cg:u1"]
    assert {s["reason"] for s in part["skipped"]} == {"retired", "unknown_record_type"}


def test_company_graph_dangling_relationship_skipped():
    part = from_company_graph([
        _cg_entity("u1", "person", "Ayesha"),
        _cg_rel("r1", "u1", "missing", "approves"),
    ])
    assert part["relationships"] == []
    assert part["skipped"] == [{"record_id": "cg:r1", "reason": "dangling_endpoint"}]


def test_company_graph_malformed_rows_raise():
    with pytest.raises(ValueError, match="non-empty 'id'"):
        from_company_graph([{"record_type": "entity", "kind": "person"}])
    with pytest.raises(ValueError, match="non-empty string"):
        from_company_graph([_cg_entity("u1", "  ")])
    with pytest.raises(ValueError, match="Confidence"):
        from_company_graph([_cg_entity("u1", "person", confidence=1.5)])


def test_company_graph_kind_aliases():
    part = from_company_graph([
        _cg_entity("s1", "server", "ERP"),
        _cg_entity("v1", "vendor", "Paper Co"),
        _cg_entity("d1", "department", "Finance"),
    ])
    assert {e["id"]: e["kind"] for e in part["entities"]} == {
        "cg:s1": "system",
        "cg:v1": "supplier",
        "cg:d1": "team",
    }


# ---------------------------------------------------------------- continuity

def test_continuity_mapping_merges_data_attributes():
    part = from_continuity(
        [
            _bc_node("n1", "person", "Ayesha", skills=["approvals"], critical=True, user_id="user-1"),
            _bc_node("n2", "process", "Invoicing"),
        ],
        [_bc_link("n2", "n1", "depends_on")],
    )
    assert len(part["entities"]) == 2
    person = next(e for e in part["entities"] if e["id"] == "bc:n1")
    assert person["kind"] == "person"
    assert person["attributes"]["skills"] == ["approvals"]
    assert person["attributes"]["critical"] is True
    assert person["attributes"]["user_id"] == "user-1"
    assert person["attributes"]["source_graph"] == "continuity"
    assert len(part["relationships"]) == 1
    assert part["relationships"][0]["source_id"] == "bc:n2"
    assert part["skipped"] == []


def test_continuity_dangling_link_skipped_and_malformed_raises():
    part = from_continuity(
        [_bc_node("n1", "person", "Ayesha")],
        [_bc_link("n1", "ghost", "depends_on")],
    )
    assert part["relationships"] == []
    assert part["skipped"][0]["reason"] == "dangling_endpoint"
    with pytest.raises(ValueError, match="non-empty 'id'"):
        from_continuity([{"kind": "person"}], [])


# ---------------------------------------------------------------- finance

def test_finance_batch_amount_from_invoices():
    part = from_finance(
        [{"id": "b1", "name": "March", "status": "awaiting_approval"}],
        [],
        invoices=[
            {"batch_id": "b1", "amount": 1000, "currency": "PKR", "status": "pending"},
            {"batch_id": "b1", "amount": 500, "currency": "PKR", "status": "verified"},
            {"batch_id": "b1", "amount": 999, "currency": "PKR", "status": "rejected"},
        ],
    )
    batch = part["entities"][0]
    assert batch["id"] == "fin:batch:b1"
    assert batch["kind"] == "invoice_batch"
    assert batch["attributes"]["amount"] == 1500
    assert batch["attributes"]["currency"] == "PKR"
    assert batch["attributes"]["status"] == "awaiting_approval"


def test_finance_batch_mixed_currencies_has_no_single_amount():
    part = from_finance(
        [{"id": "b1", "name": "March", "status": "open"}],
        [],
        invoices=[
            {"batch_id": "b1", "amount": 100, "currency": "PKR", "status": "pending"},
            {"batch_id": "b1", "amount": 50, "currency": "USD", "status": "pending"},
        ],
    )
    batch = part["entities"][0]
    assert batch["attributes"]["amount"] is None
    assert batch["attributes"]["currencies"] == ["PKR", "USD"]


def test_finance_payment_maps_amount_and_drops_payee_account():
    part = from_finance(
        [{"id": "b1", "name": "March", "status": "awaiting_approval"}],
        [{
            "id": "pay1", "batch_id": "b1", "vendor_id": "v1",
            "amount": 250, "currency": "PKR", "status": "awaiting_approval",
            "payee_account": "PK00BANK1234567890",
        }],
    )
    assert "payee_account" not in str(part)
    payment = next(e for e in part["entities"] if e["kind"] == "payment")
    assert payment["attributes"]["amount"] == 250
    assert payment["attributes"]["currency"] == "PKR"
    assert payment["attributes"]["status"] == "awaiting_approval"
    assert part["relationships"] == [{
        "id": "fin:payment:pay1->depends_on->fin:batch:b1",
        "source_id": "fin:payment:pay1",
        "target_id": "fin:batch:b1",
        "kind": "depends_on",
        "confidence": 1.0,
    }]


def test_finance_payment_without_batch_is_skipped_not_crash():
    part = from_finance(
        [],
        [{"id": "pay1", "batch_id": "ghost", "vendor_id": "v1",
          "amount": 10, "currency": "PKR", "status": "open"}],
    )
    assert len(part["entities"]) == 1  # the payment entity still exists
    assert part["relationships"] == []
    assert part["skipped"] == [{"record_id": "fin:payment:pay1", "reason": "batch_not_in_snapshot"}]


def test_finance_approvals_create_stub_persons_and_edges():
    part = from_finance(
        [{"id": "b1", "name": "March", "status": "awaiting_approval"}],
        [],
        approvals=[
            {"target_type": "batch", "target_id": "b1", "approver_id": "user-9"},
            {"target_type": "batch", "target_id": "b1", "approver_id": "user-9"},
        ],
    )
    stubs = [e for e in part["entities"] if e["attributes"].get("stub")]
    assert len(stubs) == 1  # duplicate approver -> one stub
    assert stubs[0]["id"] == "fin:person:user-9"
    assert stubs[0]["attributes"]["user_id"] == "user-9"
    edges = [r for r in part["relationships"] if r["kind"] == "approves"]
    assert len(edges) == 2
    assert edges[0]["source_id"] == "fin:person:user-9"
    assert edges[0]["target_id"] == "fin:batch:b1"


def test_finance_negative_amount_raises():
    with pytest.raises(ValueError, match="must not be negative"):
        from_finance([], [{"id": "p", "batch_id": "b", "amount": -5,
                           "currency": "PKR", "status": "open"}])
    with pytest.raises(ValueError, match="must not be negative"):
        from_finance(
            [{"id": "b1", "name": "x", "status": "open"}], [],
            invoices=[{"batch_id": "b1", "amount": -1, "currency": "PKR", "status": "pending"}],
        )


# ---------------------------------------------------------------- build + validate

def test_build_snapshot_combines_sources_and_counts():
    snapshot = build_snapshot(
        company_graph=[
            _cg_entity("u1", "person", "Ayesha", attributes={"skills": ["approvals"]}),
            _cg_entity("pr1", "process", "Invoicing"),
            _cg_rel("r1", "pr1", "u1", "depends_on"),
        ],
        continuity=(
            [_bc_node("n1", "system", "ERP")],
            [],
        ),
        finance=(
            [{"id": "b1", "name": "March", "status": "awaiting_approval"}],
            [],
            [{"batch_id": "b1", "amount": 100, "currency": "PKR", "status": "pending"}],
            [{"target_type": "batch", "target_id": "b1", "approver_id": "user-1"}],
        ),
        as_of="2026-11-28T00:00:00+00:00",
        revision=3,
    )
    assert snapshot["sources"] == {"company_graph": 2, "continuity": 1, "finance": 2}
    assert snapshot["revision"] == 3
    assert snapshot["as_of"] == "2026-11-28T00:00:00+00:00"
    ids = {e["id"] for e in snapshot["entities"]}
    assert ids == {"cg:u1", "cg:pr1", "bc:n1", "fin:batch:b1", "fin:person:user-1"}
    assert snapshot["conflicts"] == []
    assert any("Vendor-to-supplier" in note for note in snapshot["notes"])


def test_build_snapshot_dedupes_duplicate_relationships():
    snapshot = build_snapshot(
        company_graph=[
            _cg_entity("u1", "person", "Ayesha"),
            _cg_entity("b1", "invoice_batch", "Batch 1"),
            _cg_rel("r1", "u1", "b1", "approves"),
            _cg_rel("r2", "u1", "b1", "approves"),
        ],
    )
    assert len(snapshot["relationships"]) == 1


def test_build_snapshot_conflict_keeps_company_graph_version():
    # Same external_key "ext-u1" on both sides, different kind: conflict.
    snapshot2 = build_snapshot(
        company_graph=[_cg_entity("u1", "person", "Ayesha")],
        continuity=(
            [{
                "id": "n1", "external_key": "ext-u1", "kind": "system",
                "name": "Ayesha", "data": {},
            }],
            [],
        ),
    )
    assert len(snapshot2["conflicts"]) == 1
    assert snapshot2["conflicts"][0]["external_key"] == "ext-u1"
    ids = {e["id"] for e in snapshot2["entities"]}
    assert "cg:u1" in ids
    assert "bc:n1" not in ids  # conflict loser dropped, not double-counted
    assert any(s["reason"] == "external_key_conflict" for s in snapshot2["skipped"])


def test_validate_snapshot_reports_problems():
    assert validate_snapshot({"entities": [], "relationships": []}) == []
    assert validate_snapshot({"entities": [{"id": "a", "kind": "person", "name": "A"}], "relationships": []}) == []
    problems = validate_snapshot({
        "entities": [
            {"id": "a", "kind": "person", "name": "A"},
            {"id": "a", "kind": "person", "name": "A again"},
        ],
        "relationships": [{"source_id": "a", "target_id": "ghost", "kind": "depends_on"}],
    })
    assert any("Duplicate entity id" in p for p in problems)
    assert any("unknown entity" in p for p in problems)
    problems = validate_snapshot({
        "entities": [{"id": "b", "kind": "payment", "name": "P",
                      "attributes": {"amount": -5, "currency": "PKR"}}],
        "relationships": [],
    })
    assert any("must not be negative" in p for p in problems)


def test_built_snapshot_feeds_ranking_and_watch_end_to_end():
    snapshot = build_snapshot(
        company_graph=[
            _cg_entity("u1", "person", "Ayesha", attributes={"skills": ["approvals"], "critical": True}),
            _cg_entity("pr1", "process", "Invoicing", attributes={"critical": True}),
            _cg_rel("r1", "u1", "pr1", "owns"),
        ],
        finance=(
            [{"id": "b1", "name": "March", "status": "awaiting_approval"}],
            [],
            [{"batch_id": "b1", "amount": 1500, "currency": "PKR", "status": "pending"}],
            [{"target_type": "batch", "target_id": "b1", "approver_id": "u1"}],
        ),
    )
    ranked = rank_risks(snapshot)
    assert ranked  # every node scored
    assert ranked[0]["risk_score"] > 0
    # The stub approver is a different person (fin:person:u1), so the
    # batch's sole approver is the stub — the watch must still fire.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    result = evaluate_watch(snapshot, now=datetime(2026, 11, 28, 10, 0, tzinfo=ZoneInfo("Asia/Karachi")))
    assert result["alerts"]
    assert all(a.node_id.startswith(("cg:", "fin:")) for a in result["alerts"])
