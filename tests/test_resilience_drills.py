"""Tests for Step AH slice 5 (pure part): app/resilience/drills.py.

Pure-function tests over synthetic snapshots, analyses and drill records
— no database rows, no live data, no secrets.
"""

import pytest

from app.resilience.drills import (
    adjust_ranking,
    compare_drill,
    drill_from_run,
    drill_risk_adjustments,
)
from app.resilience.impact import rank_risks, score_disruption


def _entity(entity_id, kind, name=None, **attrs):
    return {"id": entity_id, "kind": kind, "name": name or entity_id, "attributes": dict(attrs)}


def _rel(source_id, target_id, kind, confidence=1.0):
    return {
        "id": f"{source_id}->{target_id}:{kind}",
        "source_id": source_id,
        "target_id": target_id,
        "kind": kind,
        "confidence": confidence,
    }


def _snapshot():
    """Server sys-1 hosts process proc-1; supplier s-1 feeds it; person
    p-1 operates it. Continuity ids use the bc: prefix."""
    entities = [
        _entity("bc:sys-1", "system", "Main server", critical=True),
        _entity("bc:proc-1", "process", "Pay suppliers", critical=True),
        _entity("bc:s-1", "supplier", "Supplier One"),
        _entity("bc:p-1", "person", "Ayesha", skills=["approvals"]),
        _entity("bc:b-1", "invoice_batch", "Batch 1", amount=900, currency="PKR",
                status="awaiting_approval"),
    ]
    relationships = [
        _rel("bc:sys-1", "bc:proc-1", "hosts"),
        _rel("bc:s-1", "bc:proc-1", "supplies"),
        _rel("bc:p-1", "bc:proc-1", "operates"),
        _rel("bc:proc-1", "bc:b-1", "depends_on"),
    ]
    return {"entities": entities, "relationships": relationships}


def _drill(observed_ids, steps=None, drill_id="drill-1"):
    return {
        "id": drill_id,
        "scenario_id": "scenario-1",
        "mode": "drill",
        "status": "closed",
        "observed": {"affected_ids": sorted(observed_ids), "steps": steps or []},
    }


def test_drill_from_run_builds_snapshot_ids_and_rto():
    run = {
        "id": "11111111-1111-1111-1111-111111111111",
        "mode": "drill",
        "status": "closed",
        "scenario_id": "22222222-2222-2222-2222-222222222222",
        "disruption_at": "2026-11-28T00:00:00+00:00",
        "closed_at": "2026-11-28T02:00:00+00:00",
    }
    plan = {
        "steps": [
            {"key": "restore-server", "resource_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "due_minutes": 60},
            {"key": "notify-team", "resource_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "due_minutes": 30},
        ],
        "objective_snapshots": {
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa": {"rto_minutes": 120, "rpo_minutes": 30,
                                                    "minimum_service_percent": 80},
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb": {"rto_minutes": 30, "rpo_minutes": 0,
                                                    "minimum_service_percent": 100},
        },
    }
    tasks = [
        {"step_key": "restore-server", "status": "verified",
         "evidence": {"recovered_at": "2026-11-28T01:30:00+00:00",
                      "restored_point_at": "2026-11-27T23:45:00+00:00", "service_percent": 90}},
        {"step_key": "notify-team", "status": "verified",
         "evidence": {"recovered_at": "2026-11-28T00:20:00+00:00",
                      "restored_point_at": None, "service_percent": 100}},
    ]
    drill = drill_from_run(run, tasks, plan)
    assert drill["id"] == "11111111-1111-1111-1111-111111111111"
    assert drill["observed"]["affected_ids"] == [
        "bc:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "bc:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]
    restore = next(s for s in drill["observed"]["steps"] if s["step_key"] == "restore-server")
    assert restore["recovery_minutes"] == 90.0
    assert restore["data_loss_minutes"] == 15.0
    assert restore["rto_met"] is True
    notify = next(s for s in drill["observed"]["steps"] if s["step_key"] == "notify-team")
    assert notify["recovery_minutes"] == 20.0
    assert notify["data_loss_minutes"] is None
    assert notify["rto_met"] is True


def test_drill_from_run_rejects_non_drills_and_open_runs():
    run = {"id": "x", "mode": "incident", "status": "closed"}
    with pytest.raises(ValueError, match="mode 'drill'"):
        drill_from_run(run, [], {})
    run = {"id": "x", "mode": "drill", "status": "active"}
    with pytest.raises(ValueError, match="closed"):
        drill_from_run(run, [], {})


def test_compare_drill_matches_when_drill_covers_prediction():
    snapshot = _snapshot()
    analysis = score_disruption(
        snapshot, {"kind": "server_outage", "target_ids": ["bc:sys-1"], "duration_days": 1}
    )
    predicted = {item["id"] for item in analysis["affected"]}
    drill = _drill(predicted)
    comparison = compare_drill(snapshot, analysis, drill)
    assert comparison["matches"] is True
    assert comparison["differences"] == []
    assert comparison["predicted_count"] == len(predicted)
    assert comparison["observed_count"] == len(predicted)


