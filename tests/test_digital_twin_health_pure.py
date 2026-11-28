"""Twin health score + watch (Step K v2 slice 3) — pure tests.

No database, no network, no app imports beyond the two slice-3
modules and the slice-1 drift detector. Run with ``--noconftest``
(the worktree venv has no sqlalchemy; these tests do not need it).
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.digital_twin import drift, health, health_watch

PKT = ZoneInfo("Asia/Karachi")


def _entities(*, names=("Ayesha", "ERP"), batch=100000, payment=None, extra=()):
    entities = [
        {"id": "e-ayesha", "kind": "employee", "name": names[0], "attributes": {"department": "Finance"}},
        {"id": "e-erp", "kind": "system", "name": names[1], "attributes": {}},
    ]
    if batch is not None:
        entities.append({"id": "e-batch", "kind": "invoice_batch", "name": "Batch 1",
                         "attributes": {"amount": batch, "currency": "PKR"}})
    if payment is not None:
        entities.append({"id": "e-pay", "kind": "payment", "name": "Pay 1",
                         "attributes": {"amount": payment, "currency": "PKR"}})
    entities.extend(extra)
    return entities


def _snapshot(*, names=("Ayesha", "ERP"), batch=100000, payment=None, extra=(),
              entities=None, stale_sources=()):
    if entities is None:
        entities = _entities(names=names, batch=batch, payment=payment, extra=extra)
    ids = {entity["id"] for entity in entities}
    edges = [
        {"id": "r-1", "source_id": "e-ayesha", "target_id": "e-erp",
         "kind": "owns", "confidence": 1.0, "evidence": []},
    ]
    edges = [edge for edge in edges if edge["source_id"] in ids and edge["target_id"] in ids]
    return {"entities": entities, "relationships": edges,
            "as_of": "2026-11-28T00:00:00Z", "stale_sources": list(stale_sources)}


def _report(old, new):
    return drift.diff_snapshots(old, new)


def _assessment(report, *, fingerprint="f" * 64, freshness=None):
    return health.compute_health(
        drift_report=report, source_freshness=freshness,
        fingerprint=fingerprint, as_of="2026-11-28T00:00:00Z")


# ------------------------------------------------------------------ score


def test_first_version_health_is_100_when_fresh():
    assessment = health.compute_health(fingerprint="a" * 64)
    assert assessment["health_score"] == 100
    assert assessment["band"] == "healthy"
    assert assessment["is_first_version"] is True
    assert assessment["drift_score"] is None
    assert assessment["trust"] is None
    assert assessment["components"] == {"drift": 0, "money": 0, "freshness": 0}
    assert assessment["stale_source_count"] == 0


def test_identical_snapshots_health_is_100():
    snap = _snapshot()
    assessment = _assessment(_report(snap, snap))
    assert assessment["health_score"] == 100
    assert assessment["band"] == "healthy"
    assert assessment["drift_score"] == 0
    assert assessment["trust"] == "trustworthy"


def test_small_drift_splits_into_structural_and_money():
    # Agent 123's verified drift-11 case: +1 entity (2) + 2 changed (6)
    # + money share 20000/120000 (3) = 11.
    report = _report(
        _snapshot(),
        _snapshot(names=("Ayesha", "ERP v2"), batch=120000,
                  extra=[{"id": "e-backup", "kind": "employee", "name": "Backup", "attributes": {}}]),
    )
    assert report["drift_score"] == 11
    assessment = _assessment(report)
    assert assessment["health_score"] == 89
    assert assessment["band"] == "healthy"
    assert assessment["components"] == {"drift": 8, "money": 3, "freshness": 0}
    assert assessment["components"]["drift"] + assessment["components"]["money"] == 11


def test_health_band_boundaries_align_with_drift_trust_bands():
    # Drift 25 (trustworthy) -> health 75, healthy boundary:
    # removed 2 (20) + changed 1 name (3) + added 1 (2) = 25.
    old_five = _snapshot(entities=_entities(extra=[
        {"id": "e-x", "kind": "employee", "name": "X", "attributes": {}},
        {"id": "e-y", "kind": "employee", "name": "Y", "attributes": {}},
    ]))
    report_25 = _report(
        old_five,
        _snapshot(entities=[
            {"id": "e-ayesha", "kind": "employee", "name": "Ayesha", "attributes": {"department": "Finance"}},
            {"id": "e-erp", "kind": "system", "name": "ERP v2", "attributes": {}},
            {"id": "e-batch", "kind": "invoice_batch", "name": "Batch 1",
             "attributes": {"amount": 100000, "currency": "PKR"}},
            {"id": "e-z", "kind": "employee", "name": "Z", "attributes": {}},
        ]),
    )
    assert report_25["drift_score"] == 25
    assert report_25["trust"] == "trustworthy"
    assert _assessment(report_25)["health_score"] == 75
    assert _assessment(report_25)["band"] == "healthy"

    # Drift 26 (review) -> health 74, degraded:
    # removed 2 (20) + changed 2 names (6) = 26.
    report_26 = _report(
        old_five,
        _snapshot(entities=[
            {"id": "e-ayesha", "kind": "employee", "name": "Ayesha v2", "attributes": {"department": "Finance"}},
            {"id": "e-erp", "kind": "system", "name": "ERP v2", "attributes": {}},
            {"id": "e-batch", "kind": "invoice_batch", "name": "Batch 1",
             "attributes": {"amount": 100000, "currency": "PKR"}},
        ]),
    )
    assert report_26["drift_score"] == 26
    assert report_26["trust"] == "review"
    assert _assessment(report_26)["health_score"] == 74
    assert _assessment(report_26)["band"] == "degraded"


def test_drift_60_is_degraded_boundary_and_above_is_critical():
    base_entities = [
        {"id": f"e-n{i}", "kind": "employee", "name": f"N{i}", "attributes": {}}
        for i in range(6)
    ] + [{"id": "e-batch", "kind": "invoice_batch", "name": "Batch 1",
          "attributes": {"amount": 100000, "currency": "PKR"}}]
    kept = base_entities[4:6]  # batch goes too: money share 1.0 -> 20 pts
    report_60 = _report(_snapshot(entities=base_entities), _snapshot(entities=kept))
    # Removed 4 entities (40, cap) + batch gone (money share 1.0 -> 20) = 60.
    assert report_60["drift_score"] == 60
    assert report_60["trust"] == "review"
    assert _assessment(report_60)["health_score"] == 40
    assert _assessment(report_60)["band"] == "degraded"

    changed = [dict(entity) for entity in kept]
    changed[0] = {**changed[0], "name": "N4 renamed"}
    report_63 = _report(_snapshot(entities=base_entities), _snapshot(entities=changed))
    assert report_63["drift_score"] == 63
    assert report_63["trust"] == "stale"
    assert _assessment(report_63)["health_score"] == 37
    assert _assessment(report_63)["band"] == "critical"


def test_stale_source_penalty_and_cap():
    freshness = [{"name": "erp", "status": "stale"}]
    assessment = health.compute_health(fingerprint="a" * 64, source_freshness=freshness)
    assert assessment["health_score"] == 90
    assert assessment["components"]["freshness"] == 10
    assert assessment["stale_sources"] == ["erp"]

    many = [{"name": f"s{i}", "stale": True} for i in range(5)]
    capped = health.compute_health(fingerprint="a" * 64, source_freshness=many)
    assert capped["components"]["freshness"] == 30  # 5 x 10 capped at 30
    assert capped["health_score"] == 70
    assert capped["stale_source_count"] == 5


def test_stale_sources_merge_from_report_and_freshness_list():
    report = _report(_snapshot(stale_sources=["erp"]), _snapshot(stale_sources=["erp"]))
    # A stale source makes drift trust stale even at drift 0.
    assert report["trust"] == "stale"
    assessment = _assessment(
        report, freshness=[{"name": "crm", "status": "stale"}, {"name": "erp", "status": "stale"}])
    assert assessment["stale_sources"] == ["crm", "erp"]  # deduped, sorted
    assert assessment["components"]["freshness"] == 20
    assert assessment["health_score"] == 80


def test_health_summary_text():
    first = health.compute_health(fingerprint="a" * 64)
    assert health.health_summary(first) == (
        "Twin health 100/100 (healthy): no drift recorded yet, 0 stale source(s).")
    report = _report(_snapshot(), _snapshot(names=("Ayesha", "ERP v2"), batch=120000,
                                            extra=[{"id": "e-backup", "kind": "employee",
                                                            "name": "Backup", "attributes": {}}]))
    summary = health.health_summary(_assessment(report))
    assert summary == "Twin health 89/100 (healthy): drift 11 (structural 8, money 3), 0 stale source(s)."


def test_compute_health_rejects_bad_inputs():
    with pytest.raises(ValueError, match="drift_score"):
        health.compute_health(drift_report={"drift_score": 101, "trust": "review"})
    with pytest.raises(ValueError, match="trust"):
        health.compute_health(drift_report={"drift_score": 10, "trust": "bogus"})
    with pytest.raises(ValueError, match="non-empty string 'name'"):
        health.compute_health(source_freshness=[{"status": "stale"}])
    with pytest.raises(ValueError, match="must be a list"):
        health.compute_health(source_freshness={"name": "erp"})
    with pytest.raises(ValueError, match="fingerprint"):
        health.compute_health(fingerprint="")


# ------------------------------------------------------- prediction staleness


def test_prediction_stale_when_fingerprint_matches_is_false():
    assessment = _assessment(_report(_snapshot(), _snapshot()), fingerprint="a" * 64)
    assert health.prediction_is_stale({"fingerprint": "a" * 64}, assessment) == {
        "stale": False, "reason": "prediction matches the latest twin version"}


def test_prediction_stale_when_drift_exceeds_threshold():
    base_entities = [
        {"id": f"e-n{i}", "kind": "employee", "name": f"N{i}", "attributes": {}}
        for i in range(6)
    ] + [{"id": "e-batch", "kind": "invoice_batch", "name": "Batch 1",
          "attributes": {"amount": 100000, "currency": "PKR"}}]
    report = _report(_snapshot(entities=base_entities), _snapshot(entities=base_entities[4:6]))
    assessment = _assessment(report, fingerprint="b" * 64)
    assert assessment["drift_score"] == 60
    result = health.prediction_is_stale({"twin_fingerprint": "a" * 64}, assessment)
    assert result == {"stale": False,
                      "reason": "twin drift 60 is within the staleness threshold 60"}
    assert health.prediction_is_stale(
        {"fingerprint": "a" * 64}, assessment, threshold=25)["stale"] is True


def test_prediction_stale_fail_closed_cases():
    assessment = _assessment(_report(_snapshot(), _snapshot()), fingerprint="b" * 64)
    assert health.prediction_is_stale({}, assessment)["stale"] is True
    assert health.prediction_is_stale({"fingerprint": ""}, assessment)["stale"] is True
    assert health.prediction_is_stale({"fingerprint": "a" * 64}, None) == {
        "stale": False, "reason": "no twin version recorded; nothing to compare against"}
    first = health.compute_health(fingerprint="b" * 64)
    assert health.prediction_is_stale({"fingerprint": "a" * 64}, first) == {
        "stale": True,
        "reason": "prediction fingerprint does not match the only recorded twin version; drift is unknown"}
    with pytest.raises(ValueError, match="prediction must be a dict"):
        health.prediction_is_stale("not-a-dict", assessment)


# ------------------------------------------------------------------- watch


def _degraded_assessment():
    # Drift 26 -> health 74: removed 2 (20) + changed 2 names (6).
    report = _report(
        _snapshot(entities=_entities(extra=[
            {"id": "e-x", "kind": "employee", "name": "X", "attributes": {}},
            {"id": "e-y", "kind": "employee", "name": "Y", "attributes": {}},
        ])),
        _snapshot(entities=[
            {"id": "e-ayesha", "kind": "employee", "name": "Ayesha v2", "attributes": {"department": "Finance"}},
            {"id": "e-erp", "kind": "system", "name": "ERP v2", "attributes": {}},
            {"id": "e-batch", "kind": "invoice_batch", "name": "Batch 1",
             "attributes": {"amount": 100000, "currency": "PKR"}},
        ]),
    )
    return _assessment(report, fingerprint="d" * 64)


def _critical_assessment():
    # Drift 63 -> health 37: removed 4 entities (40) + batch gone (20)
    # + renamed one survivor (3).
    base_entities = [
        {"id": f"e-n{i}", "kind": "employee", "name": f"N{i}", "attributes": {}}
        for i in range(6)
    ] + [{"id": "e-batch", "kind": "invoice_batch", "name": "Batch 1",
          "attributes": {"amount": 100000, "currency": "PKR"}}]
    kept = [dict(entity) for entity in base_entities[4:6]]
    kept[0] = {**kept[0], "name": "N4 renamed"}
    report = _report(_snapshot(entities=base_entities), _snapshot(entities=kept))
    return _assessment(report, fingerprint="c" * 64)


def test_healthy_twin_fires_nothing():
    assessment = _assessment(_report(_snapshot(), _snapshot()), fingerprint="a" * 64)
    result = health_watch.evaluate_health_watch(
        assessment, tenant_id="t-1", now=datetime(2026, 11, 28, 10, 0, tzinfo=PKT))
    assert result["alerts"] == [] and result["suppressed"] == [] and result["deferred"] == []
    assert result["fingerprint"] == "a" * 64


def test_degraded_fires_warning_in_daytime_and_defers_at_night():
    assessment = _degraded_assessment()
    assert assessment["health_score"] == 75 - 1  # drift 26 -> 74
    day = health_watch.evaluate_health_watch(
        assessment, tenant_id="t-1", now=datetime(2026, 11, 28, 10, 0, tzinfo=PKT))
    assert [alert.rule_key for alert in day["alerts"]] == ["health_degraded"]
    assert day["alerts"][0].severity == "warning"
    assert day["deferred"] == []

    night = health_watch.evaluate_health_watch(
        assessment, tenant_id="t-1", now=datetime(2026, 11, 28, 23, 0, tzinfo=PKT))
    assert night["alerts"] == []
    assert [entry["rule_key"] for entry in night["deferred"]] == ["health_degraded"]
    assert night["deferred"][0]["reason"] == "quiet_hours"


def test_critical_breaks_quiet_hours():
    assessment = _critical_assessment()
    assert assessment["health_score"] == 37 and assessment["trust"] == "stale"
    night = health_watch.evaluate_health_watch(
        assessment, tenant_id="t-1", now=datetime(2026, 11, 28, 23, 30, tzinfo=PKT))
    assert sorted(alert.rule_key for alert in night["alerts"]) == ["health_critical", "twin_stale"]
    assert all(alert.severity == "critical" for alert in night["alerts"])
    # health_degraded also matches but is a warning: deferred, never dropped.
    assert [entry["rule_key"] for entry in night["deferred"]] == ["health_degraded"]


def test_stale_source_rule_fires_even_when_health_is_high():
    report = _report(_snapshot(stale_sources=["erp"]), _snapshot(stale_sources=["erp"]))
    assessment = _assessment(report, fingerprint="e" * 64,
                             freshness=[{"name": "erp", "status": "stale"}])
    assert assessment["health_score"] == 90  # drift 0, one stale source
    result = health_watch.evaluate_health_watch(
        assessment, tenant_id="t-1", now=datetime(2026, 11, 28, 10, 0, tzinfo=PKT))
    assert sorted(alert.rule_key for alert in result["alerts"]) == ["stale_source", "twin_stale"]


def test_cooldown_suppresses_refire_then_expires():
    assessment = _critical_assessment()
    now = datetime(2026, 11, 28, 10, 0, tzinfo=PKT)
    first = health_watch.evaluate_health_watch(assessment, tenant_id="t-1", now=now)
    assert sorted(alert.rule_key for alert in first["alerts"]) == [
        "health_critical", "health_degraded", "twin_stale"]
    sent_at = {alert.fingerprint: now for alert in first["alerts"]}
    soon = health_watch.evaluate_health_watch(
        assessment, tenant_id="t-1", now=datetime(2026, 11, 28, 10, 10, tzinfo=PKT), sent_at=sent_at)
    assert soon["alerts"] == []
    assert sorted(entry["rule_key"] for entry in soon["suppressed"]) == [
        "health_critical", "health_degraded", "twin_stale"]
    assert all(entry["reason"] == "cooldown" for entry in soon["suppressed"])
    later = health_watch.evaluate_health_watch(
        assessment, tenant_id="t-1", now=datetime(2026, 11, 28, 13, 0, tzinfo=PKT), sent_at=sent_at)
    assert sorted(alert.rule_key for alert in later["alerts"]) == [
        "health_critical", "health_degraded", "twin_stale"]


def test_new_twin_version_refires_after_cooldown():
    assessment = _critical_assessment()
    now = datetime(2026, 11, 28, 10, 0, tzinfo=PKT)
    first = health_watch.evaluate_health_watch(assessment, tenant_id="t-1", now=now)
    sent_at = {alert.fingerprint: now for alert in first["alerts"]}
    changed = _assessment(_report(_snapshot(), _snapshot()), fingerprint="f" * 64)
    changed = {**changed, "health_score": 30, "band": "critical",
               "drift_score": 70, "trust": "stale", "is_first_version": False}
    refired = health_watch.evaluate_health_watch(
        changed, tenant_id="t-1", now=datetime(2026, 11, 28, 10, 10, tzinfo=PKT), sent_at=sent_at)
    assert sorted(alert.rule_key for alert in refired["alerts"]) == [
        "health_critical", "health_degraded", "twin_stale"]


def test_watch_fingerprint_is_stable_and_scoped():
    fp = health_watch.watch_fingerprint("health_critical", "t-1", "a" * 64)
    assert fp == health_watch.watch_fingerprint("health_critical", "t-1", "a" * 64)
    assert fp != health_watch.watch_fingerprint("health_degraded", "t-1", "a" * 64)
    assert fp != health_watch.watch_fingerprint("health_critical", "t-2", "a" * 64)
    assert fp != health_watch.watch_fingerprint("health_critical", "t-1", "b" * 64)
    assert len(fp) == 64


def test_is_quiet_hour_windows():
    assert health_watch.is_quiet_hour(datetime(2026, 11, 28, 22, 0, tzinfo=PKT)) is True
    assert health_watch.is_quiet_hour(datetime(2026, 11, 28, 3, 0, tzinfo=PKT)) is True
    assert health_watch.is_quiet_hour(datetime(2026, 11, 28, 6, 59, tzinfo=PKT)) is True
    assert health_watch.is_quiet_hour(datetime(2026, 11, 28, 7, 0, tzinfo=PKT)) is False
    assert health_watch.is_quiet_hour(datetime(2026, 11, 28, 12, 0, tzinfo=PKT)) is False
    assert health_watch.is_quiet_hour(datetime(2026, 11, 28, 21, 59, tzinfo=PKT)) is False
    # Naive datetimes are read as org time (Asia/Karachi).
    assert health_watch.is_quiet_hour(datetime(2026, 11, 28, 23, 0)) is True
    assert health_watch.is_quiet_hour(datetime(2026, 11, 28, 12, 0)) is False


def test_validate_health_rules_catches_bad_rules():
    assert health_watch.validate_health_rules(health_watch.DEFAULT_RULES) == []
    bad_key = health_watch.HealthRule(key="", severity="warning", title="T", detail="D")
    assert health_watch.validate_health_rules([bad_key])
    bad_severity = health_watch.HealthRule(key="k", severity="info", title="T", detail="D")
    assert health_watch.validate_health_rules([bad_severity])
    bad_health = health_watch.HealthRule(key="k", severity="warning", title="T", detail="D", max_health=101)
    assert health_watch.validate_health_rules([bad_health])
    bad_cooldown = health_watch.HealthRule(key="k", severity="warning", title="T", detail="D", cooldown_seconds=0)
    assert health_watch.validate_health_rules([bad_cooldown])
    dupes = [
        health_watch.HealthRule(key="k", severity="warning", title="T", detail="D"),
        health_watch.HealthRule(key="k", severity="critical", title="T", detail="D"),
    ]
    assert health_watch.validate_health_rules(dupes)


def test_evaluate_rejects_bad_inputs():
    good = _assessment(_report(_snapshot(), _snapshot()), fingerprint="a" * 64)
    with pytest.raises(ValueError, match="fingerprint"):
        health_watch.evaluate_health_watch(
            {**good, "fingerprint": None}, tenant_id="t-1",
            now=datetime(2026, 11, 28, 10, 0, tzinfo=PKT))
    with pytest.raises(ValueError, match="tenant_id"):
        health_watch.evaluate_health_watch(
            good, tenant_id="", now=datetime(2026, 11, 28, 10, 0, tzinfo=PKT))
    with pytest.raises(ValueError, match="Invalid health-watch rules"):
        health_watch.evaluate_health_watch(
            good, tenant_id="t-1", now=datetime(2026, 11, 28, 10, 0, tzinfo=PKT),
            rules=[health_watch.HealthRule(key="k", severity="info", title="T", detail="D")])
    disabled = health_watch.HealthRule(
        key="health_degraded", severity="warning", title="T", detail="D",
        max_health=75, enabled=False)
    result = health_watch.evaluate_health_watch(
        _degraded_assessment(), tenant_id="t-1",
        now=datetime(2026, 11, 28, 10, 0, tzinfo=PKT), rules=[disabled])
    assert result["alerts"] == []


def test_format_watch_message():
    assessment = _critical_assessment()
    result = health_watch.evaluate_health_watch(
        assessment, tenant_id="t-1", now=datetime(2026, 11, 28, 10, 0, tzinfo=PKT))
    message = health_watch.format_watch_message(result["alerts"][0])
    assert message.startswith("[InfraSpine • twin health • critical]")
    assert "Twin health is critical" in message
