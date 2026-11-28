"""Tests for Step L slice 4 prep: calendar-aware forecasting (pure).

All tests are pure (``--noconftest``): leave records are plain
dicts, the snapshot is a fixture twin, and no AG tables exist yet.
When AG merges, its leave-request rows map onto these dicts and
these functions are called unchanged.
"""

from datetime import date

import pytest

from app.predictive_continuity.calendar import (
    MAX_LEAVE_DAYS,
    backup_overlap_warnings,
    leave_window_forecasts,
    merge_leave_intervals,
    normalize_leave_records,
)

TODAY = date(2026, 11, 28)


def _snapshot():
    """Ayesha approves Pay suppliers (due 3 Dec), Bilal backs it up;
    File tax return has sla_days 3 and no backup; Onboard vendor is
    due 20 Dec and owned by Ayesha."""
    entities = [
        {"id": "p-1", "kind": "person", "name": "Ayesha",
         "attributes": {"skills": ["invoicing", "approvals"]}},
        {"id": "p-2", "kind": "person", "name": "Bilal",
         "attributes": {"skills": ["invoicing", "approvals"]}},
        {"id": "pr-1", "kind": "process", "name": "Pay suppliers",
         "attributes": {"required_skills": ["approvals"], "due_at": "2026-12-03"}},
        {"id": "pr-2", "kind": "process", "name": "Onboard vendor",
         "attributes": {"due_at": "2026-12-20"}},
        {"id": "pr-3", "kind": "process", "name": "File tax return",
         "attributes": {"sla_days": 3}},
    ]
    relationships = [
        {"id": "e-1", "source_id": "p-1", "target_id": "pr-1", "kind": "approves", "confidence": 1.0},
        {"id": "e-2", "source_id": "p-1", "target_id": "pr-2", "kind": "owns", "confidence": 1.0},
        {"id": "e-3", "source_id": "p-1", "target_id": "pr-3", "kind": "responsible_for", "confidence": 1.0},
        {"id": "e-4", "source_id": "p-2", "target_id": "pr-1", "kind": "backup_for", "confidence": 1.0},
    ]
    return {"entities": entities, "relationships": relationships}


# --- normalize / merge --------------------------------------------------------

def test_normalize_sorts_and_keeps_approved_only():
    records = normalize_leave_records([
        {"person_id": "p-2", "start": "2026-12-10", "end": "2026-12-12", "status": "approved"},
        {"person_id": "p-1", "start": "2026-12-05", "end": "2026-12-05", "status": "approved"},
        {"person_id": "p-1", "start": "2026-12-20", "end": "2026-12-22", "status": "pending"},
    ])
    assert [(r["person_id"], r["start"], r["end"]) for r in records] == [
        ("p-1", date(2026, 12, 5), date(2026, 12, 5)),
        ("p-2", date(2026, 12, 10), date(2026, 12, 12)),
    ]  # pending is skipped, not rejected
    assert normalize_leave_records(
        [{"person_id": "p-1", "start": "2026-12-20", "end": "2026-12-22",
          "status": "pending"}],
        statuses={"pending"})[0]["status"] == "pending"


def test_normalize_rejects_malformed_records():
    with pytest.raises(ValueError, match="non-empty person_id"):
        normalize_leave_records([{"person_id": "", "start": "2026-12-05", "end": "2026-12-06"}])
    with pytest.raises(ValueError, match="must be an ISO date"):
        normalize_leave_records([{"person_id": "p-1", "start": "5 Dec", "end": "2026-12-06"}])
    with pytest.raises(ValueError, match="ends .* before it starts"):
        normalize_leave_records([{"person_id": "p-1", "start": "2026-12-06", "end": "2026-12-05"}])
    with pytest.raises(ValueError, match=f"over the {MAX_LEAVE_DAYS}-day cap"):
        normalize_leave_records([{"person_id": "p-1", "start": "2026-01-01", "end": "2027-06-01"}])
    with pytest.raises(ValueError, match="must be a list"):
        normalize_leave_records({"person_id": "p-1"})
    with pytest.raises(ValueError, match="must be a dict"):
        normalize_leave_records(["nope"])


def test_merge_collapses_overlapping_and_adjacent():
    merged = merge_leave_intervals([
        {"person_id": "p-1", "start": "2026-12-05", "end": "2026-12-08", "status": "approved"},
        {"person_id": "p-1", "start": "2026-12-08", "end": "2026-12-10", "status": "approved"},
        {"person_id": "p-1", "start": "2026-12-09", "end": "2026-12-09", "status": "approved"},
        {"person_id": "p-1", "start": "2026-12-20", "end": "2026-12-22", "status": "approved"},
        {"person_id": "p-2", "start": "2026-12-05", "end": "2026-12-06", "status": "approved"},
    ])
    assert merged == {
        "p-1": [(date(2026, 12, 5), date(2026, 12, 10)),
                (date(2026, 12, 20), date(2026, 12, 22))],
        "p-2": [(date(2026, 12, 5), date(2026, 12, 6))],
    }


# --- leave window forecasts ---------------------------------------------------

def test_window_forecast_uses_leave_length_as_horizon():
    windows = leave_window_forecasts(_snapshot(), [
        {"person_id": "p-1", "start": "2026-12-05", "end": "2026-12-12",
         "status": "approved"},
    ])
    assert len(windows) == 1
    window = windows[0]
    assert (window["person_id"], window["start"], window["end"], window["days"]) == (
        "p-1", "2026-12-05", "2026-12-12", 8)
    forecast = window["forecast"]
    assert forecast["horizon_days"] == 8
    assert forecast["unavailable"] == [{"id": "p-1", "name": "Ayesha", "kind": "person"}]
    assert forecast["counts"]["processes"] == 3