def test_compare_drill_lists_unexpected_and_missing():
    snapshot = _snapshot()
    analysis = score_disruption(
        snapshot, {"kind": "server_outage", "target_ids": ["bc:sys-1"], "duration_days": 1}
    )
    # The drill exercised an unpredicted supplier and missed the process.
    # With no per-step evidence there is no uncovered hit: contact alone
    # is not a failure (every affected entity lacks a recorded backup).
    drill = _drill(["bc:sys-1", "bc:s-1"])
    comparison = compare_drill(snapshot, analysis, drill)
    types = {d["type"] for d in comparison["differences"]}
    assert "unexpected_affected" in types
    assert "missing_affected" in types
    assert "uncovered_hit" not in types
    assert comparison["matches"] is False
    unexpected = [d for d in comparison["differences"] if d["type"] == "unexpected_affected"]
    assert {d["node_id"] for d in unexpected} == {"bc:s-1"}
    missing = [d for d in comparison["differences"] if d["type"] == "missing_affected"]
    assert {d["node_id"] for d in missing} == {"bc:proc-1"}


def test_compare_drill_uncovered_hit_and_rto_missed():
    snapshot = _snapshot()
    analysis = score_disruption(
        snapshot, {"kind": "server_outage", "target_ids": ["bc:sys-1"], "duration_days": 1}
    )
    uncovered_ids = set(analysis["uncovered"])
    assert uncovered_ids  # the process and the server have no backup
    hit = sorted(uncovered_ids)[0]
    drill = _drill(
        ["bc:sys-1", "bc:proc-1", hit],
        steps=[
            {"step_key": "restore", "entity_id": "bc:sys-1", "recovery_minutes": 200.0,
             "rto_minutes": 120, "status": "verified"},
            # The uncovered entity's own step also missed its RTO: the
            # missing cover mattered, so this is an uncovered hit.
            {"step_key": "notify", "entity_id": hit, "recovery_minutes": 200.0,
             "rto_minutes": 30, "status": "verified"},
        ],
    )
    comparison = compare_drill(snapshot, analysis, drill)
    types = [(d["type"], d.get("node_id")) for d in comparison["differences"]]
    assert ("uncovered_hit", hit) in types
    assert ("rto_missed", "bc:sys-1") in types
    assert ("rto_missed", hit) in types
    assert comparison["matches"] is False


def test_drill_risk_adjustments_raise_only_on_failures():
    comparison = {
        "differences": [
            {"type": "unexpected_affected", "node_id": "n-1"},
            {"type": "uncovered_hit", "node_id": "n-1"},
            {"type": "rto_missed", "node_id": "n-2", "step_key": "restore"},
            {"type": "missing_affected", "node_id": "n-3"},
        ]
    }
    adjustments = drill_risk_adjustments(comparison)
    assert adjustments == [
        {"node_id": "n-1", "delta": 25, "factors": ["uncovered_entity_hit_in_drill", "unexpected_in_drill"]},
        {"node_id": "n-2", "delta": 10, "factors": ["rto_missed_in_drill"]},
    ]
    # Missing entities never raise a score; a passing drill never lowers one.
    assert "n-3" not in {a["node_id"] for a in adjustments}


def test_drill_risk_adjustments_cap_at_thirty():
    comparison = {
        "differences": [
            {"type": "unexpected_affected", "node_id": "n-1"},
            {"type": "uncovered_hit", "node_id": "n-1"},
            {"type": "rto_missed", "node_id": "n-1"},
            {"type": "rto_missed", "node_id": "n-1"},
        ]
    }
    adjustments = drill_risk_adjustments(comparison)
    assert adjustments[0]["delta"] == 30


def test_adjust_ranking_applies_deltas_and_keeps_order():
    snapshot = _snapshot()
    ranked = rank_risks(snapshot)
    before = {item["node_id"]: item["risk_score"] for item in ranked}
    adjustments = [{"node_id": "bc:s-1", "delta": 25, "factors": ["unexpected_in_drill"]}]
    adjusted = adjust_ranking(ranked, adjustments)
    after = {item["node_id"]: item["risk_score"] for item in adjusted}
    assert after["bc:s-1"] == min(100, before["bc:s-1"] + 25)
    assert "failed_drill:+25" in next(
        item["factors"] for item in adjusted if item["node_id"] == "bc:s-1"
    )
    scores = [item["risk_score"] for item in adjusted]
    assert scores == sorted(scores, reverse=True)
    # Nodes without adjustments are untouched.
    assert after["bc:sys-1"] == before["bc:sys-1"]
    # Unknown nodes are ignored.
    assert adjust_ranking(ranked, [{"node_id": "nope", "delta": 10, "factors": []}]) == ranked
