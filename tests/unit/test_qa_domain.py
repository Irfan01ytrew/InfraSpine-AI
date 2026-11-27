"""QA self-tests include deliberate bad evidence and a real failing child test."""
import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import pytest
from app.qa.catalog import requirements, SUITES
from app.qa.domain import digest, junit_results, outcome, performance_comparison, redact, release_report

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)
POLICY = {"version": 1, "max_age_hours": 24}

def requirement(mandatory=True):
    return {"id": "12.01", "implemented": True, "mandatory": mandatory}

def run(status="passed"):
    return {"id": "run-1", "suite_id": "qa-unit", "source_digest": "source", "created_at": NOW.isoformat(),
            "verifies": ["12.01"], "status": status, "evidence_sha256": "a" * 64, "results": [{"status": status}]}

def release(rows, reqs=None):
    return release_report(reqs or [requirement()], rows, source_digest="source", policy=POLICY, now=NOW)

@pytest.mark.parametrize("tag,status", [("", "passed"), ('<failure message="incorrect"/>', "failed"),
                                        ('<error message="database offline"/>', "blocked"), ('<skipped/>', "blocked")])
def test_real_junit_statuses(tag, status):
    rows = junit_results(f'<testsuite><testcase classname="test" name="case" time=".25">{tag}</testcase></testsuite>'.encode())
    assert rows[0]["status"] == status
    assert rows[0]["duration_ms"] == 250

def test_missing_collection_never_passes():
    rows = junit_results(b"<testsuites/>")
    assert outcome(rows, 0) == "blocked"
    assert outcome([], 0) == "blocked"
    assert outcome([{"status": "passed"}], 2) == "blocked"
    assert outcome([{"status": "unverified"}], 0) == "unverified"

@pytest.mark.parametrize("body", [b'<!DOCTYPE a [<!ENTITY x "abc">]><testsuite/>', b"<x/>",
                                b'<testsuite><testcase time="nan"/></testsuite>', b'<testsuite><testcase time="-1"/></testsuite>'])
def test_invalid_evidence_rejected(body):
    with pytest.raises(ValueError):
        junit_results(body)

def test_missing_and_blocked_mandatory_workflows_block_release():
    assert release([])["decision"] == "NO-GO"
    for status in ("failed", "blocked", "unverified", "running", "cancelled", "queued"):
        assert release([run(status)])["decision"] == "NO-GO"

def test_go_requires_all_current_evidence():
    report = release([run()])
    assert report["decision"] == "GO"
    assert report["matrix"][0]["status"] == "Verified"
    optional = {"id": "12.02", "implemented": True, "mandatory": False}
    assert release([run()], [requirement(), optional])["decision"] == "CONDITIONAL GO"

@pytest.mark.parametrize("change", [{"source_digest": "old"}, {"evidence_sha256": None}, {"results": []},
                                   {"created_at": (NOW - timedelta(days=2)).isoformat()},
                                   {"created_at": (NOW + timedelta(days=1)).isoformat()}])
def test_stale_or_missing_evidence_is_blocked(change):
    candidate = {**run(), **change}
    assert release([candidate])["decision"] == "NO-GO"
    assert release([candidate])["matrix"][0]["status"] == "Blocked"

def test_recent_failure_supersedes_old_pass():
    old = {**run(), "created_at": (NOW - timedelta(hours=1)).isoformat()}
    assert release([old, run("failed")])["matrix"][0]["status"] == "Failed"

def test_unmapped_regression_blocks_release():
    bad = {**run("failed"), "id": "bad", "suite_id": "security", "verifies": []}
    assert release([run(), bad])["failed_suites"] == ["bad"]

def test_snapshot_is_stable_and_content_bound():
    assert release([run()])["snapshot_sha256"] == release([run()])["snapshot_sha256"]
    assert release([run()])["snapshot_sha256"] != release([run("failed")])["snapshot_sha256"]

def test_measured_performance_regression_against_stored_baseline(tmp_path):
    baseline = {"dataset_sha256": "abc", "model": "fixture-v1", "tools_sha256": "none", "environment": "cpu-test",
                "scorer_version": "v1", "metric": "p95", "unit": "ms", "value": 100}
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline))
    stored = json.loads(path.read_text())
    assert performance_comparison(stored, {**baseline, "value": 140})["status"] == "failed"
    assert performance_comparison(stored, {**baseline, "value": 110})["status"] == "passed"
    assert performance_comparison(stored, {**baseline, "model": "other"})["status"] == "blocked"
    assert performance_comparison(stored, {**baseline, "value": float("nan")})["status"] == "blocked"
    # This verifies the detector. Synthetic samples are not a production performance claim.

def test_injected_defect_detected_then_fixed_with_real_pytest(tmp_path):
    code = tmp_path / "test_mutant.py"
    report = tmp_path / "result.xml"
    code.write_text("def test_total():\n    assert 2 + 2 == 5\n")
    command = [sys.executable, "-m", "pytest", "--noconftest", "-q", "-c", str(tmp_path / "empty.ini"), f"--junitxml={report}", str(code)]
    (tmp_path / "empty.ini").write_text("[pytest]\n")
    # The fixed file has the same size as the mutant and is often rewritten within the same mtime second, so a
    # cached .pyc from the first run could be reused. Disable bytecode caching so the second run sees the fix.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    first = subprocess.run(command, cwd=tmp_path, capture_output=True, timeout=30, env=env)
    assert first.returncode == 1
    assert outcome(junit_results(report.read_bytes()), first.returncode) == "failed"
    code.write_text("def test_total():\n    assert 2 + 2 == 4\n")
    second = subprocess.run(command, cwd=tmp_path, capture_output=True, timeout=30, env=env)
    assert second.returncode == 0
    assert outcome(junit_results(report.read_bytes()), second.returncode) == "passed"

def test_all_twelve_modules_have_unique_traceable_requirements():
    rows = requirements()
    assert {r["module"] for r in rows} == set(range(1, 13))
    assert len({r["id"] for r in rows}) == len(rows)
    assert all(r["mandatory"] and r["source"] for r in rows)
    assert len([r for r in rows if r["module"] == 12]) == 15
    assert all(set(s["verifies"]) <= {r["id"] for r in rows} for s in SUITES.values())

def test_redaction_excludes_credentials_and_output():
    secret = "sensitive-value"
    assert secret not in redact(f"password={secret} postgresql://person:{secret}@localhost/test")
    assert secret not in str(junit_results(f'<testsuite><testcase><system-out>{secret}</system-out></testcase></testsuite>'.encode()))

def test_digest_rejects_nan():
    with pytest.raises(ValueError):
        digest({"metric": float("nan")})
