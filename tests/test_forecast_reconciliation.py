"""Tests for Step L slice 7: drill reconciliation (pure layer).

No database, no conftest: run with ``--noconftest``. Proves the
forecast-vs-drill comparison (matched / over-predicted /
missed), the Step G difference grading, fingerprint stability and
dedupe, the 500-finding cap, fail-closed validation, and the
miss-penalty math that feeds back into forecast trust.
"""

import uuid

import pytest

from app.predictive_continuity import reconciliation as rec

FP = "a" * 64
FP2 = "b" * 64
RUN = str(uuid.uuid4())
TENANT = uuid.uuid4()

PR1 = str(uuid.uuid4())
PR2 = str(uuid.uuid4())
PR3 = str(uuid.uuid4())
SYS1 = str(uuid.uuid4())
B1 = str(uuid.uuid4())
EXTRA = str(uuid.uuid4())


def _forecast(*, fingerprint=FP):
    return {
        "fingerprint": fingerprint,
        "processes": [
            {"id": PR1, "name": "Pay suppliers", "due_status": "due_within_horizon",
             "money_at_risk": {"totals": {"PKR": 1500.0}, "batch_count": 1, "payment_count": 0}},
            {"id": PR2, "name": "Onboard vendor", "due_status": "at_risk_later",
             "money_at_risk": {"totals": {}, "batch_count": 0, "payment_count": 0}},
            {"id": PR3, "name": "File tax return", "due_status": "due_within_horizon",
             "money_at_risk": {"totals": {}, "batch_count": 0, "payment_count": 0}},
        ],
        "affected": [
            {"entity": {"id": PR1, "kind": "process", "name": "Pay suppliers"},
             "due_status": "due_within_horizon"},
            {"entity": {"id": PR2, "kind": "process", "name": "Onboard vendor"},
             "due_status": "at_risk_later"},
            {"entity": {"id": PR3, "kind": "process", "name": "File tax return"},
             "due_status": "due_within_horizon"},
            {"entity": {"id": SYS1, "kind": "system", "name": "ERP"},
             "due_status": "no_deadline"},
            {"entity": {"id": B1, "kind": "invoice_batch", "name": "March batch"},
             "due_status": "due_within_horizon"},
        ],
    }


def _comparison(*, recovered, differences=(), run_id=RUN, affected=None):
    recovered = list(recovered)
    affected = list(recovered) if affected is None else list(affected)
    return {
        "run_id": run_id,
        "scenario_id": str(uuid.uuid4()),
        "drill": {
            "affected_ids": affected,
            "recovered_ids": recovered,
            "recovery_minutes": {}, "rto_minutes": {}, "mtpd_minutes": {},
            "data_loss_minutes": {}, "rpo_minutes": {},
        },
        "simulation": {"affected_ids": affected, "forecast": {}},
        "differences": list(differences),
        "matches": not differences,
        "affected": len(affected), "recovered": len(recovered),
        "simulated": len(affected),
    }


ALL_AFFECTED = [PR1, PR2, PR3, SYS1, B1]


def _difference(code, resource_id):
    return {"code": code, "resource_id": resource_id,
            "detail": f"Difference {code} for {resource_id}."}


# --- matches ----------------------------------------------------------------

def test_match_when_drill_recovers_everything_forecast_named():
    report = rec.reconcile(_forecast(), _comparison(recovered=ALL_AFFECTED),
                           tenant_id=TENANT)
    assert report["matches"] is True
    assert report["forecast_matches"] is True
    assert report["g_matches"] is True
    assert report["findings"] == []
    assert report["counts"] == {"total": 0, "critical": 0, "warning": 0,
                                "info": 0, "unacknowledged": 0}
    assert report["worst_severity"] is None
    assert report["truncated"] is False
    assert "match" in rec.reconciliation_summary(report)


def test_forecasted_not_drilled_is_critical_for_due_process():
    report = rec.reconcile(
        _forecast(), _comparison(recovered=[PR2, PR3, SYS1, B1]), tenant_id=TENANT)
    assert report["matches"] is False
    assert report["forecast_matches"] is False
    assert len(report["findings"]) == 1
    finding = report["findings"][0]
    assert finding["code"] == "forecasted_not_drilled"
    assert finding["resource_id"] == PR1
    assert finding["severity"] == "critical"
    assert finding["source"] == "forecast"
    assert finding["money"] == {"PKR": 1500.0}
    assert "1500" not in finding["detail"]
    assert report["counts"]["critical"] == 1
    assert report["worst_severity"] == "critical"


