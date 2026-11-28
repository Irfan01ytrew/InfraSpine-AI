"""Step AM slice 3 — tests for app/foundry/preview.py.

Pure-function tests need no database or network and run with
`python -m pytest tests/test_foundry_preview.py --noconftest`. The two
`sandbox` tests use a real disposable PostgreSQL cluster via
`app/qa/sandbox.py` (127.0.0.1 only, seed data only) and skip when the
PostgreSQL 16 binaries are not present.
"""

import dataclasses
import os
import pwd
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.foundry.generate import generate_app
from app.foundry.preview import (
    SandboxVerification,
    TestResult,
    apply_gate,
    approval_offered,
    build_preview,
    check_generated_consistency,
    decide_gate,
    evaluate_test_results,
    required_test_ids,
    verify_in_sandbox,
)
from app.foundry.spec import validate_spec

PG_BIN = Path("/usr/lib/postgresql/16/bin")
pytestmark = pytest.mark.usefixtures("tmpdir_home")


@pytest.fixture
def tmpdir_home(monkeypatch):
    """Keep disposable clusters off the 512 MB /tmp tmpfs (home disk)."""
    monkeypatch.setattr(tempfile, "tempdir", "/home/hatch/tmp")
    return None


def _valid_spec(**overrides):
    spec = {
        "name": "Leave Tracker",
        "purpose": "Let employees request leave and managers approve it.",
        "pages": [
            {"name": "Requests", "path": "/requests",
             "endpoints": ["list_requests"]},
            {"name": "New request", "path": "/requests/new",
             "endpoints": ["create_request"]},
        ],
        "endpoints": [
            {"name": "list_requests", "method": "GET",
             "path": "/api/requests", "table": "leave_requests"},
            {"name": "create_request", "method": "POST",
             "path": "/api/requests", "table": "leave_requests"},
        ],
        "tables": [
            {"name": "leave_requests",
             "columns": [{"name": "id", "type": "uuid"},
                         {"name": "status", "type": "string"},
                         {"name": "days", "type": "integer"}]},
        ],
    }
    spec.update(overrides)
    return spec


def _setup(spec=None):
    spec = _valid_spec() if spec is None else spec
    validation = validate_spec(spec)
    assert validation.valid
    return spec, validation, generate_app(spec, validation)


def _all_passing_results(spec):
    return [TestResult(name=test_id, passed=True)
            for test_id in required_test_ids(spec)]


# --- required test ids ------------------------------------------------------

def test_required_test_ids_cover_spec():
    assert required_test_ids(_valid_spec()) == (
        "endpoint:list_requests",
        "endpoint:create_request",
        "table:leave_requests",
        "page:/requests",
        "page:/requests/new",
        "gate:approval_blocked_without_approval",
        "gate:preview_disposable_db",
    )


# --- test gate --------------------------------------------------------------

def test_all_passing_tests_open_the_gate():
    decision = evaluate_test_results(
        required_test_ids(_valid_spec()), _all_passing_results(_valid_spec()))
    assert decision.gate == "preview_ready"
    assert decision.approval_offered is True
    assert decision.reasons == ()


def test_one_failed_test_blocks_the_gate():
    results = _all_passing_results(_valid_spec())
    results[0] = TestResult(name="endpoint:list_requests", passed=False,
                            detail="500 response")
    decision = evaluate_test_results(required_test_ids(_valid_spec()), results)
    assert decision.gate == "blocked"
    assert decision.approval_offered is False
    assert any("endpoint:list_requests" in r for r in decision.reasons)


def test_missing_test_blocks_the_gate():
    results = _all_passing_results(_valid_spec())[:-1]
    decision = evaluate_test_results(required_test_ids(_valid_spec()), results)
    assert decision.gate == "blocked"
    assert any("missing test gate:preview_disposable_db" in r
               for r in decision.reasons)


def test_no_results_blocks_the_gate():
    decision = evaluate_test_results(required_test_ids(_valid_spec()), [])
    assert decision.gate == "blocked"
    assert len(decision.reasons) == len(required_test_ids(_valid_spec()))


# --- preview checklist ------------------------------------------------------

def test_fresh_preview_is_blocked_with_pending_health():
    spec, validation, app = _setup()
    preview = build_preview(app, spec, validation)
    assert preview.gate == "blocked"
    assert approval_offered(preview) is False
    checks = {c.name: c.status for c in preview.checks}
    assert checks["disposable_database"] == "pass"
    assert checks["preview_link_expires"] == "pass"
    assert checks["fingerprint_shown"] == "pass"
    assert checks["lists_all_spec_items"] == "pass"
    assert checks["health_check"] == "pending"


