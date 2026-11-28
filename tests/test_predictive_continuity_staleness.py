"""Tests for Step L slice 3: forecast staleness from twin drift.

Pure functions only — no database, no network, no conftest. Run with::

    PYTHONPATH=<worktree> python -m pytest tests/test_predictive_continuity_staleness.py -q -p no:cacheprovider --noconftest

Staleness reimplements the Step K drift weights and trust bands
(``app.digital_twin.drift``, branch ``agent/digital-twin-v2``) because
that module does not exist on this base. The cross-check test below
imports it from the sibling worktree when present and requires
byte-identical scores and verdicts on the same snapshot pairs.
"""

from pathlib import Path

import pytest

from app.predictive_continuity.staleness import (
    REVIEW_MAX_SCORE,
    TRUSTWORTHY_MAX_SCORE,
    assess_staleness,
    snapshot_fingerprint,
    staleness_summary,
    trust_for_score,
    validate_snapshot,
)


def _snap(entities, relationships=(), **extra):
    snap = {"entities": list(entities), "relationships": list(relationships)}
    snap.update(extra)
    return snap


def _ent(eid, kind="process", name=None, **attrs):
    return {"id": eid, "kind": kind, "name": name or eid, "attributes": attrs}


def _edge(rid, source, target, kind="depends_on", confidence=1.0):
    return {"id": rid, "source_id": source, "target_id": target,
            "kind": kind, "confidence": confidence}


BASE = _snap(
    [_ent("pr-1"), _ent("pr-2"),
     _ent("b-1", kind="invoice_batch", amount=1500.00, currency="PKR")],
    [_edge("e-1", "b-1", "pr-1")],
    as_of="2026-11-28T00:00:00+00:00",
)


def test_identical_snapshots_score_zero_and_trustworthy():
    report = assess_staleness(BASE, _snap(BASE["entities"], BASE["relationships"]))
    assert report["drift_score"] == 0
    assert report["trust"] == "trustworthy"
    assert report["changed"] is False
    assert report["entities"]["added"] == []
    assert report["entities"]["removed"] == []
    assert report["money"]["delta"]["totals"] == {"PKR": 0.0}
    assert "0/100 (trustworthy)" in staleness_summary(report)


def test_one_removed_entity_scores_10():
    new = _snap([e for e in BASE["entities"] if e["id"] != "pr-2"],
                [_edge("e-1", "b-1", "pr-1")])
    report = assess_staleness(BASE, new)
    assert report["entities"]["removed"] == ["pr-2"]
    assert report["drift_score"] == 10  # one removed entity x 10 points
    assert report["trust"] == "trustworthy"


def test_removed_entity_points_cap_at_40():
    entities = [_ent(f"pr-{i}") for i in range(1, 8)]
    old = _snap(entities)
    new = _snap(entities[:2])  # five removed: 5 x 10 = 50, capped at 40
    report = assess_staleness(old, new)
    assert report["drift_score"] == 40
    assert report["trust"] == "review"  # 40 is above 25, below 60


def test_added_and_changed_entities_score():
    new = _snap(
        [_ent("pr-1"), _ent("pr-2", name="Pay vendors"),  # name changed: 3
         _ent("b-1", kind="invoice_batch", amount=1500.00, currency="PKR"),
         _ent("pr-3")],  # added: 2
        [_edge("e-1", "b-1", "pr-1")])
    report = assess_staleness(BASE, new)
    assert report["entities"]["added"] == ["pr-3"]
    assert [c["id"] for c in report["entities"]["changed"]] == ["pr-2"]
    assert report["drift_score"] == 5  # 2 added + 3 changed
    assert report["trust"] == "trustworthy"


def test_money_delta_half_scores_10():
    new = _snap(
        [_ent("pr-1"), _ent("pr-2"),
         _ent("b-1", kind="invoice_batch", amount=750.00, currency="PKR")],
        [_edge("e-1", "b-1", "pr-1")])
    report = assess_staleness(BASE, new)
    assert report["money"]["delta"]["totals"] == {"PKR": -750.0}
    # 10 money points (20 x 50% largest share) + 3 for the changed
    # amount attribute on the batch itself.
    assert report["drift_score"] == 13
    assert report["trust"] == "trustworthy"


def test_edge_changes_score():
    new = _snap(BASE["entities"],
                [_edge("e-1", "b-1", "pr-1", confidence=0.5)])  # changed edge: 2
    report = assess_staleness(BASE, new)
    assert [c["id"] for c in report["relationships"]["changed"]] == ["e-1"]
    assert report["drift_score"] == 2


def test_stale_sources_force_stale_even_at_score_zero():
    new = _snap(BASE["entities"], BASE["relationships"], stale_sources=["hr_feed"])
    report = assess_staleness(BASE, new)
    assert report["drift_score"] == 0
    assert report["trust"] == "stale"
    assert report["stale_sources"] == ["hr_feed"]