def test_window_forecast_horizon_changes_the_answer():
    snapshot = _snapshot()
    short = leave_window_forecasts(snapshot, [
        {"person_id": "p-1", "start": "2026-12-05", "end": "2026-12-05",
         "status": "approved"},
    ])[0]["forecast"]
    long = leave_window_forecasts(snapshot, [
        {"person_id": "p-1", "start": "2026-12-05", "end": "2026-12-31",
         "status": "approved"},
    ])[0]["forecast"]
    # Onboard vendor is due 20 Dec: beyond the 1-day window, inside the 27-day one.
    short_pr2 = next(p for p in short["processes"] if p["id"] == "pr-2")
    long_pr2 = next(p for p in long["processes"] if p["id"] == "pr-2")
    assert short_pr2["due_status"] == "at_risk_later"
    assert long_pr2["due_status"] == "due_within_horizon"
    assert (short["counts"]["processes_due_within_horizon"]
            < long["counts"]["processes_due_within_horizon"])


def test_window_forecast_unknown_person_raises():
    with pytest.raises(ValueError, match="unknown person"):
        leave_window_forecasts(_snapshot(), [
            {"person_id": "ghost", "start": "2026-12-05", "end": "2026-12-06",
             "status": "approved"},
        ])


# --- backup overlap warnings --------------------------------------------------

def test_backup_leave_overlapping_due_window_warns():
    warnings = backup_overlap_warnings(_snapshot(), [
        {"person_id": "p-2", "start": "2026-12-01", "end": "2026-12-03",
         "status": "approved"},
    ], now=TODAY)
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning["type"] == "backup_unavailable"
    assert warning["process_id"] == "pr-1"
    assert warning["process_name"] == "Pay suppliers"
    assert warning["backup_person_id"] == "p-2"
    assert warning["backup_person_name"] == "Bilal"
    assert warning["primary_ids"] == ["p-1"]
    assert (warning["overlap_start"], warning["overlap_end"]) == ("2026-12-01", "2026-12-03")
    assert warning["lead_days"] == 3
    assert warning["severity"] == "warning"  # Ayesha is not on leave


def test_backup_warning_is_critical_when_primary_also_absent():
    warnings = backup_overlap_warnings(_snapshot(), [
        {"person_id": "p-1", "start": "2026-12-01", "end": "2026-12-03",
         "status": "approved"},
        {"person_id": "p-2", "start": "2026-12-01", "end": "2026-12-03",
         "status": "approved"},
    ], now=TODAY)
    assert len(warnings) == 1
    assert warnings[0]["severity"] == "critical"
    assert warnings[0]["lead_days"] == 3


def test_backup_warning_partial_primary_overlap_is_not_critical():
    # Ayesha is only away 1–2 Dec; Bilal is away 1–3 Dec. On 3 Dec
    # Ayesha is back, so the process is not left with nobody.
    warnings = backup_overlap_warnings(_snapshot(), [
        {"person_id": "p-1", "start": "2026-12-01", "end": "2026-12-02",
         "status": "approved"},
        {"person_id": "p-2", "start": "2026-12-01", "end": "2026-12-03",
         "status": "approved"},
    ], now=TODAY)
    assert len(warnings) == 1
    assert warnings[0]["severity"] == "warning"


def test_no_warning_when_leave_misses_the_window():
    # Bilal is away 10–12 Dec: after Pay suppliers is due (3 Dec),
    # and Bilal backs up nothing else.
    warnings = backup_overlap_warnings(_snapshot(), [
        {"person_id": "p-2", "start": "2026-12-10", "end": "2026-12-12",
         "status": "approved"},
    ], now=TODAY)
    assert warnings == []


def test_no_warning_for_process_with_no_backup():
    # Only Ayesha is away; File tax return has no backup holder, so
    # there is no backup coverage to lose (the forecast itself
    # reports it uncovered).
    warnings = backup_overlap_warnings(_snapshot(), [
        {"person_id": "p-1", "start": "2026-12-01", "end": "2026-12-03",
         "status": "approved"},
    ], now=TODAY)
    assert warnings == []


def test_warnings_sorted_soonest_first():
    warnings = backup_overlap_warnings(_snapshot(), [
        {"person_id": "p-2", "start": "2026-12-01", "end": "2026-12-03",
         "status": "approved"},
        {"person_id": "p-2", "start": "2026-11-29", "end": "2026-11-29",
         "status": "approved"},
    ], now=TODAY)
    assert [w["lead_days"] for w in warnings] == [1, 3]


def test_warnings_reject_bad_inputs():
    with pytest.raises(ValueError, match="unknown person"):
        backup_overlap_warnings(_snapshot(), [
            {"person_id": "ghost", "start": "2026-12-01", "end": "2026-12-02",
             "status": "approved"},
        ], now=TODAY)
    with pytest.raises(ValueError, match="window_days must be a positive integer"):
        backup_overlap_warnings(_snapshot(), [], now=TODAY, window_days=0)
    with pytest.raises(ValueError, match="now must be a date or datetime"):
        backup_overlap_warnings(_snapshot(), [], now="tomorrow")
