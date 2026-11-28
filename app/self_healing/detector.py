"""Step W — Self-Healing: fault detection and remedy decisions.

Pure stdlib functions. No I/O, no database, no network, no secrets. The detector
only *decides*; executing a remedy and recording it belong to later slices, which
must route every side effect through ``execute_once`` with read-back verification.

Safety contract (enforced here, not just documented):
- Remedies come from an allow-list only. Anything else escalates.
- ``critical`` incidents always escalate, even when a remedy exists.
- At most ``MAX_ATTEMPTS`` auto-remedy attempts per incident key; afterwards the
  decision is always ``escalate``.
- The same incident key cannot be remedied again inside ``COOLDOWN_SECONDS``.
- Remedies never touch business data, permissions, approvals, or payments: they
  may only restart a process, clear a lock, retry a transient call, requeue a job,
  or switch to a configured fallback.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

# --- thresholds (owner-tunable; see docs/step-w-self-healing-plan.md) ---
HEARTBEAT_STALE_SECONDS = 300          # 5 min without a worker heartbeat = stalled
ERROR_RATE_THRESHOLD = 0.05            # 5% errors over a window = spike
ERROR_RATE_MIN_REQUESTS = 100          # too few requests to call it a spike
STALE_LOCK_SECONDS = 600               # 10 min lock age = stale
MAX_ATTEMPTS = 3                       # auto-remedy attempts per incident key
COOLDOWN_SECONDS = 300                 # 5 min between repeat remedies of one key

COMPONENTS = {"api", "database", "workflow_worker", "ingestion_worker", "vector_index", "backup"}
STATUSES = {"ok", "degraded", "down", "unknown"}
SEVERITIES = {"low", "medium", "high", "critical"}

# incident kind -> (severity, auto remedy or None). None means "escalate only".
SAFE_REMEDIES = {
    "worker_stalled": ("high", "restart_worker"),
    "ingestion_backlog": ("medium", "requeue_job"),
    "error_rate_spike": ("high", "switch_fallback"),
    "stale_lock": ("medium", "clear_stale_lock"),
    "transient_failure": ("low", "retry_once"),
    "api_down": ("critical", None),
    "database_unreachable": ("critical", None),
    "unknown": ("high", None),
    # Slice 5: a failing self-test suite is never auto-remedied (a failing
    # test must never trigger a remedy); it escalates to the owner.
    "self_test_failed": ("high", None),
}

_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+\S+|(?:password|token|secret|api[_-]?key)\s*[:=]\s*[^,\s;]+"
    r"|(?:postgres(?:ql)?|https?)://[^,\s/@]+:[^,\s/@]+@[^,\s]+)"
)


def scrub(text: str) -> str:
    """Remove secret-looking material from owner-facing text. Never log raw evidence."""
    if not isinstance(text, str):
        return ""
    return _SECRET_TEXT.sub("[redacted]", text)[:2000]


@dataclass(frozen=True)
class Probe:
    """One health signal for one component."""

    component: str
    status: str  # ok | degraded | down | unknown
    age_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.component not in COMPONENTS:
            raise ValueError(f"unknown component: {self.component!r}")
        if self.status not in STATUSES:
            raise ValueError(f"unknown status: {self.status!r}")
        if self.age_seconds < 0:
            raise ValueError("age_seconds must not be negative")


@dataclass(frozen=True)
class Incident:
    """A classified fault, ready for a remedy-or-escalate decision."""

    key: str
    kind: str
    severity: str
    component: str
    summary: str
    evidence: str = ""


@dataclass
class AttemptHistory:
    """How often one incident key has already been auto-remedied, and when."""

    attempts: int = 0
    last_attempt_at: datetime | None = None


@dataclass(frozen=True)
class Decision:
    """The detector's verdict for one incident."""

    action: str  # "remedy" | "escalate"
    remedy: str | None = None
    reason: str = ""


def _probe_map(probes) -> dict[str, Probe]:
    out: dict[str, Probe] = {}
    for probe in probes or []:
        out[probe.component] = probe
    return out


