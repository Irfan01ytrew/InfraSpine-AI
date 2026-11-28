"""Step W slice 2 — signal collection.

Turns live health signals into detector ``Probe`` objects, stores a snapshot of
each probe with a timestamp, and records the classified incident (if any).

Signal sources (all reads, no writes except the probe/incident rows):
- database: a ``SELECT 1`` round-trip.
- api: caller-supplied liveness (the ``/health`` result); ``None`` means unknown.
- workflow / ingestion workers: ``IngestionWorkerHeartbeat`` rows (mode
  ``worker`` / ``embedded``). Fresh is three missed beats (30s); older than the
  detector's 5-minute threshold is down; in between is degraded.
- ingestion queue: documents in ``pending`` status whose ``next_attempt_at``
  has passed. A non-empty due queue with no worker online degrades ingestion.
- stale locks: the oldest expired lease on a running workflow step
  (``wf_steps.lease_until``) or a processing document (``documents.locked_until``).
- error rate: caller-supplied counts (no error-log table exists); the detector
  only calls it a spike over 100+ requests.
- vector index: caller-supplied; ``None`` means unknown.
- backup: there is no backup/restore code in ``app/`` yet, so this probe is
  always ``unknown`` and never auto-remedied.

The collector never touches business data, permissions, approvals, or payments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DOC_PENDING, DOC_PROCESSING, Document, IngestionWorkerHeartbeat
from app.db.workflow_engine_models import WorkflowStep
from app.self_healing.detector import (
    HEARTBEAT_STALE_SECONDS,
    AttemptHistory,
    Incident,
    Probe,
    classify_incident,
)
from app.self_healing.models import SelfHealingIncident, SelfHealingProbe

# Three missed beats at the default 10s heartbeat interval (see
# app.core.config rag_worker_heartbeat_seconds); mirrors app.rag.ingestion.workers_online.
HEARTBEAT_FRESH_SECONDS = 30.0


@dataclass
class Signals:
    """Everything one collection round observed."""

    probes: list[Probe] = field(default_factory=list)
    worker_heartbeat_age_seconds: float | None = None
    lock_age_seconds: float | None = None
    queue_depth: int = 0
    workers_online: int = 0
    error_total: int = 0
    error_count: int = 0


def _probe_status(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "unknown"
    if age_seconds <= HEARTBEAT_FRESH_SECONDS:
        return "ok"
    if age_seconds <= HEARTBEAT_STALE_SECONDS:
        return "degraded"
    return "down"


async def read_signals(
    db: AsyncSession,
    *,
    api_ok: bool | None = None,
    vector_index_ok: bool | None = None,
    error_total: int = 0,
    error_count: int = 0,
    now: datetime | None = None,
) -> Signals:
    """Read the live signals and build detector probes. Reads only."""
    moment = now or datetime.now(UTC)
    signals = Signals(error_total=error_total, error_count=error_count)

    try:
        await db.execute(select(1))
        db_status = "ok"
    except Exception:
        await db.rollback()
        db_status = "down"
    api_status = "unknown" if api_ok is None else ("ok" if api_ok else "down")
    vector_status = "unknown" if vector_index_ok is None else ("ok" if vector_index_ok else "down")
    if db_status == "down":
        # The database itself is down: no further signal reads are possible,
        # and probe rows cannot be stored either. Return the probes the caller
        # can still classify (database_unreachable); the escalation for a dead
        # database must travel outside the database (slices 4–5).
        return Signals(
            probes=[
                Probe("database", "down", 0.0),
                Probe("api", api_status, 0.0),
                Probe("workflow_worker", "unknown", 0.0),
                Probe("ingestion_worker", "unknown", 0.0),
                Probe("vector_index", vector_status, 0.0),
                Probe("backup", "unknown", 0.0),
            ],
            error_total=error_total,
            error_count=error_count,
        )
    signals.probes.append(Probe("database", db_status, 0.0))

    signals.probes.append(Probe("api", api_status, 0.0))

    heartbeats = {
        row[0]: row[1]
        for row in await db.execute(
            select(
                IngestionWorkerHeartbeat.mode,
                func.max(IngestionWorkerHeartbeat.heartbeat_at),
            ).group_by(IngestionWorkerHeartbeat.mode)
        )
    }

    worker_age = None
    if "worker" in heartbeats and heartbeats["worker"] is not None:
        worker_age = (moment - heartbeats["worker"]).total_seconds()
    signals.worker_heartbeat_age_seconds = worker_age
    signals.probes.append(
        Probe(
            "workflow_worker",
            _probe_status(worker_age),
            max(worker_age or 0.0, 0.0),
        )
    )

    embedded_age = None
    if "embedded" in heartbeats and heartbeats["embedded"] is not None:
        embedded_age = (moment - heartbeats["embedded"]).total_seconds()
    signals.workers_online = int(
        await db.scalar(
            select(func.count()).where(
                IngestionWorkerHeartbeat.heartbeat_at >= moment - _fresh_window()
            )
        )
        or 0
    )
    signals.queue_depth = int(
        await db.scalar(
            select(func.count()).where(
                Document.status == DOC_PENDING,
                Document.next_attempt_at <= moment,
            )
        )
        or 0
    )
    ingestion_status = _probe_status(embedded_age)
    if ingestion_status == "ok" and signals.queue_depth > 0 and signals.workers_online == 0:
        ingestion_status = "degraded"
    signals.probes.append(
        Probe(
            "ingestion_worker",
            ingestion_status,
            max(embedded_age or 0.0, 0.0),
        )
    )

    stale_step_age = await db.scalar(
        select(func.max(func.extract("epoch", moment - WorkflowStep.lease_until))).where(
            WorkflowStep.status == "running",
            WorkflowStep.lease_until.is_not(None),
            WorkflowStep.lease_until < moment,
        )
    )
    stale_doc_age = await db.scalar(
        select(func.max(func.extract("epoch", moment - Document.locked_until))).where(
            Document.status == DOC_PROCESSING,
            Document.locked_until.is_not(None),
            Document.locked_until < moment,
        )
    )
    ages = [float(a) for a in (stale_step_age, stale_doc_age) if a is not None]
    signals.lock_age_seconds = max(ages) if ages else None

    signals.probes.append(Probe("vector_index", vector_status, 0.0))
    signals.probes.append(Probe("backup", "unknown", 0.0))
    return signals


def _fresh_window() -> timedelta:
    return timedelta(seconds=HEARTBEAT_FRESH_SECONDS)


async def store_probes(db: AsyncSession, signals: Signals, *, now: datetime | None = None) -> list[SelfHealingProbe]:
    """Insert one probe snapshot row per probe. Flushes; the caller commits."""
    moment = now or datetime.now(UTC)
    rows = []
    for probe in signals.probes:
        row = SelfHealingProbe(
            component=probe.component,
            status=probe.status,
            age_seconds=probe.age_seconds,
            error_total=signals.error_total,
            error_count=signals.error_count,
            detail={
                "queue_depth": signals.queue_depth,
                "workers_online": signals.workers_online,
            },
            recorded_at=moment,
        )
        db.add(row)
        rows.append(row)
    await db.flush()
    return rows


async def record_incident(
    db: AsyncSession, incident: Incident, *, now: datetime | None = None
) -> SelfHealingIncident:
    """Upsert the incident row by key. A fault that recurs after it was remedied
    or escalated reopens the row (status back to ``open``); attempts are kept."""
    moment = now or datetime.now(UTC)
    row = await db.scalar(select(SelfHealingIncident).where(SelfHealingIncident.key == incident.key))
    if row is None:
        row = SelfHealingIncident(
            key=incident.key,
            kind=incident.kind,
            severity=incident.severity,
            component=incident.component,
            summary=incident.summary,
            evidence=incident.evidence,
            status="open",
            first_seen_at=moment,
        )
        db.add(row)
    else:
        row.kind = incident.kind
        row.severity = incident.severity
        row.component = incident.component
        row.summary = incident.summary
        row.evidence = incident.evidence
        if row.status in {"remedied", "escalated"}:
            row.status = "open"
        row.updated_at = moment
    await db.flush()
    return row


def attempt_history(row: SelfHealingIncident | None) -> AttemptHistory:
    """The detector's attempt history, read from the persisted incident row."""
    if row is None:
        return AttemptHistory()
    return AttemptHistory(attempts=row.attempts, last_attempt_at=row.last_attempt_at)


async def collect_and_store(
    db: AsyncSession,
    *,
    api_ok: bool | None = None,
    vector_index_ok: bool | None = None,
    error_total: int = 0,
    error_count: int = 0,
    now: datetime | None = None,
) -> tuple[Signals, Incident | None, SelfHealingIncident | None]:
    """One collection round: read signals, store probe snapshots, classify the
    worst incident (if any) and record it. Commits once at the end."""
    moment = now or datetime.now(UTC)
    signals = await read_signals(
        db,
        api_ok=api_ok,
        vector_index_ok=vector_index_ok,
        error_total=error_total,
        error_count=error_count,
        now=moment,
    )
    await store_probes(db, signals, now=moment)
    incident = classify_incident(
        signals.probes,
        error_total=signals.error_total,
        error_count=signals.error_count,
        lock_age_seconds=signals.lock_age_seconds,
        worker_heartbeat_age_seconds=signals.worker_heartbeat_age_seconds,
    )
    row = await record_incident(db, incident, now=moment) if incident is not None else None
    await db.commit()
    return signals, incident, row
