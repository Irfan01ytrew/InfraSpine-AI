"""Step W slice 6 — watchdog integration: detector decisions for process restarts.

The watchdog answers one question the Windows scheduled-task script
(``scripts/ops/windows/watchdog.ps1``) asks every minute: *is a process
missing, or is it present but wedged?* The answer is a decision, never an
action: this module never starts, stops or kills a process. The script (or
the OS scheduler) performs the restart; the decision is recorded here as an
incident + escalation + audit event so a restart is never silent.

Decision rules (pure ``decide_watchdog``):

- API port closed (nothing listening) -> ``restart_api`` + an ``api_down``
  incident (critical). The process is gone; restarting it is safe.
- API port open but ``/health`` does not answer -> ``escalate_only`` + an
  ``api_down`` incident. The process is wedged; killing it could lose
  in-flight work, so the watchdog never kills it — the owner decides.
- Worker heartbeat stale (older than ``HEARTBEAT_STALE_SECONDS``) and no
  local worker process -> ``restart_worker`` + a ``worker_stalled``
  incident (high). No worker anywhere is alive.
- Worker heartbeat stale but a local worker process exists -> ``escalate_only``
  + a ``worker_stalled`` incident. The process is wedged; never kill it.
- Otherwise -> ``ok``: the API is healthy and some worker, somewhere, is
  heartbeating (workers may run on another machine sharing the database, so
  a missing *local* process with a fresh heartbeat is fine).

The heartbeat is the cross-machine authority: ``read_signals`` reads the
worker's last heartbeat from the database. ``worker_running`` only says
whether a worker process exists on *this* machine; it is supplied by the
caller (the .ps1 script, which already knows its processes).

If the database itself is unreachable, nothing can be recorded in it: the
watchdog writes the incident to the out-of-band escalation file instead
(same format as slice 4's ``escalate_out_of_band``) and returns
``escalate_only``. A watchdog that crashes on a dead database would be
useless exactly when it is needed most.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.events import audit
from app.self_healing.collector import attempt_history, read_signals, record_incident
from app.self_healing.detector import AttemptHistory, Incident
from app.self_healing.escalation import (
    build_escalation,
    escalate_out_of_band,
    record_escalation,
)

#: Worker heartbeat older than this is stale (plan default: 5 minutes).
HEARTBEAT_STALE_SECONDS = 300.0

#: Default API health endpoint, matching the Windows wrapper (localhost:8000).
DEFAULT_API_URL = "http://127.0.0.1:8000/health"


@dataclass(frozen=True)
class ProcessFacts:
    """What the watchdog knows about the two processes it guards."""

    api_listening: bool
    api_healthy: bool
    worker_running: bool
    worker_heartbeat_age_seconds: float | None


@dataclass(frozen=True)
class WatchdogDecision:
    """The watchdog's answer: an action for the scheduler, never the act itself."""

    action: str  # "ok" | "restart_api" | "restart_worker" | "escalate_only"
    incident: Incident | None
    reason: str
    db_down: bool = False

    def to_json(self) -> dict:
        return {
            "action": self.action,
            "incident_key": self.incident.key if self.incident else None,
            "kind": self.incident.kind if self.incident else None,
            "severity": self.incident.severity if self.incident else None,
            "reason": self.reason,
            "db_down": self.db_down,
        }


def decide_watchdog(facts: ProcessFacts) -> WatchdogDecision:
    """Pure decision from process facts. No I/O, no side effects."""
    if not facts.api_listening:
        return WatchdogDecision(
            action="restart_api",
            incident=Incident(
                key="api_down",
                kind="api_down",
                severity="critical",
                component="api",
                summary="API is not listening; the scheduled task should restart it.",
                evidence="",
            ),
            reason="API is not listening on 127.0.0.1:8000; the process is gone.",
        )
    if not facts.api_healthy:
        return WatchdogDecision(
            action="escalate_only",
            incident=Incident(
                key="api_down",
                kind="api_down",
                severity="critical",
                component="api",
                summary="API port is open but /health does not answer; the process is wedged.",
                evidence="",
            ),
            reason="API port is open but /health failed; never kill a wedged process.",
        )
    age = facts.worker_heartbeat_age_seconds
    if age is not None and age > HEARTBEAT_STALE_SECONDS:
        if facts.worker_running:
            return WatchdogDecision(
                action="escalate_only",
                incident=Incident(
                    key="worker_stalled",
                    kind="worker_stalled",
                    severity="high",
                    component="worker",
                    summary="Worker process exists but its heartbeat is stale; it is wedged.",
                    evidence=f"heartbeat age {age:.0f}s",
                ),
                reason="Worker heartbeat is stale but the process exists; never kill it.",
            )
        return WatchdogDecision(
            action="restart_worker",
            incident=Incident(
                key="worker_stalled",
                kind="worker_stalled",
                severity="high",
                component="worker",
                summary="No worker heartbeat anywhere and no local worker process.",
                evidence=f"heartbeat age {age:.0f}s",
            ),
            reason="No worker heartbeat anywhere and no local process; restart the worker.",
        )
    return WatchdogDecision(
        action="ok",
        incident=None,
        reason="API healthy and worker heartbeat fresh.",
    )