def test_forecasted_not_drilled_is_warning_for_later_process_and_system():
    report = rec.reconcile(
        _forecast(), _comparison(recovered=[PR1, PR3, B1]), tenant_id=TENANT)
    by_id = {item["resource_id"]: item for item in report["findings"]}
    assert set(by_id) == {PR2, SYS1}
    assert by_id[PR2]["severity"] == "warning"
    assert by_id[PR2]["money"] is None
    assert by_id[SYS1]["severity"] == "warning"
    assert report["counts"]["warning"] == 2


def test_drilled_not_forecasted_is_a_miss():
    report = rec.reconcile(
        _forecast(), _comparison(recovered=ALL_AFFECTED + [EXTRA]),
        tenant_id=TENANT)
    assert len(report["findings"]) == 1
    finding = report["findings"][0]
    assert finding["code"] == "drilled_not_forecasted"
    assert finding["resource_id"] == EXTRA
    assert finding["severity"] == "warning"
    assert finding["money"] is None
    assert report["counts"]["warning"] == 1


# --- Step G differences ------------------------------------------------------

def test_g_differences_are_graded_and_kept_verbatim():
    differences = [
        _difference("rto_missed_in_drill", SYS1),
        _difference("drill_met_rto_simulation_missed", PR1),
        _difference("recovered_not_in_simulation", PR2),
    ]
    report = rec.reconcile(
        _forecast(), _comparison(recovered=ALL_AFFECTED, differences=differences),
        tenant_id=TENANT)
    by_code = {item["code"]: item for item in report["findings"]}
    assert by_code["rto_missed_in_drill"]["severity"] == "critical"
    assert by_code["rto_missed_in_drill"]["source"] == "continuity"
    assert by_code["drill_met_rto_simulation_missed"]["severity"] == "info"
    assert by_code["recovered_not_in_simulation"]["severity"] == "warning"
    assert report["counts"] == {"total": 3, "critical": 1, "warning": 1,
                                "info": 1, "unacknowledged": 3}
    assert report["g_matches"] is False
    assert report["forecast_matches"] is True
    assert "3 finding(s)" in rec.reconciliation_summary(report)


def test_g_unknown_code_is_a_value_error():
    with pytest.raises(ValueError, match="unknown Step G difference code"):
        rec.reconcile(
            _forecast(),
            _comparison(recovered=ALL_AFFECTED,
                        differences=[_difference("made_up_code", SYS1)]),
            tenant_id=TENANT)


def test_g_run_id_mismatch_is_a_value_error():
    with pytest.raises(ValueError, match="is for drill run"):
        rec.reconcile(
            _forecast(),
            _comparison(recovered=ALL_AFFECTED, run_id=str(uuid.uuid4())),
            tenant_id=TENANT, drill_run_id=RUN)


def test_duplicate_g_differences_dedupe_to_one_finding():
    difference = _difference("rto_missed_in_drill", SYS1)
    report = rec.reconcile(
        _forecast(),
        _comparison(recovered=ALL_AFFECTED, differences=[difference, dict(difference)]),
        tenant_id=TENANT)
    assert len(report["findings"]) == 1


# --- fingerprints ------------------------------------------------------------

def test_fingerprint_is_stable_and_scoped_to_forecast_and_run():
    first = rec.reconcile(_forecast(), _comparison(recovered=[PR2, PR3, SYS1, B1]),
                          tenant_id=TENANT)
    second = rec.reconcile(_forecast(), _comparison(recovered=[PR2, PR3, SYS1, B1]),
                           tenant_id=TENANT)
    assert first["findings"][0]["fingerprint"] == second["findings"][0]["fingerprint"]
    other_forecast = rec.reconcile(
        _forecast(fingerprint=FP2),
        _comparison(recovered=[PR2, PR3, SYS1, B1]), tenant_id=TENANT)
    assert (other_forecast["findings"][0]["fingerprint"]
            != first["findings"][0]["fingerprint"])
    other_run = rec.reconcile(
        _forecast(), _comparison(recovered=[PR2, PR3, SYS1, B1], run_id=str(uuid.uuid4())),
        tenant_id=TENANT)
    assert (other_run["findings"][0]["fingerprint"]
            != first["findings"][0]["fingerprint"])


