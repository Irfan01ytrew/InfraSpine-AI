"""Step W slice 5 — auto-test loop: pytest summary parsing, failure triage,
run records, and the CLI wiring (stub runners only — the real suite is never
run inside a test).
"""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.errors import InvalidInputError
from app.db.session import test_engine_options as _test_engine_options
from app.self_healing.autotest import (
    RunnerResult,
    SuiteResult,
    main,
    parse_pytest_summary,
    run_autotest,
    run_suite,
    triage_suite_result,
)
from app.self_healing.detector import SAFE_REMEDIES, decide_action
from app.self_healing.models import SelfHealingEscalation, SelfHealingIncident, SelfHealingTestRun
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.usefixtures("clean_tables")


def run(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


# --------------------------------------------------------------------------- parsing

def test_parse_pytest_summary_passed_only():
    counts = parse_pytest_summary("==================== 27 passed in 12.34s ====================")
    assert counts == {"passed": 27, "failed": 0, "errors": 0, "skipped": 0}


def test_parse_pytest_summary_mixed():
    counts = parse_pytest_summary(
        "================ 3 failed, 120 passed, 2 skipped, 1 error in 45.20s ================"
    )
    assert counts == {"passed": 120, "failed": 3, "errors": 1, "skipped": 2}


def test_parse_pytest_summary_singular_error():
    counts = parse_pytest_summary("================ 1 error in 3.10s ================")
    assert counts["errors"] == 1


def test_parse_pytest_summary_missing_is_none():
    assert parse_pytest_summary("collected 0 items\nno tests ran\n") is None
    assert parse_pytest_summary("") is None


def test_parse_pytest_summary_uses_last_line():
    counts = parse_pytest_summary(
        "================ 1 failed in 1.00s ================\n"
        "================ 5 passed, 1 failed in 9.99s ================\n"
    )
    assert counts == {"passed": 5, "failed": 1, "errors": 0, "skipped": 0}


# --------------------------------------------------------------------------- triage

def test_triage_passed_is_none():
    assert triage_suite_result("focused", SuiteResult("focused", "passed", passed=10)) is None
    assert triage_suite_result("focused", SuiteResult("focused", "error")) is None


def test_triage_failed_builds_escalate_only_incident():
    result = SuiteResult(
        "focused", "failed", passed=10, failed=2, errors=1,
        failure_names=["tests/test_x.py::test_a", "tests/test_x.py::test_b"],
    )
    incident = triage_suite_result("focused", result)
    assert incident is not None
    assert incident.kind == "self_test_failed"
    assert incident.severity == "high"
    assert incident.component == "self_test"
    assert incident.key == "self_test_failed:focused"
    assert "test_a" in incident.evidence
    # The detector never auto-remedies a failed suite.
    assert SAFE_REMEDIES["self_test_failed"] == ("high", None)
    assert decide_action(incident).action == "escalate"


def test_triage_scrubs_secrets_from_failure_names():
    result = SuiteResult(
        "focused", "failed", failed=1,
        failure_names=["tests/test_x.py::test_a password=hunter2"],
    )
    incident = triage_suite_result("focused", result)
    assert incident is not None
    assert "hunter2" not in incident.evidence


# --------------------------------------------------------------------------- suite runner (stubbed)

def _stub_runner(stdout, returncode=0):
    def run(argv, *, timeout_seconds):
        assert argv[1] == "-m" and argv[2] == "pytest"
        return RunnerResult(returncode, stdout, "")
    return run


def test_run_suite_passing_stub():
    result = run_suite(
        suite="focused",
        runner=_stub_runner("==================== 10 passed in 1.23s ====================\n"),
    )
    assert result.status == "passed"
    assert result.passed == 10 and result.failed == 0


def test_run_suite_failing_stub_parses_names():
    stdout = (
        "FAILED tests/test_x.py::test_a - AssertionError\n"
        "FAILED tests/test_x.py::test_b - AssertionError\n"
        "================ 2 failed, 8 passed in 2.00s ====================\n"
    )
    result = run_suite(suite="focused", runner=_stub_runner(stdout, returncode=1))
    assert result.status == "failed"
    assert result.failed == 2 and result.passed == 8
    assert result.failure_names == ["tests/test_x.py::test_a", "tests/test_x.py::test_b"]


def test_run_suite_no_summary_is_error():
    result = run_suite(suite="focused", runner=_stub_runner("collected 0 items\n", returncode=2))
    assert result.status == "error"


def test_run_suite_timeout_is_error():
    def timeout_runner(argv, *, timeout_seconds):
        return RunnerResult(None, "", "suite run timed out")
    result = run_suite(suite="focused", runner=timeout_runner)
    assert result.status == "error"


def test_run_suite_misconfigured_without_pg_bin(monkeypatch):
    monkeypatch.delenv("SELF_HEALING_PG_BIN", raising=False)
    result = run_suite(suite="focused")
    assert result.status == "error"


def test_run_suite_unknown_suite():
    with pytest.raises(InvalidInputError):
        run_suite(suite="nightly")


# --------------------------------------------------------------------------- service (stubbed runner)

def test_run_autotest_failure_records_run_incident_and_escalation():
    stdout = (
        "FAILED tests/test_x.py::test_a - AssertionError\n"
        "================ 1 failed, 9 passed in 1.00s ====================\n"
    )

    async def go(sm):
        async with sm() as db:
            row, incident = await run_autotest(
                db, None, suite="focused", runner=_stub_runner(stdout, returncode=1)
            )
            assert row.status == "failed"
            assert row.failed == 1 and row.passed == 9
            assert row.failure_names == ["tests/test_x.py::test_a"]
            assert incident is not None and incident.kind == "self_test_failed"
            stored_incident = await db.scalar(
                select(SelfHealingIncident).where(SelfHealingIncident.key == "self_test_failed:focused")
            )
            assert stored_incident is not None
            assert stored_incident.status == "open"
            escalation = await db.scalar(
                select(SelfHealingEscalation).where(
                    SelfHealingEscalation.incident_key == "self_test_failed:focused"
                )
            )
            assert escalation is not None
            assert escalation.channel == "admin_ui"

    run(go)


def test_run_autotest_pass_records_run_only():
    async def go(sm):
        async with sm() as db:
            row, incident = await run_autotest(
                db, None, suite="focused",
                runner=_stub_runner("==================== 10 passed in 1.00s ====================\n"),
            )
            assert row.status == "passed"
            assert incident is None
            assert await db.scalar(select(SelfHealingIncident)) is None
            assert await db.scalar(select(SelfHealingEscalation)) is None
            assert await db.scalar(select(SelfHealingTestRun)) is not None

    run(go)


# --------------------------------------------------------------------------- CLI

def test_cli_help():
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_cli_rejects_short_loop():
    with pytest.raises(SystemExit) as exc:
        main(["--loop", "30"])
    assert exc.value.code == 2


def test_cli_rejects_unknown_suite():
    with pytest.raises(SystemExit) as exc:
        main(["--suite", "nightly", "--once"])
    assert exc.value.code == 2
