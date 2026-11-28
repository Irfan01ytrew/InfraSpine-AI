"""Step W slice 5 — auto-test loop: scheduled suite runs on a throwaway DB,
failures auto-triaged into incidents via slice 1.

The loop runs the suite through the repository's own throwaway-cluster
runner (``scripts/irfanix_postgres_tests.py``), stores one run record per
run, and — when the suite fails — records a ``self_test_failed`` incident
and escalates it to the owner. A failing test never triggers a remedy: the
detector maps ``self_test_failed`` to escalate-only.

The suite runner is injectable (``runner=``) so tests can prove parsing and
triage with a stub instead of running the real suite. The default runner
needs a PostgreSQL bin directory (``SELF_HEALING_PG_BIN`` or ``--pg-bin``);
without one the run is recorded as an ``error``, never faked as a pass.

This module is only ever driven by the CLI (``python -m
app.self_healing.autotest``) or a scheduler calling it — never inside a
request handler, because a suite run takes minutes.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError
from app.platform import Principal
from app.platform.events import audit
from app.self_healing.collector import attempt_history, record_incident
from app.self_healing.detector import Incident, scrub
from app.self_healing.escalation import record_escalation
from app.self_healing.models import SelfHealingTestRun

SUITES = ("focused", "full")
# The focused suite is the self-healing module's own tests. The full suite is
# the whole repository suite (nightly): it is heavy and is never run by the
# 5-minute loop — only by an explicit nightly schedule.
FOCUSED_TARGETS = [
    "tests/test_self_healing_detector.py",
    "tests/test_self_healing_collector.py",
    "tests/test_self_healing_executor.py",
    "tests/test_self_healing_escalation.py",
    "tests/test_self_healing_autotest.py",
]
FULL_TARGETS = ["tests"]
MAX_FAILURE_NAMES = 50

_SUMMARY_RE = re.compile(r"^=+ (.*?) in [\d.]+s =+$", re.MULTILINE)
_COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)")


@dataclass
class RunnerResult:
    """What one suite invocation produced (subprocess-shaped)."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""


@dataclass
class SuiteResult:
    """The parsed outcome of one suite run."""

    suite: str
    status: str  # "passed" | "failed" | "error"
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    failure_names: list[str] = field(default_factory=list)


def parse_pytest_summary(text: str) -> dict | None:
    """Parse pytest's final summary line (``3 failed, 120 passed in 45s``).

    Returns the counts, or None when no summary line is present (collection
    crash, timeout, or an empty run).
    """
    matches = _SUMMARY_RE.findall(text or "")
    if not matches:
        return None
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for number, kind in _COUNT_RE.findall(matches[-1]):
        key = {"passed": "passed", "failed": "failed", "error": "errors",
               "errors": "errors", "skipped": "skipped"}.get(kind)
        if key:
            counts[key] += int(number)
    return counts


def _failure_names(text: str) -> list[str]:
    """Test ids pytest reported as FAILED / ERROR (``-q`` short summary)."""
    names: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith(("FAILED ", "ERROR ")):
            name = line.split(" - ", 1)[0].split(None, 1)[1].strip()
            if name and name not in names:
                names.append(name)
        if len(names) >= MAX_FAILURE_NAMES:
            break
    return names


def _default_runner(argv: list[str], *, timeout_seconds: int) -> RunnerResult:
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_seconds
        )
        return RunnerResult(completed.returncode, completed.stdout, completed.stderr)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return RunnerResult(None, out, "suite run timed out")