def test_trust_bands():
    assert trust_for_score(0) == "trustworthy"
    assert trust_for_score(TRUSTWORTHY_MAX_SCORE) == "trustworthy"  # 25
    assert trust_for_score(TRUSTWORTHY_MAX_SCORE + 1) == "review"  # 26
    assert trust_for_score(REVIEW_MAX_SCORE) == "review"  # 60
    assert trust_for_score(REVIEW_MAX_SCORE + 1) == "stale"  # 61
    assert trust_for_score(100) == "stale"
    assert trust_for_score(None) == "unknown"


def test_malformed_snapshots_raise():
    duplicate = _snap([_ent("pr-1"), _ent("pr-1")])
    with pytest.raises(ValueError, match="Duplicate entity id"):
        validate_snapshot(duplicate)
    with pytest.raises(ValueError, match="Duplicate entity id"):
        assess_staleness(BASE, duplicate)
    bad_money = _snap([_ent("b-1", kind="invoice_batch", amount="lots", currency="PKR")])
    with pytest.raises(ValueError, match="Non-numeric money amount"):
        assess_staleness(BASE, bad_money)
    dangling = _snap([_ent("pr-1")], [_edge("e-1", "pr-1", "ghost")])
    with pytest.raises(ValueError, match="dangling target"):
        assess_staleness(BASE, dangling)


def test_fingerprint_uses_stored_value_when_present():
    snap = _snap([_ent("pr-1")], fingerprint="a" * 64)
    assert snapshot_fingerprint(snap) == "a" * 64
    assert snapshot_fingerprint(_snap([_ent("pr-1")])) == snapshot_fingerprint(_snap([_ent("pr-1")]))
    assert snapshot_fingerprint(_snap([_ent("pr-1")])) != snapshot_fingerprint(_snap([_ent("pr-2")]))


# --- Cross-check against Step K's drift module -------------------------

K_DRIFT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "digital-twin-v2" / "app" / "digital_twin" / "drift.py"
)


def _load_k_drift():
    import importlib.util

    spec = importlib.util.spec_from_file_location("k_drift", K_DRIFT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PAIRS = [
    ("identical", BASE, _snap(BASE["entities"], BASE["relationships"])),
    ("one removed", BASE, _snap([e for e in BASE["entities"] if e["id"] != "pr-2"],
                                [_edge("e-1", "b-1", "pr-1")])),
    ("one added", BASE, _snap([*BASE["entities"], _ent("pr-9")],
                              BASE["relationships"])),
    ("name changed", BASE, _snap(
        [_ent("pr-1", name="Pay vendors"), BASE["entities"][1], BASE["entities"][2]],
        BASE["relationships"])),
    ("money halved", BASE, _snap(
        [_ent("pr-1"), _ent("pr-2"),
         _ent("b-1", kind="invoice_batch", amount=750.00, currency="PKR")],
        [_edge("e-1", "b-1", "pr-1")])),
    ("edge confidence changed", BASE, _snap(
        BASE["entities"], [_edge("e-1", "b-1", "pr-1", confidence=0.5)])),
    ("stale source", BASE, _snap(
        BASE["entities"], BASE["relationships"], stale_sources=["hr_feed"])),
    ("everything moves", _snap(
        [_ent(f"pr-{i}") for i in range(1, 6)]
        + [_ent("b-1", kind="invoice_batch", amount=2000.00, currency="PKR")],
        [_edge("e-1", "b-1", "pr-1")]),
     _snap(
        [_ent("pr-1"), _ent("pr-3", name="Renamed"),
         _ent("b-1", kind="invoice_batch", amount=500.00, currency="PKR"),
         _ent("pr-9")],
        [_edge("e-1", "b-1", "pr-1", confidence=0.4),
         _edge("e-2", "pr-9", "pr-1")])),
]


@pytest.mark.parametrize("name,old,new", _PAIRS, ids=[p[0] for p in _PAIRS])
def test_cross_check_against_step_k_drift(name, old, new):
    """L staleness must agree with K drift on score, trust, and diffs.

    Skipped when the sibling Step K worktree is absent (e.g. after K
    merges — then this module should be replaced by the K import and
    this test deleted).
    """
    if not K_DRIFT_PATH.exists():
        pytest.skip(f"Step K drift module not present at {K_DRIFT_PATH}")
    k = _load_k_drift()
    ours = assess_staleness(old, new)
    theirs = k.diff_snapshots(old, new)
    assert ours["drift_score"] == theirs["drift_score"], name
    assert ours["trust"] == theirs["trust"], name
    assert ours["entities"]["added"] == theirs["entities"]["added"], name
    assert ours["entities"]["removed"] == theirs["entities"]["removed"], name
    assert [c["id"] for c in ours["entities"]["changed"]] == [
        c["id"] for c in theirs["entities"]["changed"]], name
    assert ours["relationships"]["added"] == theirs["relationships"]["added"], name
    assert ours["relationships"]["removed"] == theirs["relationships"]["removed"], name
    assert [c["id"] for c in ours["relationships"]["changed"]] == [
        c["id"] for c in theirs["relationships"]["changed"]], name
    assert ours["money"]["delta"] == theirs["money"]["delta"], name
    assert ours["changed"] == theirs["changed"], name
