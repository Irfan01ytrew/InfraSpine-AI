"""Tests for Step L slice 2: standing forecast / always-on watch.

Pure functions only — no database, no network, no conftest. Run with::

    PYTHONPATH=<worktree> python -m pytest tests/test_predictive_continuity_watch.py -q -p no:cacheprovider --noconftest

All fixtures are synthetic (fake people, systems, amounts).
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.predictive_continuity.watch import (
    DEFAULT_RULES,
    MAX_CRITICAL_IDS,
    ForecastRule,
    evaluate_forecast_watch,
    find_critical_ids,
    fingerprint_changed,
    format_watch_message,
    is_quiet_hour,
    scan_forecasts,
    validate_forecast_rules,
    watch_fingerprint,
)

NOW = datetime(2026, 11, 28, 10, 0, tzinfo=ZoneInfo("Asia/Karachi"))
NOW_UTC = datetime(2026, 11, 28, 5, 0, tzinfo=UTC)
QUIET_NOW = datetime(2026, 11, 28, 23, 30, tzinfo=ZoneInfo("Asia/Karachi"))


def _snapshot():
    """Same twin as the slice-1 fixture: Ayesha approves/owns three
    processes; one ERP system supplied by Acme; one Finance team.

    - pr-1 "Pay suppliers": approver Ayesha, due 2026-12-03, backup
      Bilal (lacks "approvals"), depends on ERP, live invoice batch
      (due 2026-12-02) + live workflow run.
    - pr-2 "Onboard vendor": owner Ayesha, due 2026-12-20, no backup.
    - pr-3 "File tax return": owner Ayesha, sla_days 3, no backup.
    """
    return {
        "entities": [
            {"id": "p-1", "kind": "person", "name": "Ayesha",
             "attributes": {"skills": ["invoicing", "approvals"]}},
            {"id": "p-2", "kind": "person", "name": "Bilal",
             "attributes": {"skills": ["invoicing"]}},
            {"id": "t-1", "kind": "team", "name": "Finance", "attributes": {}},
            {"id": "sys-1", "kind": "system", "name": "ERP", "attributes": {}},
            {"id": "sup-1", "kind": "supplier", "name": "Acme Supplies", "attributes": {}},
            {"id": "pr-1", "kind": "process", "name": "Pay suppliers",
             "attributes": {"required_skills": ["approvals"], "due_at": "2026-12-03"}},
            {"id": "pr-2", "kind": "process", "name": "Onboard vendor",
             "attributes": {"due_at": "2026-12-20"}},
            {"id": "pr-3", "kind": "process", "name": "File tax return",
             "attributes": {"sla_days": 3}},
            {"id": "b-1", "kind": "invoice_batch", "name": "March batch",
             "attributes": {"amount": 1500.00, "currency": "PKR",
                            "status": "awaiting_approval", "due_at": "2026-12-02"}},
            {"id": "wr-1", "kind": "workflow_run", "name": "Invoice run 7",
             "attributes": {"status": "running"}},
        ],
        "relationships": [
            {"id": "e-1", "source_id": "p-1", "target_id": "pr-1", "kind": "approves", "confidence": 1.0},
            {"id": "e-2", "source_id": "p-1", "target_id": "pr-2", "kind": "owns", "confidence": 1.0},
            {"id": "e-3", "source_id": "p-1", "target_id": "pr-3", "kind": "responsible_for", "confidence": 1.0},
            {"id": "e-4", "source_id": "p-2", "target_id": "pr-1", "kind": "backup_for", "confidence": 1.0},
            {"id": "e-5", "source_id": "pr-1", "target_id": "sys-1", "kind": "depends_on", "confidence": 1.0},
            {"id": "e-6", "source_id": "sup-1", "target_id": "sys-1", "kind": "supplies", "confidence": 1.0},
            {"id": "e-7", "source_id": "b-1", "target_id": "pr-1", "kind": "depends_on", "confidence": 1.0},
            {"id": "e-8", "source_id": "wr-1", "target_id": "pr-1", "kind": "depends_on", "confidence": 1.0},
            {"id": "e-9", "source_id": "p-1", "target_id": "t-1", "kind": "member_of", "confidence": 1.0},
            {"id": "e-10", "source_id": "p-2", "target_id": "t-1", "kind": "member_of", "confidence": 1.0},
        ],
        "as_of": "2026-11-28T00:00:00+00:00",
    }


def _entry(scan, root_id, horizon_days):
    return next(
        e for e in scan["forecasts"]
        if e["root_id"] == root_id and e["horizon_days"] == horizon_days
    )


def _alerts_by_rule(result, rule_key):
    return [a for a in result["alerts"] if a.rule_key == rule_key]


# --- Critical set -----------------------------------------------------


def test_find_critical_ids_matches_plan_definition():
    # p-1 holds authority over pr-1 (required skills + money-linked);
    # sys-1 has dependents (pr-1 depends on it); sup-1 supplies sys-1.
    # Bilal only backs up — not critical. The team holds no authority.
    assert find_critical_ids(_snapshot()) == ["p-1", "sup-1", "sys-1"]


def test_find_critical_ids_ignores_standalone_system_and_backup_only_person():
    snapshot = _snapshot()
    snapshot["entities"] += [
        {"id": "sys-2", "kind": "system", "name": "Old printer", "attributes": {}},
        {"id": "p-3", "kind": "person", "name": "Sara", "attributes": {"skills": ["approvals"]}},
    ]
    snapshot["relationships"] += [
        {"id": "e-11", "source_id": "p-3", "target_id": "pr-1", "kind": "backup_for", "confidence": 1.0},
    ]
    assert find_critical_ids(snapshot) == ["p-1", "sup-1", "sys-1"]


# --- scan_forecasts ---------------------------------------------------


def test_scan_covers_every_critical_root_times_standard_horizons():
    scan = scan_forecasts(_snapshot(), now=NOW)
    assert scan["refreshed"] is True
    assert scan["horizons"] == [1, 7, 14, 30]
    assert scan["critical_ids"] == ["p-1", "sup-1", "sys-1"]
    assert scan["counts"] == {
        "critical": 3,
        "horizons": 4,
        "forecasts": 12,
        "with_due_processes": 9,  # every root at 7/14/30 days, none at 1 day
        "with_uncovered": 12,  # every forecast has at least one uncovered process
    }
    assert [(e["root_id"], e["horizon_days"]) for e in scan["forecasts"]] == [
        (root, horizon) for root in ["p-1", "sup-1", "sys-1"] for horizon in [1, 7, 14, 30]
    ]


def test_scan_entries_carry_summary_plus_full_forecast():
    scan = scan_forecasts(_snapshot(), now=NOW)
    entry = _entry(scan, "p-1", 7)
    assert entry["root_kind"] == "person"
    assert entry["root_name"] == "Ayesha"
    assert entry["processes"] == 3
    assert entry["processes_due"] == 2
    assert entry["uncovered"] == 5
    assert entry["money_due"] == (("PKR", 1500.0),)
    assert entry["forecast"]["counts"]["processes"] == entry["processes"]
    assert entry["forecast"]["counts"]["processes_due_within_horizon"] == entry["processes_due"]


def test_horizon_changes_the_scan_answer():
    scan = scan_forecasts(_snapshot(), critical_ids=["p-1"], now=NOW)
    assert _entry(scan, "p-1", 1)["processes_due"] == 0
    assert _entry(scan, "p-1", 7)["processes_due"] == 2
    assert _entry(scan, "p-1", 30)["processes_due"] == 3


def test_system_and_supplier_roots_forecasted():
    scan = scan_forecasts(_snapshot(), critical_ids=["sys-1", "sup-1"], horizons=[7], now=NOW)
    sys_entry = _entry(scan, "sys-1", 7)
    assert sys_entry["processes"] == 1  # pr-1 only
    assert sys_entry["processes_due"] == 1
    assert sys_entry["uncovered"] == 4  # sys-1, pr-1, b-1, wr-1
    assert sys_entry["money_due"] == (("PKR", 1500.0),)
    assert _entry(scan, "sup-1", 7)["processes"] == 1
    assert _entry(scan, "sup-1", 7)["uncovered"] == 5


def test_scan_with_custom_horizons_and_explicit_critical_ids():
    scan = scan_forecasts(_snapshot(), critical_ids=["p-1"], horizons=[7], now=NOW)
    assert scan["horizons"] == [7]
    assert scan["counts"]["forecasts"] == 1


def test_scan_is_deterministic():
    first = scan_forecasts(_snapshot(), now=NOW)
    second = scan_forecasts(_snapshot(), now=NOW)
    assert json.dumps(first, default=str, sort_keys=True) == json.dumps(second, default=str, sort_keys=True)


def test_scan_naive_now_is_read_as_org_time():
    scan = scan_forecasts(_snapshot(), critical_ids=["p-1"], horizons=[7],
                          now=datetime(2026, 11, 28, 10, 0))
    assert scan["scanned_at"] == "2026-11-28T05:00:00+00:00"  # 10:00 PKT == 05:00 UTC


# --- Fingerprint gating ------------------------------------------------


def test_fingerprint_gating_skips_unchanged_twin():
    first = scan_forecasts(_snapshot(), now=NOW)
    second = scan_forecasts(_snapshot(), now=NOW, previous_fingerprint=first["fingerprint"])
    assert second["refreshed"] is False
    assert second["forecasts"] == []
    assert second["counts"]["forecasts"] == 0
    assert second["fingerprint"] == first["fingerprint"]


def test_fingerprint_gating_refreshes_on_twin_change():
    first = scan_forecasts(_snapshot(), now=NOW)
    changed = _snapshot()
    changed["entities"].append(
        {"id": "p-9", "kind": "person", "name": "New hire", "attributes": {}})
    second = scan_forecasts(changed, now=NOW, previous_fingerprint=first["fingerprint"])
    assert second["refreshed"] is True
    assert second["counts"]["forecasts"] == 12


def test_fingerprint_changed_helper():
    from app.predictive_continuity.forecast import snapshot_fingerprint

    snapshot = _snapshot()
    fingerprint = snapshot_fingerprint(snapshot)
    assert fingerprint_changed(None, snapshot) is True
    assert fingerprint_changed(fingerprint, snapshot) is False
    changed = _snapshot()
    changed["entities"] = changed["entities"][:-1]
    assert fingerprint_changed(fingerprint, changed) is True
    with pytest.raises(ValueError):
        fingerprint_changed("", snapshot)
    with pytest.raises(ValueError):
        fingerprint_changed(123, snapshot)


# --- Visibility --------------------------------------------------------


def test_scan_visibility_filters_processes_but_not_visible_money():
    snapshot = _snapshot()
    visible = {e["id"] for e in snapshot["entities"]} - {"pr-1"}
    scan = scan_forecasts(snapshot, critical_ids=["p-1"], horizons=[7],
                          now=NOW, visible_ids=visible)
    entry = _entry(scan, "p-1", 7)
    assert [p["id"] for p in entry["forecast"]["processes"]] == ["pr-3", "pr-2"]
    assert entry["processes"] == 2
    # b-1 is itself visible and due, so its money still counts.
    assert entry["money_due"] == (("PKR", 1500.0),)
    assert "pr-1" not in json.dumps(entry["forecast"]["processes"])


def test_scan_visibility_filters_money_when_batch_hidden():
    snapshot = _snapshot()
    visible = {e["id"] for e in snapshot["entities"]} - {"pr-1", "b-1"}
    scan = scan_forecasts(snapshot, critical_ids=["p-1"], horizons=[7],
                          now=NOW, visible_ids=visible)
    assert _entry(scan, "p-1", 7)["money_due"] == ()


# --- Scan validation ---------------------------------------------------


@pytest.mark.parametrize("bad_ids", [[], ["p-1", "p-1"], ["nope"], ["pr-1"], [""], [7]])
def test_scan_rejects_bad_critical_ids(bad_ids):
    with pytest.raises(ValueError):
        scan_forecasts(_snapshot(), critical_ids=bad_ids, now=NOW)


@pytest.mark.parametrize("bad_horizons", [[0], [-3], [7, 7], [7.5], ["7"], list(range(1, 10))])
def test_scan_rejects_bad_horizons(bad_horizons):
    with pytest.raises(ValueError):
        scan_forecasts(_snapshot(), critical_ids=["p-1"], horizons=bad_horizons, now=NOW)


def test_scan_rejects_too_many_critical_ids():
    snapshot = {"entities": [{"id": f"p-{i}", "kind": "person", "name": f"P{i}",
                             "attributes": {}} for i in range(MAX_CRITICAL_IDS + 1)],
                "relationships": []}
    with pytest.raises(ValueError):
        scan_forecasts(snapshot, critical_ids=[f"p-{i}" for i in range(MAX_CRITICAL_IDS + 1)],
                       horizons=[7], now=NOW)


def test_scan_rejects_bad_previous_fingerprint():
    with pytest.raises(ValueError):
        scan_forecasts(_snapshot(), now=NOW, previous_fingerprint="")
    with pytest.raises(ValueError):
        scan_forecasts(_snapshot(), now=NOW, previous_fingerprint=123)


# --- Watch rules --------------------------------------------------------


def test_default_rules_are_valid():
    assert validate_forecast_rules(DEFAULT_RULES) == []


def test_watch_fires_expected_rules_in_daytime():
    scan = scan_forecasts(_snapshot(), now=NOW)
    result = evaluate_forecast_watch(scan, now=NOW)
    assert len(result["alerts"]) == 25
    assert len(_alerts_by_rule(result, "forecast_critical")) == 9
    assert len(_alerts_by_rule(result, "forecast_due")) == 1
    assert len(_alerts_by_rule(result, "forecast_uncovered")) == 12
    assert len(_alerts_by_rule(result, "supplier_single_point")) == 3
    assert result["suppressed"] == []
    assert result["deferred"] == []
    assert result["fingerprint"] == scan["fingerprint"]
    # Critical first, then most due processes: p-1 at 30 days (3 due).
    first = result["alerts"][0]
    assert first.rule_key == "forecast_critical"
    assert first.root_id == "p-1" and first.horizon_days == 30
    assert first.severity == "critical"
    assert first.processes_due == 3 and first.uncovered == 5


def test_watch_alert_detail_carries_counts_and_money_only():
    scan = scan_forecasts(_snapshot(), critical_ids=["p-1"], horizons=[7], now=NOW)
    result = evaluate_forecast_watch(scan, now=NOW)
    alert = _alerts_by_rule(result, "forecast_critical")[0]
    assert alert.root_name == "Ayesha"
    assert "PKR 1500.0" in alert.detail
    assert "2 process(es) due" in alert.detail
    assert set(alert.__dataclass_fields__) == {
        "rule_key", "severity", "root_id", "root_name", "root_kind",
        "horizon_days", "processes", "processes_due", "uncovered",
        "money_due", "fingerprint", "evaluated_at", "title", "detail",
    }


def test_watch_fingerprint_is_stable_and_specific():
    assert watch_fingerprint("k", "p-1", 7) == watch_fingerprint("k", "p-1", 7)
    assert watch_fingerprint("k", "p-1", 7) != watch_fingerprint("k", "p-1", 14)
    assert watch_fingerprint("k", "p-1", 7) != watch_fingerprint("k", "p-2", 7)
    assert watch_fingerprint("k", "p-1", 7) != watch_fingerprint("other", "p-1", 7)


def test_quiet_hours_only_critical_breaks_through():
    scan = scan_forecasts(_snapshot(), now=QUIET_NOW)
    result = evaluate_forecast_watch(scan, now=QUIET_NOW)
    assert len(result["alerts"]) == 9  # forecast_critical only
    assert all(a.severity == "critical" for a in result["alerts"])
    assert len(result["deferred"]) == 16  # 1 due + 12 uncovered + 3 supplier
    assert all(d["reason"] == "quiet_hours" for d in result["deferred"])
    assert result["suppressed"] == []


def test_is_quiet_hour_boundaries():
    zone = ZoneInfo("Asia/Karachi")
    assert is_quiet_hour(datetime(2026, 11, 28, 23, 30, tzinfo=zone)) is True
    assert is_quiet_hour(datetime(2026, 11, 28, 6, 59, tzinfo=zone)) is True
    assert is_quiet_hour(datetime(2026, 11, 28, 7, 0, tzinfo=zone)) is False
    assert is_quiet_hour(datetime(2026, 11, 28, 10, 0, tzinfo=zone)) is False
    assert is_quiet_hour(datetime(2026, 11, 28, 22, 0, tzinfo=zone)) is True
    assert is_quiet_hour(datetime(2026, 11, 28, 23, 30)) is True  # naive read as PKT


def test_cooldown_suppresses_then_releases():
    scan = scan_forecasts(_snapshot(), critical_ids=["p-1"], horizons=[7], now=NOW)
    fp = watch_fingerprint("forecast_critical", "p-1", 7)
    sent_at = {fp: NOW - timedelta(minutes=30)}
    result = evaluate_forecast_watch(scan, now=NOW, sent_at=sent_at)
    assert _alerts_by_rule(result, "forecast_critical") == []
    assert len(result["suppressed"]) == 1
    assert result["suppressed"][0]["reason"] == "cooldown"
    assert result["suppressed"][0]["root_id"] == "p-1"
    # The other rules for the same forecast are unaffected.
    assert len(result["alerts"]) == 1  # forecast_uncovered only (due=2 < 3)
    sent_at = {fp: NOW - timedelta(hours=2)}
    result = evaluate_forecast_watch(scan, now=NOW, sent_at=sent_at)
    assert len(_alerts_by_rule(result, "forecast_critical")) == 1


def test_disabled_rule_never_fires():
    scan = scan_forecasts(_snapshot(), now=NOW)
    rules = [replace(r, enabled=False) if r.key == "forecast_uncovered" else r
             for r in DEFAULT_RULES]
    result = evaluate_forecast_watch(scan, now=NOW, rules=rules)
    assert _alerts_by_rule(result, "forecast_uncovered") == []
    assert len(result["alerts"]) == 13  # 9 critical + 1 due + 3 supplier


def test_rule_kind_and_horizon_filters():
    scan = scan_forecasts(_snapshot(), now=NOW)
    rules = [
        ForecastRule(key="only-supplier-7", severity="warning", title="t", detail="{name}",
                     min_processes_due=1, kinds=frozenset({"supplier"}),
                     horizons=frozenset({7})),
    ]
    result = evaluate_forecast_watch(scan, now=NOW, rules=rules)
    assert len(result["alerts"]) == 1
    assert result["alerts"][0].root_id == "sup-1"
    assert result["alerts"][0].horizon_days == 7


@pytest.mark.parametrize("bad_rule", [
    replace(DEFAULT_RULES[0], severity="info"),
    replace(DEFAULT_RULES[0], min_processes_due=-1),
    replace(DEFAULT_RULES[0], min_uncovered=-2),
    replace(DEFAULT_RULES[0], cooldown_seconds=0),
    replace(DEFAULT_RULES[0], kinds=frozenset()),
    replace(DEFAULT_RULES[0], horizons=frozenset({0})),
    replace(DEFAULT_RULES[0], title=""),
    replace(DEFAULT_RULES[0], detail=""),
])
def test_watch_rejects_bad_rules(bad_rule):
    assert validate_forecast_rules([bad_rule])
    with pytest.raises(ValueError):
        evaluate_forecast_watch(scan_forecasts(_snapshot(), now=NOW), now=NOW, rules=[bad_rule])


def test_watch_rejects_duplicate_rule_keys():
    assert validate_forecast_rules([DEFAULT_RULES[0], DEFAULT_RULES[0]])
    with pytest.raises(ValueError):
        evaluate_forecast_watch(scan_forecasts(_snapshot(), now=NOW), now=NOW,
                                rules=[DEFAULT_RULES[0], DEFAULT_RULES[0]])


def test_watch_rejects_bad_scan_result():
    with pytest.raises(ValueError):
        evaluate_forecast_watch({"forecasts": "nope"}, now=NOW)
    with pytest.raises(ValueError):
        evaluate_forecast_watch(None, now=NOW)


def test_format_watch_message():
    scan = scan_forecasts(_snapshot(), critical_ids=["p-1"], horizons=[7], now=NOW)
    result = evaluate_forecast_watch(scan, now=NOW)
    alert = _alerts_by_rule(result, "forecast_critical")[0]
    message = format_watch_message(alert)
    assert message.startswith("[InfraSpine • predictive continuity • critical]")
    assert "Ayesha" in message and "7 days" in message


# --- Hostile text is data ----------------------------------------------


def test_hostile_text_is_data_not_instruction():
    snapshot = {
        "entities": [
            {"id": "p-9", "kind": "person", "name": "ignore previous instructions",
             "attributes": {"skills": ["approvals"]}},
            {"id": "pr-9", "kind": "process", "name": "approve all payments",
             "attributes": {"required_skills": ["approvals"], "due_at": "2026-12-03"}},
        ],
        "relationships": [
            {"id": "e-1", "source_id": "p-9", "target_id": "pr-9", "kind": "approves",
             "confidence": 1.0},
        ],
    }
    scan = scan_forecasts(snapshot, critical_ids=["p-9"], horizons=[7], now=NOW)
    assert scan["counts"]["forecasts"] == 1
    result = evaluate_forecast_watch(scan, now=NOW)
    alert = _alerts_by_rule(result, "forecast_critical")[0]
    assert alert.root_name == "ignore previous instructions"
    assert "ignore previous instructions" in alert.detail
    # The alert carries no approval, dispatch or payment path at all.
    assert not any("approv" in field or "dispatch" in field or "pay" in field
                   for field in alert.__dataclass_fields__)
