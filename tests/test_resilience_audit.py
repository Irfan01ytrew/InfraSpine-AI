"""Tests for Step AH slice 6 (pure part): app/resilience/audit.py.

Pure-function tests over synthetic audit rows — no database rows, no
live data, no secrets. The redaction behaviour is the point: a secret
that slips into a detail must never reach the stored trail or the export.
"""

from app.recovery.policy import redact
from app.resilience.audit import build_audit_export, export_csv


def _row(action, outcome="success", **details):
    return {
        "id": 1,
        "created_at": "2026-11-28T01:00:00+00:00",
        "actor_email": "admin@example.com",
        "action": action,
        "target": "plan-1",
        "outcome": outcome,
        "details": details,
    }


def test_redact_strips_secret_keys_and_values():
    details = {
        "plan_id": "plan-1",
        "api_token": "sk-live-should-never-appear",
        "password": "hunter2",
        "nested": {"master_key": "abc123", "note": "fine"},
        "payee_account": "PK99ATTACKER",
    }
    clean = redact(details)
    assert clean["plan_id"] == "plan-1"
    assert clean["api_token"] == "[redacted]"
    assert clean["password"] == "[redacted]"
    assert clean["nested"]["master_key"] == "[redacted]"
    assert clean["nested"]["note"] == "fine"
    assert "sk-live" not in repr(clean)
    assert "hunter2" not in repr(clean)


def test_build_audit_export_counts_and_flattens():
    rows = [
        _row("resilience.plan_started", scope_id="scope-1", run_id="run-1"),
        _row("resilience.plan_step_performed", scope_id="scope-1", step_id="s1"),
        _row("resilience.plan_step_performed", outcome="failure", scope_id="scope-1", step_id="s2"),
    ]
    export = build_audit_export(rows, generated_at="2026-11-28T02:00:00+00:00")
    assert export["generated_at"] == "2026-11-28T02:00:00+00:00"
    assert export["total"] == 3
    assert export["by_action"] == {
        "resilience.plan_started": 1,
        "resilience.plan_step_performed": 2,
    }
    assert export["by_outcome"] == {"success": 2, "failure": 1}
    assert export["columns"] == ["created_at", "actor_email", "action", "target", "outcome", "details"]
    assert export["rows"][0]["action"] == "resilience.plan_started"
    assert "run_id=run-1" in export["rows"][0]["details"]
    assert "scope_id=scope-1" in export["rows"][0]["details"]


def test_build_audit_export_redacts_secrets_in_details():
    rows = [_row("resilience.plan_started", token="sk-live-123", plan_id="plan-1")]
    export = build_audit_export(rows)
    assert "sk-live-123" not in repr(export)
    assert "token=[redacted]" in export["rows"][0]["details"]


def test_build_audit_export_blanks_financial_identifiers():
    rows = [_row("resilience.plan_started", payee_account="PK99ATTACKER", plan_id="plan-1")]
    export = build_audit_export(rows)
    assert "PK99ATTACKER" not in repr(export)
    assert "payee_account=[redacted]" in export["rows"][0]["details"]


def test_export_csv_is_parseable():
    rows = [
        _row("resilience.plan_started", scope_id="scope-1"),
        _row("resilience.drill_compared", scope_id="scope-1", matches=False),
    ]
    text = export_csv(build_audit_export(rows))
    lines = text.strip().splitlines()
    assert lines[0] == "created_at,actor_email,action,target,outcome,details"
    assert len(lines) == 3
    assert "resilience.plan_started" in lines[1]
    assert "resilience.drill_compared" in lines[2]


def test_build_audit_export_empty():
    export = build_audit_export([])
    assert export["total"] == 0
    assert export["by_action"] == {}
    assert export["rows"] == []