def test_finding_fingerprint_rejects_a_bad_forecast_fingerprint():
    with pytest.raises(ValueError, match="64-hex"):
        rec.finding_fingerprint(TENANT, RUN, "not-a-fingerprint", "code", PR1)


# --- cap ---------------------------------------------------------------------

def test_more_than_500_findings_truncates():
    forecast = _forecast()
    forecast["affected"] = [
        {"entity": {"id": str(uuid.uuid4()), "kind": "process", "name": f"p-{n}"},
         "due_status": "no_deadline"}
        for n in range(600)
    ]
    forecast["processes"] = []
    report = rec.reconcile(
        forecast, _comparison(recovered=[]), tenant_id=TENANT)
    assert report["truncated"] is True
    assert len(report["findings"]) == rec.MAX_FINDINGS
    assert report["counts"]["total"] == rec.MAX_FINDINGS


# --- validation ---------------------------------------------------------------

def test_missing_run_id_is_a_value_error():
    comparison = _comparison(recovered=ALL_AFFECTED)
    del comparison["run_id"]
    with pytest.raises(ValueError, match="drill_run_id"):
        rec.reconcile(_forecast(), comparison, tenant_id=TENANT)


def test_forecast_without_processes_is_a_value_error():
    forecast = _forecast()
    del forecast["processes"]
    with pytest.raises(ValueError, match="'processes'"):
        rec.reconcile(forecast, _comparison(recovered=ALL_AFFECTED), tenant_id=TENANT)


def test_forecast_with_bad_fingerprint_is_a_value_error():
    with pytest.raises(ValueError, match="64-hex"):
        rec.reconcile(_forecast(fingerprint="xyz"),
                      _comparison(recovered=ALL_AFFECTED), tenant_id=TENANT)


def test_comparison_without_drill_is_a_value_error():
    comparison = _comparison(recovered=ALL_AFFECTED)
    del comparison["drill"]
    with pytest.raises(ValueError, match="'drill'"):
        rec.reconcile(_forecast(), comparison, tenant_id=TENANT)


def test_comparison_without_differences_is_a_value_error():
    comparison = _comparison(recovered=ALL_AFFECTED)
    del comparison["differences"]
    with pytest.raises(ValueError, match="'differences'"):
        rec.reconcile(_forecast(), comparison, tenant_id=TENANT)


def test_non_boolean_g_matches_is_a_value_error():
    comparison = _comparison(recovered=ALL_AFFECTED)
    comparison["matches"] = "yes"
    with pytest.raises(ValueError, match="'matches' must be a boolean"):
        rec.reconcile(_forecast(), comparison, tenant_id=TENANT)


# --- miss penalty -------------------------------------------------------------

def test_miss_penalty_weights():
    findings = [
        {"code": "drilled_not_forecasted", "severity": "warning"},
        {"code": "forecasted_not_drilled", "severity": "critical"},
        {"code": "forecasted_not_drilled", "severity": "warning"},
        {"code": "drill_met_rto_simulation_missed", "severity": "info"},
    ]
    assert rec.miss_penalty(findings) == 15 + 10 + 5 + 0
    assert rec.miss_penalty([]) == 0


def test_apply_miss_penalty_raises_never_lowers_and_caps():
    findings = [{"code": "drilled_not_forecasted", "severity": "warning"}]
    assert rec.apply_miss_penalty(None, findings) == 15
    assert rec.apply_miss_penalty(40, findings) == 55
    assert rec.apply_miss_penalty(95, findings) == 100
    assert rec.apply_miss_penalty(40, []) == 40
    with pytest.raises(ValueError, match="out of range"):
        rec.apply_miss_penalty(101, findings)
    with pytest.raises(ValueError, match="must be an int"):
        rec.apply_miss_penalty("40", findings)


def test_explicit_drill_run_id_must_agree_with_comparison():
    other_run = str(uuid.uuid4())
    with pytest.raises(ValueError, match="is for drill run"):
        rec.reconcile(
            _forecast(), _comparison(recovered=ALL_AFFECTED, run_id=other_run),
            tenant_id=TENANT, drill_run_id=RUN)
    report = rec.reconcile(
        _forecast(), _comparison(recovered=ALL_AFFECTED, run_id=RUN),
        tenant_id=TENANT, drill_run_id=RUN)
    assert report["drill_run_id"] == RUN
