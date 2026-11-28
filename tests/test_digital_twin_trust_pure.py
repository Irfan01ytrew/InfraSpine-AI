"""Multi-source trust scoring (Step K v2 slice 7). Pure tests, no DB."""

import pytest

from app.digital_twin import trust


def test_all_fresh_is_trustworthy_full_coverage():
    report = trust.compute_trust([
        {"name": "company_graph", "stale": False},
        {"name": "finance", "stale": False},
        {"name": "continuity", "stale": False},
        {"name": "membership", "stale": False},
        {"name": "workflow", "stale": False},
        {"name": "agent_studio", "stale": False},
        {"name": "connectors", "stale": False},
    ])
    assert report["score"] == 100
    assert report["band"] == "trustworthy"
    assert report["coverage"] == 1.0
    assert report["stale_sources"] == []
    assert report["drift_score"] is None
    assert report["drift_trust"] is None


def test_weighted_mean_counts_graph_heaviest():
    # Only the graph (30) fresh and finance (20) stale: 30/50 = 60.
    report = trust.compute_trust([
        {"name": "company_graph", "stale": False},
        {"name": "finance", "stale": True},
    ])
    assert report["score"] == 60
    assert report["band"] == "review"
    assert report["coverage"] == 0.5
    assert report["stale_sources"] == ["finance"]


def test_unknown_sources_drop_coverage_not_score():
    report = trust.compute_trust([{"name": "company_graph", "stale": False}])
    assert report["score"] == 100
    assert report["band"] == "trustworthy"
    assert report["coverage"] == 0.3
    unknown = {s["source"]: s for s in report["sources"]}
    assert unknown["finance"]["status"] == "unknown"
    assert unknown["finance"]["score"] is None


def test_no_known_source_is_unknown_not_zero():
    report = trust.compute_trust([])
    assert report["score"] is None
    assert report["band"] == "unknown"
    assert report["coverage"] == 0.0
    report = trust.compute_trust([{"name": "finance", "status": "unknown"}])
    assert report["score"] is None
    assert report["band"] == "unknown"


def test_drift_verdict_can_only_worsen_the_band():
    fresh = [{"name": "company_graph", "stale": False}, {"name": "finance", "stale": False}]
    report = trust.compute_trust(fresh, {"drift_score": 10, "trust": "trustworthy"})
    assert report["band"] == "trustworthy"
    report = trust.compute_trust(fresh, {"drift_score": 90, "trust": "stale"})
    assert report["band"] == "stale"
    assert report["score"] == 100  # freshness score is untouched; the band carries the verdict
    report = trust.compute_trust([], {"drift_score": 90, "trust": "stale"})
    assert report["band"] == "stale"
    assert report["score"] is None


def test_unlisted_source_listed_with_zero_weight_never_dropped():
    report = trust.compute_trust([
        {"name": "company_graph", "stale": False},
        {"name": "telemetry", "stale": True},
    ])
    by_name = {s["source"]: s for s in report["sources"]}
    assert by_name["telemetry"]["weight"] == 0
    assert by_name["telemetry"]["known_source"] is False
    assert by_name["telemetry"]["status"] == "stale"
    assert by_name["telemetry"]["score"] == 0
    assert report["score"] == 100  # zero-weight source cannot move the score
    assert report["stale_sources"] == ["telemetry"]


def test_status_field_and_last_entry_wins():
    report = trust.compute_trust([{"name": "finance", "status": "stale"}])
    assert report["score"] == 0
    assert report["band"] == "stale"
    report = trust.compute_trust([
        {"name": "finance", "status": "stale"},
        {"name": "finance", "status": "fresh"},
    ])
    assert report["score"] == 100


def test_malformed_inputs_raise():
    with pytest.raises(ValueError):
        trust.compute_trust("not-a-list")
    with pytest.raises(ValueError):
        trust.compute_trust([{"stale": False}])
    with pytest.raises(ValueError):
        trust.compute_trust([{"name": "finance", "status": "weird"}])
    with pytest.raises(ValueError):
        trust.compute_trust([], {"drift_score": 101})
    with pytest.raises(ValueError):
        trust.compute_trust([], {"trust": "great"})
    with pytest.raises(ValueError):
        trust.trust_band(101)
    with pytest.raises(ValueError):
        trust.trust_band("high")


def test_trust_band_boundaries():
    assert trust.trust_band(100) == "trustworthy"
    assert trust.trust_band(75) == "trustworthy"
    assert trust.trust_band(74) == "review"
    assert trust.trust_band(40) == "review"
    assert trust.trust_band(39) == "stale"
    assert trust.trust_band(0) == "stale"
    assert trust.trust_band(None) == "unknown"


def test_trust_summary_is_count_only():
    report = trust.compute_trust([
        {"name": "company_graph", "stale": False},
        {"name": "finance", "stale": True},
    ])
    line = trust.trust_summary(report)
    assert "60" in line and "review" in line and "finance" in line