def classify_incident(
    probes,
    *,
    error_total: int = 0,
    error_count: int = 0,
    lock_age_seconds: float | None = None,
    worker_heartbeat_age_seconds: float | None = None,
) -> Incident | None:
    """Classify health signals into one incident, or None when everything is fine.

    Priority order (worst first): database, API, worker stall, error spike,
    stale lock, ingestion backlog. A single call reports the single most severe
    fault so the operator fixes one thing at a time.
    """
    by_component = _probe_map(probes)

    db = by_component.get("database")
    if db is not None and db.status == "down":
        return Incident(
            key="database_unreachable",
            kind="database_unreachable",
            severity="critical",
            component="database",
            summary="Database is unreachable.",
            evidence=f"database probe status=down age={db.age_seconds:.0f}s",
        )

    api = by_component.get("api")
    if api is not None and api.status == "down":
        return Incident(
            key="api_down",
            kind="api_down",
            severity="critical",
            component="api",
            summary="API is down.",
            evidence=f"api probe status=down age={api.age_seconds:.0f}s",
        )

    if worker_heartbeat_age_seconds is not None and worker_heartbeat_age_seconds > HEARTBEAT_STALE_SECONDS:
        return Incident(
            key="worker_stalled",
            kind="worker_stalled",
            severity="high",
            component="workflow_worker",
            summary="Workflow worker heartbeat is stale.",
            evidence=f"heartbeat age={worker_heartbeat_age_seconds:.0f}s "
            f"(threshold {HEARTBEAT_STALE_SECONDS}s)",
        )

    if error_total >= ERROR_RATE_MIN_REQUESTS and error_total > 0:
        rate = error_count / error_total
        if rate > ERROR_RATE_THRESHOLD:
            return Incident(
                key="error_rate_spike",
                kind="error_rate_spike",
                severity="high",
                component="api",
                summary=f"Error rate is {rate:.1%} over the last {error_total} requests.",
                evidence=f"errors={error_count} total={error_total} rate={rate:.3f}",
            )

    if lock_age_seconds is not None and lock_age_seconds > STALE_LOCK_SECONDS:
        return Incident(
            key="stale_lock",
            kind="stale_lock",
            severity="medium",
            component="workflow_worker",
            summary="A workflow lock has been held too long.",
            evidence=f"lock age={lock_age_seconds:.0f}s (threshold {STALE_LOCK_SECONDS}s)",
        )

    degraded_worker = next(
        (
            probe
            for probe in (by_component.get("workflow_worker"), by_component.get("ingestion_worker"))
            if probe is not None and probe.status == "degraded"
        ),
        None,
    )
    if degraded_worker is not None:
        return Incident(
            key="ingestion_backlog",
            kind="ingestion_backlog",
            severity="medium",
            component=degraded_worker.component,
            summary=f"{degraded_worker.component} is degraded; jobs are backing up.",
            evidence=f"{degraded_worker.component} probe status=degraded age={degraded_worker.age_seconds:.0f}s",
        )

    stale = [p for p in by_component.values() if p.status == "unknown" and p.age_seconds > HEARTBEAT_STALE_SECONDS]
    if stale:
        component = sorted(p.component for p in stale)[0]
        return Incident(
            key="unknown",
            kind="unknown",
            severity="high",
            component=component,
            summary=f"Health signal for {component} is stale and unclassified.",
            evidence=f"{component} probe status=unknown age={by_component[component].age_seconds:.0f}s",
        )

    return None


def decide_action(
    incident: Incident,
    history: AttemptHistory | None = None,
    *,
    now: datetime | None = None,
) -> Decision:
    """Remedy or escalate? Guards: allow-list, severity, attempt budget, cooldown."""
    history = history or AttemptHistory()
    moment = now or datetime.now(UTC)

    severity, remedy = SAFE_REMEDIES.get(incident.kind, ("high", None))
    if incident.severity == "critical" or severity == "critical":
        return Decision(action="escalate", reason="critical incident: never auto-remedied")
    if remedy is None:
        return Decision(action="escalate", reason=f"no safe auto-remedy for kind {incident.kind!r}")
    if history.attempts >= MAX_ATTEMPTS:
        return Decision(
            action="escalate",
            reason=f"auto-remedy budget exhausted ({history.attempts} attempts already)",
        )
    if history.last_attempt_at is not None:
        elapsed = (moment - history.last_attempt_at).total_seconds()
        if elapsed < COOLDOWN_SECONDS:
            return Decision(
                action="escalate",
                reason=f"cooldown active ({COOLDOWN_SECONDS - int(elapsed)}s left for {incident.key})",
            )
    return Decision(action="remedy", remedy=remedy, reason=f"safe remedy {remedy!r} for {incident.kind!r}")


def escalation_report(incident: Incident, history: AttemptHistory | None = None) -> dict:
    """Plain-language, secret-free owner report for an escalated incident."""
    history = history or AttemptHistory()
    return {
        "incident_key": incident.key,
        "kind": incident.kind,
        "severity": incident.severity,
        "component": incident.component,
        "what_happened": incident.summary,
        "evidence": scrub(incident.evidence),
        "auto_remedy_attempts": history.attempts,
        "needs_owner": True,
        "note": "Self-healing did not change any data, permission, approval, or payment.",
    }


def summarize_incidents(incidents) -> dict:
    """Dashboard roll-up: counts by severity plus the worst severity overall."""
    counts = {level: 0 for level in ("low", "medium", "high", "critical")}
    worst: str | None = None
    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    for incident in incidents or []:
        if incident.severity in counts:
            counts[incident.severity] += 1
            if worst is None or order[incident.severity] > order[worst]:
                worst = incident.severity
    return {"total": sum(counts.values()), "by_severity": counts, "worst": worst}
