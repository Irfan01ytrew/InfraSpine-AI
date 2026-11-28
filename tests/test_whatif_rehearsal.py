"""Tests for the rehearsal runner (Step AL slice 5).

Pure-function tests: no database, no network. Run with
``--noconftest`` in a minimal venv because the repo conftest needs
the full app stack and this module is pure stdlib.
"""

import copy

import pytest

from app.whatif import rehearsal
from app.whatif.simulator import build_scenario, simulate

USER = "11111111-1111-1111-1111-111111111111"
USER2 = "22222222-2222-2222-2222-222222222222"
NOW = "2026-11-28T04:00:00+00:00"


def snapshot():
    return {
        "entities": [
            {"id": "s-erp", "kind": "system", "name": "ERP",
             "attributes": {"restorable_from_backup": True}},
            {"id": "p-invoicing", "kind": "process", "name": "Invoicing",
             "attributes": {"required_skills": ["approvals"]}},
            {"id": "m-finance", "kind": "person", "name": "Finance Manager",
             "attributes": {"skills": ["approvals"]}},
        ],
        "relationships": [
            {"id": "e-1", "source_id": "p-invoicing", "target_id": "s-erp",
             "kind": "depends_on", "confidence": 1.0},
            {"id": "e-2", "source_id": "m-finance", "target_id": "p-invoicing",
             "kind": "approves", "confidence": 1.0},
        ],
    }


def result(rehearse=True):
    return simulate(snapshot(), build_scenario("server_down", ["s-erp"]), rehearse=rehearse)


def started(**kwargs):
    kwargs.setdefault("now", NOW)
    return rehearsal.start_rehearsal(result(), rehearsed_by=USER, **kwargs)


def test_start_from_rehearsal_block():
    record = started(rehearsed_by_name="Ayesha")
    assert record["status"] == "in_progress"
    assert record["dry_run"] is True
    assert record["side_effects"] == []
    assert record["practice_only"] is True
    assert record["started_by"] == USER
    assert record["started_by_name"] == "Ayesha"
    assert record["started_at"] == NOW
    assert record["completed_at"] is None
    assert record["scenario"]["kind"] == "system_outage"
    assert record["scenario"]["seed_ids"] == ["s-erp"]
    assert [s["number"] for s in record["steps"]] == [1, 2]
    assert all(s["status"] == "pending" and s["practice_only"] is True for s in record["steps"])
    assert rehearsal.validate_rehearsal(record) is True


def test_start_without_rehearsal_block_rebuilds_from_waves():
    record = rehearsal.start_rehearsal(result(rehearse=False), rehearsed_by=USER, now=NOW)
    assert len(record["steps"]) == 2
    assert record["steps"][0]["action"] == "restore_from_backup"
    assert rehearsal.validate_rehearsal(record) is True


def test_start_rejects_bad_inputs():
    with pytest.raises(ValueError):
        rehearsal.start_rehearsal({"scenario": {"kind": "nope"}}, rehearsed_by=USER)
    with pytest.raises(ValueError):
        rehearsal.start_rehearsal(result(), rehearsed_by="")
    with pytest.raises(ValueError):
        rehearsal.start_rehearsal(result(), rehearsed_by=USER, title="x")


def test_check_off_records_who_when_and_finding():
    record = started()
    updated = rehearsal.check_off_step(record, 1, by=USER, by_name="Ayesha",
                                       finding="Backup person was on leave.", now=NOW)
    assert record["steps"][0]["status"] == "pending"  # input not mutated
    step = updated["steps"][0]
    assert step["status"] == "done"
    assert step["checked_off_by"] == USER
    assert step["checked_off_by_name"] == "Ayesha"
    assert step["checked_off_at"] == NOW
    assert step["finding"] == "Backup person was on leave."
    assert updated["findings"] == ["Backup person was on leave."]
    assert updated["steps"][1]["status"] == "pending"
    assert rehearsal.validate_rehearsal(updated) is True


def test_check_off_errors():
    record = started()
    with pytest.raises(ValueError):
        rehearsal.check_off_step(record, 99, by=USER)
    done = rehearsal.check_off_step(record, 1, by=USER, now=NOW)
    with pytest.raises(ValueError):
        rehearsal.check_off_step(done, 1, by=USER)
    with pytest.raises(ValueError):
        rehearsal.check_off_step(record, 1, by="")
    with pytest.raises(ValueError):
        rehearsal.check_off_step(record, 1, by=USER, finding="")


def test_skip_needs_reason_and_records_it():
    record = started()
    with pytest.raises(ValueError):
        rehearsal.skip_step(record, 1, by=USER, reason="n/a")
    updated = rehearsal.skip_step(record, 1, by=USER, reason="ERP backup window is tonight.", now=NOW)
    step = updated["steps"][0]
    assert step["status"] == "skipped"
    assert step["skipped_reason"] == "ERP backup window is tonight."
    assert step["checked_off_by"] == USER
    with pytest.raises(ValueError):
        rehearsal.skip_step(updated, 1, by=USER, reason="again")


def test_complete_requires_no_pending_steps():
    record = started()
    with pytest.raises(ValueError):
        rehearsal.complete_rehearsal(record, by=USER)
    done = rehearsal.check_off_step(record, 1, by=USER, now=NOW)
    done = rehearsal.skip_step(done, 2, by=USER2, reason="Process owner is travelling.", now=NOW)
    completed = rehearsal.complete_rehearsal(done, by=USER, summary="Two gaps found.", now=NOW)
    assert completed["status"] == "completed"
    assert completed["completed_at"] == NOW
    assert completed["completed_by"] == USER
    assert completed["summary"] == "Two gaps found."
    assert rehearsal.validate_rehearsal(completed) is True
    with pytest.raises(ValueError):
        rehearsal.check_off_step(completed, 1, by=USER)
    with pytest.raises(ValueError):
        rehearsal.complete_rehearsal(completed, by=USER)


def test_validate_catches_tampered_records():
    record = started()
    tampered = copy.deepcopy(record)
    tampered["dry_run"] = False
    with pytest.raises(ValueError):
        rehearsal.validate_rehearsal(tampered)
    tampered = copy.deepcopy(record)
    tampered["side_effects"] = ["pay"]
    with pytest.raises(ValueError):
        rehearsal.validate_rehearsal(tampered)
    tampered = copy.deepcopy(record)
    tampered["steps"][0]["practice_only"] = False
    with pytest.raises(ValueError):
        rehearsal.validate_rehearsal(tampered)
    tampered = copy.deepcopy(record)
    tampered["steps"][0]["status"] = "done"  # no checked_off_by/at
    with pytest.raises(ValueError):
        rehearsal.validate_rehearsal(tampered)
    tampered = copy.deepcopy(record)
    tampered["steps"][0]["number"] = 2  # duplicate numbering
    with pytest.raises(ValueError):
        rehearsal.validate_rehearsal(tampered)


def test_summary_counts_and_findings():
    record = started()
    record = rehearsal.check_off_step(record, 1, by=USER, finding="First gap.", now=NOW)
    record = rehearsal.skip_step(record, 2, by=USER, reason="Owner away.", now=NOW)
    summary = rehearsal.rehearsal_summary(record)
    assert summary["total_steps"] == 2
    assert summary["done_count"] == 1
    assert summary["skipped_count"] == 1
    assert summary["pending_count"] == 0
    assert summary["skipped_reasons"] == ["Owner away."]
    assert summary["findings"] == ["First gap."]
    assert summary["dry_run"] is True
    assert summary["side_effects"] == []