def run_suite(
    *,
    suite: str,
    runner=None,
    pg_bin: str | Path | None = None,
    timeout_seconds: int = 1800,
    repo_root: str | Path | None = None,
) -> SuiteResult:
    """Run one suite and parse the outcome. Synchronous: suite runs take minutes.

    ``runner`` is ``runner(argv, timeout_seconds=...) -> RunnerResult``; the
    default runner invokes ``scripts/irfanix_postgres_tests.py`` (throwaway
    cluster). Without a PostgreSQL bin directory the run is an ``error``.
    """
    if suite not in SUITES:
        raise InvalidInputError(f"unknown suite: {suite!r}")
    started = time.monotonic()
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    pg_bin = pg_bin or os.environ.get("SELF_HEALING_PG_BIN")
    if runner is None and not pg_bin:
        return SuiteResult(suite, "error", duration_seconds=time.monotonic() - started)
    targets = FOCUSED_TARGETS if suite == "focused" else FULL_TARGETS
    if runner is None:
        script = root / "scripts" / "irfanix_postgres_tests.py"
        argv = [
            sys.executable, str(script), "--pg-bin", str(pg_bin),
            "--", "-q", "-p", "no:cacheprovider", *targets,
        ]
        result = _default_runner(argv, timeout_seconds=timeout_seconds)
    else:
        result = runner(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets],
            timeout_seconds=timeout_seconds,
        )
    duration = time.monotonic() - started
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    counts = parse_pytest_summary(text)
    names = _failure_names(text)
    if result.returncode == 0:
        status = "passed"
        counts = counts or {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    elif counts is not None and (counts["failed"] > 0 or counts["errors"] > 0):
        status = "failed"
    else:
        status = "error"
        counts = counts or {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    return SuiteResult(
        suite, status,
        passed=counts["passed"], failed=counts["failed"],
        errors=counts["errors"], skipped=counts["skipped"],
        duration_seconds=duration, failure_names=names,
    )


def triage_suite_result(suite: str, result: SuiteResult) -> Incident | None:
    """Map a failed suite run to a slice-1 incident, or None when it passed.

    A failing suite is always escalated, never remedied: ``self_test_failed``
    has no auto-remedy in the detector's allow-list.
    """
    if result.status != "failed":
        return None
    names = result.failure_names[:10]
    evidence = ", ".join(names) if names else "no failure names captured"
    return Incident(
        key=f"self_test_failed:{suite}",
        kind="self_test_failed",
        severity="high",
        component="self_test",
        summary=f"Self-test suite {suite!r} failed: {result.failed} failed, {result.errors} errors.",
        evidence=scrub(evidence),
    )


async def run_autotest(
    db: AsyncSession,
    principal: Principal | None,
    *,
    suite: str,
    runner=None,
    pg_bin: str | Path | None = None,
    timeout_seconds: int = 1800,
    now: datetime | None = None,
) -> tuple[SelfHealingTestRun, Incident | None]:
    """Run one suite, store the run record, and triage a failure. Commits.

    ``principal`` may be None (the CLI runs without a user when no admin
    exists); the run row, incident row, and escalation row are the record in
    that case, and the audit event is skipped.
    """
    started = now or datetime.now(UTC)
    result = run_suite(
        suite=suite, runner=runner, pg_bin=pg_bin, timeout_seconds=timeout_seconds
    )
    finished = datetime.now(UTC)
    row = SelfHealingTestRun(
        suite=suite,
        status=result.status,
        passed=result.passed,
        failed=result.failed,
        errors=result.errors,
        skipped=result.skipped,
        duration_seconds=result.duration_seconds,
        failure_names=result.failure_names,
        started_at=started,
        finished_at=finished,
    )
    db.add(row)
    incident = triage_suite_result(suite, result)
    if incident is not None:
        incident_row = await record_incident(db, incident, now=finished)
        await record_escalation(db, incident, attempt_history(incident_row), now=finished)
        if principal is not None:
            audit(
                db, principal, "self_healing.autotest_failed", outcome="success",
                target=scrub(incident.key)[:100],
                details={"suite": suite, "failed": result.failed, "errors": result.errors},
            )
    await db.commit()
    return row, incident


# --------------------------------------------------------------------------- CLI

async def _resolve_actor(db: AsyncSession) -> Principal | None:
    """The first active admin user, for audit events; None when there is none."""
    from app.db.models import ROLE_ADMIN, User

    user = await db.scalar(
        select(User).where(User.role == ROLE_ADMIN, User.is_active.is_(True)).order_by(User.created_at)
    )
    if user is None:
        return None
    return Principal(user.id, user.email, "user", "web", user.auth_version)


def main(argv: list[str] | None = None) -> int:
    """``python -m app.self_healing.autotest --suite focused --once``.

    Exit codes: 0 suite passed, 1 suite failed (escalated to the owner),
    2 misconfigured or errored run. ``--loop SECONDS`` repeats forever.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.self_healing.autotest",
        description="Step W auto-test loop: run a suite on a throwaway DB and triage failures.",
    )
    parser.add_argument("--suite", choices=SUITES, default="focused")
    parser.add_argument("--once", action="store_true", help="run once, then exit (default)")
    parser.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                        help="repeat every SECONDS instead of exiting")
    parser.add_argument("--pg-bin", type=str, default=None,
                        help="PostgreSQL bin directory (or SELF_HEALING_PG_BIN)")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args(argv)
    if args.loop and args.loop < 60:
        parser.error("--loop must be at least 60 seconds")
    if args.timeout_seconds < 60:
        parser.error("--timeout-seconds must be at least 60")

    from app.core.config import get_settings
    from app.db.session import create_engine, create_sessionmaker

    settings = get_settings()
    engine = create_engine(settings.database_url.get_secret_value())
    sessionmaker = create_sessionmaker(engine)

    async def _run_once() -> int:
        async with sessionmaker() as db:
            principal = await _resolve_actor(db)
            row, incident = await run_autotest(
                db, principal, suite=args.suite, pg_bin=args.pg_bin,
                timeout_seconds=args.timeout_seconds,
            )
            if row.status == "error":
                return 2
            return 1 if incident is not None else 0

    try:
        if args.loop:
            while True:
                code = asyncio.run(_run_once())
                print(f"self-healing autotest {args.suite}: exit={code}", flush=True)
                time.sleep(args.loop)
        return asyncio.run(_run_once())
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