def test_preview_with_passed_health_passes_health_check():
    spec, validation, app = _setup()
    preview = build_preview(app, spec, validation, health="passed")
    checks = {c.name: c.status for c in preview.checks}
    assert checks["health_check"] == "pass"


def test_preview_with_failed_health_fails_health_check():
    spec, validation, app = _setup()
    preview = build_preview(app, spec, validation, health="failed")
    checks = {c.name: c.status for c in preview.checks}
    assert checks["health_check"] == "fail"


def test_preview_ttl_over_one_hour_fails():
    spec, validation, app = _setup()
    preview = build_preview(app, spec, validation, expires_in_seconds=7200)
    checks = {c.name: c.status for c in preview.checks}
    assert checks["preview_link_expires"] == "fail"


def test_preview_ttl_zero_fails():
    spec, validation, app = _setup()
    preview = build_preview(app, spec, validation, expires_in_seconds=0)
    checks = {c.name: c.status for c in preview.checks}
    assert checks["preview_link_expires"] == "fail"


def test_preview_token_is_deterministic_and_shows_fingerprint():
    spec, validation, app = _setup()
    first = build_preview(app, spec, validation)
    second = build_preview(app, spec, validation)
    assert first.preview_token == second.preview_token
    assert len(first.preview_token) == 32
    assert first.fingerprint_shown == app.fingerprint[:12]
    assert first.pages == ("/requests", "/requests/new")
    assert first.endpoints == ("list_requests", "create_request")
    assert first.tables == ("leave_requests",)


def test_build_preview_rejects_invalid_validation():
    spec = _valid_spec(purpose="x")
    validation = validate_spec(spec)
    app = _setup()[2]
    with pytest.raises(ValueError, match="valid SpecValidation"):
        build_preview(app, spec, validation)


def test_build_preview_rejects_changed_spec():
    spec, validation, app = _setup()
    changed = _valid_spec(purpose="Let employees request leave quickly.")
    with pytest.raises(ValueError, match="fingerprint"):
        build_preview(app, changed, validation)


def test_build_preview_rejects_tampered_generated_app():
    spec, validation, app = _setup()
    tampered_files = tuple(
        dataclasses.replace(f, content=f.content + "\n-- tampered")
        if f.kind == "schema" else f for f in app.files)
    tampered = dataclasses.replace(app, files=tampered_files)
    with pytest.raises(ValueError, match="changed after generation"):
        build_preview(tampered, spec, validation)


def test_build_preview_rejects_bad_health_value():
    spec, validation, app = _setup()
    with pytest.raises(ValueError, match="health"):
        build_preview(app, spec, validation, health="ok")


# --- consistency ------------------------------------------------------------

def test_consistency_clean_for_generated_app():
    spec, validation, app = _setup()
    assert check_generated_consistency(app, spec) == ()


def test_consistency_catches_extra_table_in_schema():
    spec, validation, app = _setup()
    tampered = dataclasses.replace(
        app, files=tuple(
            dataclasses.replace(
                f, content=f.content + '\nCREATE TABLE IF NOT EXISTS '
                                      '"shadow" (\n    "id" UUID\n);')
            if f.kind == "schema" else f for f in app.files))
    problems = check_generated_consistency(tampered, spec)
    assert any("schema.sql creates" in p for p in problems)


def test_consistency_catches_missing_test_id():
    spec, validation, app = _setup()
    tampered = dataclasses.replace(
        app, files=tuple(
            dataclasses.replace(
                f, content=f.content.replace(
                    "# foundry-test-id: table:leave_requests\n", ""))
            if f.kind == "test" else f for f in app.files))
    problems = check_generated_consistency(tampered, spec)
    assert any("miss test id table:leave_requests" in p for p in problems)


# --- gate decision ----------------------------------------------------------

def _ready_preview(spec, validation, app):
    preview = build_preview(app, spec, validation, health="passed")
    decision = decide_gate(
        preview,
        evaluate_test_results(required_test_ids(spec),
                              _all_passing_results(spec)),
        SandboxVerification(passed=True, tables_verified=("leave_requests",),
                            rows_inserted=1))
    return apply_gate(preview, decision)


def test_full_green_path_offers_approval():
    spec, validation, app = _setup()
    preview = _ready_preview(spec, validation, app)
    assert preview.gate == "preview_ready"
    assert approval_offered(preview) is True


def test_pending_health_blocks_gate_even_when_tests_pass():
    spec, validation, app = _setup()
    preview = build_preview(app, spec, validation)  # health pending
    decision = decide_gate(
        preview,
        evaluate_test_results(required_test_ids(spec),
                              _all_passing_results(spec)),
        SandboxVerification(passed=True, tables_verified=("leave_requests",),
                            rows_inserted=1))
    assert decision.gate == "blocked"
    assert any("health check is pending" in r for r in decision.reasons)