def _probe_api(url: str, timeout: float = 8.0) -> tuple[bool, bool]:
    """Return ``(listening, healthy)`` for the API health endpoint.

    Connection refused -> nothing listening. A non-200 answer or a read
    timeout -> something is there but it does not answer healthily.
    """
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, response.status == 200
    except urllib.error.HTTPError:
        return True, False
    except (TimeoutError, socket.timeout):
        return True, False
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return False, False
        return False, False
    except OSError:
        return False, False


async def run_watchdog_check(
    db: AsyncSession | None,
    *,
    api_url: str = DEFAULT_API_URL,
    worker_running: bool = False,
    api_probe: Callable[[str], tuple[bool, bool]] | None = None,
    out_of_band_path: str | None = None,
    principal=None,
    now: datetime | None = None,
) -> WatchdogDecision:
    """Probe the API, read the worker heartbeat, decide, and record the outcome.

    ``db`` may be ``None`` (or the heartbeat read may fail): the database is
    down, so the incident goes to the out-of-band file instead of the tables
    and the decision is ``escalate_only``. The watchdog never raises for a
    dead database — it reports. ``principal`` may be ``None`` (the CLI runs
    without a user); the audit event is then skipped, the incident and
    escalation rows are still the trail.
    """
    moment = now or datetime.now(UTC)
    probe = api_probe or _probe_api
    listening, healthy = probe(api_url)

    if db is None:
        decision = WatchdogDecision(
            action="escalate_only",
            incident=Incident(
                key="database_unreachable",
                kind="database_unreachable",
                severity="critical",
                component="database",
                summary="Database unreachable during the watchdog check.",
                evidence="",
            ),
            reason="Database is unreachable; the incident goes to the out-of-band file.",
            db_down=True,
        )
        if out_of_band_path:
            escalate_out_of_band(
                build_escalation(decision.incident, AttemptHistory()),
                path=out_of_band_path,
            )
        return decision

    try:
        signals = await read_signals(db)
    except Exception:
        decision = WatchdogDecision(
            action="escalate_only",
            incident=Incident(
                key="database_unreachable",
                kind="database_unreachable",
                severity="critical",
                component="database",
                summary="Database unreachable during the watchdog check.",
                evidence="",
            ),
            reason="Database read failed during the watchdog check; out-of-band instead.",
            db_down=True,
        )
        if out_of_band_path:
            escalate_out_of_band(
                build_escalation(decision.incident, AttemptHistory()),
                path=out_of_band_path,
            )
        return decision

    decision = decide_watchdog(
        ProcessFacts(
            api_listening=listening,
            api_healthy=healthy,
            worker_running=worker_running,
            worker_heartbeat_age_seconds=signals.worker_heartbeat_age_seconds,
        )
    )
    if decision.incident is not None:
        row = await record_incident(db, decision.incident, now=moment)
        await record_escalation(db, decision.incident, attempt_history(row), now=moment)
        if principal is not None:
            audit(
                db, principal, "self_healing.watchdog_decision", outcome="success",
                target=decision.incident.key,
            )
        await db.commit()
    return decision


def main(argv: list[str] | None = None) -> int:
    """``python -m app.self_healing.watchdog [--api-url URL] [--worker-running]``.

    Prints one JSON line with the decision and always exits 0: the decision
    is data for the scheduled-task script, not an error. A nonzero exit
    (traceback) means the CLI itself could not run, and the script falls
    back to its old blind behaviour.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.self_healing.watchdog",
        description="Step W watchdog: decide whether the API or worker needs a restart.",
    )
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--worker-running", action="store_true",
                        help="a worker process exists on this machine")
    parser.add_argument("--escalation-file", default=None,
                        help="out-of-band escalation file (or SELF_HEALING_ESCALATION_FILE)")
    args = parser.parse_args(argv)

    from app.core.config import get_settings
    from app.db.session import create_engine, create_sessionmaker

    settings = get_settings()
    engine = create_engine(settings.database_url.get_secret_value())
    sessionmaker = create_sessionmaker(engine)
    out_of_band = args.escalation_file or os.environ.get("SELF_HEALING_ESCALATION_FILE")

    async def _run_once() -> WatchdogDecision:
        try:
            async with sessionmaker() as db:
                return await run_watchdog_check(
                    db, api_url=args.api_url, worker_running=args.worker_running,
                    out_of_band_path=out_of_band,
                )
        except Exception:
            # The session itself could not open: the database is down.
            return await run_watchdog_check(
                None, api_url=args.api_url, worker_running=args.worker_running,
                out_of_band_path=out_of_band,
            )

    try:
        decision = asyncio.run(_run_once())
    finally:
        engine.dispose()
    print(json.dumps(decision.to_json()), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