def test_missing_sandbox_verification_blocks_gate():
    spec, validation, app = _setup()
    preview = build_preview(app, spec, validation, health="passed")
    decision = decide_gate(
        preview,
        evaluate_test_results(required_test_ids(spec),
                              _all_passing_results(spec)),
        None)
    assert decision.gate == "blocked"
    assert any("no sandbox verification" in r for r in decision.reasons)


def test_failed_sandbox_blocks_gate_with_its_problems():
    spec, validation, app = _setup()
    preview = build_preview(app, spec, validation, health="passed")
    decision = decide_gate(
        preview,
        evaluate_test_results(required_test_ids(spec),
                              _all_passing_results(spec)),
        SandboxVerification(passed=False, problems=("schema.sql failed",)))
    assert decision.gate == "blocked"
    assert any("schema.sql failed" in r for r in decision.reasons)


def test_failed_tests_block_gate_even_when_preview_is_healthy():
    spec, validation, app = _setup()
    preview = build_preview(app, spec, validation, health="passed")
    bad = _all_passing_results(spec)
    bad[1] = TestResult(name="endpoint:create_request", passed=False)
    decision = decide_gate(
        preview, evaluate_test_results(required_test_ids(spec), bad),
        SandboxVerification(passed=True, tables_verified=("leave_requests",),
                            rows_inserted=1))
    assert decision.gate == "blocked"
    assert any("endpoint:create_request" in r for r in decision.reasons)


# --- sandbox verification (real disposable PostgreSQL) ----------------------

def _run_node_as_agent27(request):
    """initdb refuses to run as root, so a sandbox test re-runs itself as
    the unprivileged user `agent27` (created by Agent 27 for the same
    reason). The child run sets FOUNDRY_SANDBOX_CHILD so it runs directly.
    Returns True when the parent should return (child already asserted).
    When that user does not exist in this environment (e.g. after a VM
    rebuild) the test is skipped instead of failing on the missing user."""
    if os.geteuid() == 0 and os.environ.get("FOUNDRY_SANDBOX_CHILD") != "1":
        try:
            pwd.getpwnam("agent27")
        except KeyError:
            pytest.skip("unprivileged user 'agent27' not present in this environment")
        env = dict(os.environ)
        env["FOUNDRY_SANDBOX_CHILD"] = "1"
        env["TMPDIR"] = "/home/hatch/tmp"
        node_id = request.node.nodeid
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            ["runuser", "-u", "agent27", "--",
             "/tmp/foundry-venv/bin/python", "-m", "pytest", node_id,
             "--noconftest", "-q", "-p", "no:cacheprovider"],
            cwd=str(root), env=env, capture_output=True, text=True,
            timeout=300)
        assert result.returncode == 0, (
            result.stdout[-2000:] + "\n" + result.stderr[-2000:])
        return True
    return False


@pytest.mark.skipif(not PG_BIN.is_dir(),
                    reason="PostgreSQL 16 binaries not present")
def test_verify_in_sandbox_applies_schema_and_seeds_rows(request):
    if _run_node_as_agent27(request):
        return
    spec, validation, app = _setup()
    result = verify_in_sandbox(app, spec, PG_BIN)
    assert result.passed is True, result.problems
    assert result.tables_verified == ("leave_requests",)
    assert result.rows_inserted == 1


@pytest.mark.skipif(not PG_BIN.is_dir(),
                    reason="PostgreSQL 16 binaries not present")
def test_verify_in_sandbox_rejects_broken_schema(request):
    if _run_node_as_agent27(request):
        return
    spec, validation, app = _setup()
    broken = dataclasses.replace(
        app, files=tuple(
            dataclasses.replace(f, content="CREATE TABLE broken (;")
            if f.kind == "schema" else f for f in app.files))
    result = verify_in_sandbox(broken, spec, PG_BIN)
    assert result.passed is False
    assert result.problems


def test_verify_in_sandbox_missing_psql_reports_problem():
    spec, validation, app = _setup()
    result = verify_in_sandbox(app, spec, Path("/nonexistent/pg-bin"))
    assert result.passed is False
    assert any("psql not found" in p for p in result.problems)


def test_verify_in_sandbox_missing_schema_reports_problem():
    spec, validation, app = _setup()
    no_schema = dataclasses.replace(
        app, files=tuple(f for f in app.files if f.kind != "schema"))
    result = verify_in_sandbox(no_schema, spec, PG_BIN)
    assert result.passed is False
    assert result.problems == ("missing schema.sql",)
