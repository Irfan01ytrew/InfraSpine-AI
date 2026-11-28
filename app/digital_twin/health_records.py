"""Twin health records service (Step K v2 slice 3).

Async service over the stored twin versions and health alerts.
Reads the latest version, computes its health
(:func:`app.digital_twin.health.compute_health`), keeps a per-version
health history, and evaluates + records the health watch
(:func:`app.digital_twin.health_watch.evaluate_health_watch`).

No write route: the twin service / worker calls
:func:`evaluate_and_record_health_watch` on a schedule (or after
recording a version). The API in ``app.api.routes.digital_twin`` is
read-only. Nothing here approves, dispatches, grants access, or
pays — alerts are advisory rows, and their detail carries score
points and counts only, never money amounts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select

from app.db.twin_health_models import TwinHealthAlert
from app.db.twin_snapshot_models import TwinSnapshotVersion
from app.digital_twin import health, health_watch

#: Alert retention defaults (plan §6 retention, same 90-day age rule
#: as versions; 100 alerts is the per-tenant cap, not 30 — alerts are
#: small rows and a watch history shorter than the version history
#: would hide repeat firings).
DEFAULT_ALERT_RETENTION_COUNT = 100
DEFAULT_ALERT_RETENTION_DAYS = 90

#: Hard cap on list responses so alert/history lists stay bounded.
MAX_LIST_LIMIT = 100

__all__ = [
    "DEFAULT_ALERT_RETENTION_COUNT",
    "DEFAULT_ALERT_RETENTION_DAYS",
    "MAX_LIST_LIMIT",
    "alert_summary",
    "evaluate_and_record_health_watch",
    "health_history",
    "latest_health",
    "list_health_alerts",
    "prune_health_alerts",
    "record_health_alerts",
]


def alert_summary(row):
    """Small read-only summary of a stored alert row.

    No money amounts, no entity names — the detail is score points
    and counts only, so the list endpoint is safe for any member.
    """
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "rule_key": row.rule_key,
        "severity": row.severity,
        "fingerprint": row.fingerprint,
        "health_score": row.health_score,
        "drift_score": row.drift_score,
        "trust": row.trust,
        "title": row.title,
        "detail": row.detail,
        "evaluated_at": row.evaluated_at.isoformat(),
        "created_at": row.created_at.isoformat(),
    }


def _assessment_for_row(row):
    """Health assessment for one stored version row."""
    return health.compute_health(
        drift_report=row.drift_report,
        source_freshness=row.source_freshness or [],
        fingerprint=row.fingerprint,
        as_of=row.as_of.isoformat() if row.as_of is not None else None,
    )


async def latest_health(db, tenant_id):
    """Health of the latest recorded version, or ``None`` when the
    tenant has no versions yet."""
    if not isinstance(tenant_id, UUID):
        raise ValueError(f"tenant_id must be a UUID, got {tenant_id!r}.")
    row = await db.scalar(
        select(TwinSnapshotVersion)
        .where(TwinSnapshotVersion.tenant_id == tenant_id)
        .order_by(TwinSnapshotVersion.seq.desc())
        .limit(1)
    )
    if row is None:
        return None
    return _assessment_for_row(row)


async def health_history(db, tenant_id, *, limit=30):
    """Per-version health, newest first (never the full snapshots)."""
    if not isinstance(tenant_id, UUID):
        raise ValueError(f"tenant_id must be a UUID, got {tenant_id!r}.")
    clamped = max(1, min(MAX_LIST_LIMIT, int(limit)))
    rows = (
        await db.scalars(
            select(TwinSnapshotVersion)
            .where(TwinSnapshotVersion.tenant_id == tenant_id)
            .order_by(TwinSnapshotVersion.seq.desc())
            .limit(clamped)
        )
    ).all()
    return [_assessment_for_row(row) for row in rows]


async def record_health_alerts(db, tenant_id, evaluation):
    """Store the fired alerts of one watch evaluation.

    Idempotent per (tenant, fingerprint): an alert already stored
    for the same rule + twin version is not stored twice — a refire
    after the rule's cooldown only refreshes the row's
    ``evaluated_at`` (the cooldown memory), so the same version
    cannot spam the alert list. Returns the stored summaries (newly
    stored plus already-stored rows for the fired fingerprints,
    newest first).
    """
    if not isinstance(tenant_id, UUID):
        raise ValueError(f"tenant_id must be a UUID, got {tenant_id!r}.")
    fired = evaluation.get("alerts") or []
    if not fired:
        return []
    rows = (
        await db.scalars(
            select(TwinHealthAlert).where(
                TwinHealthAlert.tenant_id == tenant_id,
                TwinHealthAlert.fingerprint.in_([alert.fingerprint for alert in fired]),
            )
        )
    ).all()
    existing = {row.fingerprint: row for row in rows}
    for alert in fired:
        row = existing.get(alert.fingerprint)
        if row is not None:
            # Refire after cooldown: refresh the last-fired time so
            # the next evaluation's cooldown is measured from now.
            row.evaluated_at = alert.evaluated_at
            continue
        db.add(TwinHealthAlert(
            tenant_id=tenant_id,
            rule_key=alert.rule_key,
            severity=alert.severity,
            fingerprint=alert.fingerprint,
            health_score=alert.health_score,
            drift_score=alert.drift_score,
            trust=alert.trust,
            title=alert.title,
            detail=alert.detail,
            evaluated_at=alert.evaluated_at,
        ))
    await db.flush()
    rows = (
        await db.scalars(
            select(TwinHealthAlert)
            .where(
                TwinHealthAlert.tenant_id == tenant_id,
                TwinHealthAlert.fingerprint.in_([alert.fingerprint for alert in fired]),
            )
            .order_by(TwinHealthAlert.created_at.desc(), TwinHealthAlert.id.desc())
        )
    ).all()
    return [alert_summary(row) for row in rows]


async def list_health_alerts(db, tenant_id, *, limit=30):
    """Stored alerts, newest first."""
    if not isinstance(tenant_id, UUID):
        raise ValueError(f"tenant_id must be a UUID, got {tenant_id!r}.")
    clamped = max(1, min(MAX_LIST_LIMIT, int(limit)))
    rows = (
        await db.scalars(
            select(TwinHealthAlert)
            .where(TwinHealthAlert.tenant_id == tenant_id)
            .order_by(TwinHealthAlert.created_at.desc(), TwinHealthAlert.id.desc())
            .limit(clamped)
        )
    ).all()
    return [alert_summary(row) for row in rows]


async def prune_health_alerts(db, tenant_id, *, keep=DEFAULT_ALERT_RETENTION_COUNT,
                              max_age_days=DEFAULT_ALERT_RETENTION_DAYS, now=None):
    """Delete alerts beyond the retention rule; returns how many.

    An alert leaves when it is older than ``max_age_days`` OR when
    it falls outside the newest ``keep`` alerts for the tenant.
    Derived data only — a pruned alert is re-derived by re-evaluating
    the watch over the stored versions.
    """
    if keep < 1:
        raise ValueError("keep must be at least 1.")
    if max_age_days < 0:
        raise ValueError("max_age_days must not be negative.")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    cutoff = current - timedelta(days=max_age_days)
    rows = (
        await db.scalars(
            select(TwinHealthAlert)
            .where(TwinHealthAlert.tenant_id == tenant_id)
            .order_by(TwinHealthAlert.created_at.desc(), TwinHealthAlert.id.desc())
        )
    ).all()
    doomed = [
        row.id
        for index, row in enumerate(rows)
        if index >= keep or row.created_at < cutoff
    ]
    if doomed:
        await db.execute(
            delete(TwinHealthAlert).where(TwinHealthAlert.id.in_(doomed))
        )
    return len(doomed)


async def evaluate_and_record_health_watch(db, tenant_id, *, now=None,
                                           rules=health_watch.DEFAULT_RULES,
                                           quiet_start=health_watch.DEFAULT_QUIET_START,
                                           quiet_end=health_watch.DEFAULT_QUIET_END):
    """Evaluate the health watch for a tenant and store new firings.

    Loads the latest version's health, evaluates the rules against
    the stored alerts (their fingerprints + last-fired times are the
    cooldown memory), records newly fired alerts, and prunes old
    ones. Returns the evaluation dict (``alerts`` / ``suppressed`` /
    ``deferred`` / ``evaluated_at`` / ``fingerprint``) plus
    ``health`` (the assessment, or ``None`` when the tenant has no
    versions) and ``recorded`` (stored summaries of the fired
    alerts). With no versions recorded, nothing fires and
    ``recorded`` is empty.
    """
    if not isinstance(tenant_id, UUID):
        raise ValueError(f"tenant_id must be a UUID, got {tenant_id!r}.")
    moment = now or datetime.now(UTC)
    if not isinstance(moment, datetime):
        raise ValueError("now must be a datetime.")
    assessment = await latest_health(db, tenant_id)
    if assessment is None:
        return {
            "health": None,
            "alerts": [],
            "suppressed": [],
            "deferred": [],
            "evaluated_at": moment,
            "fingerprint": None,
            "recorded": [],
            "reason": "no twin versions recorded",
        }
    stored = (
        await db.scalars(
            select(TwinHealthAlert).where(TwinHealthAlert.tenant_id == tenant_id)
        )
    ).all()
    sent_at = {}
    for row in stored:
        # Cooldown memory is the last-fired time (evaluated_at), not
        # the first-fire time: a refire refreshes it (see
        # record_health_alerts).
        previous = sent_at.get(row.fingerprint)
        if previous is None or row.evaluated_at > previous:
            sent_at[row.fingerprint] = row.evaluated_at
    evaluation = health_watch.evaluate_health_watch(
        assessment, tenant_id=str(tenant_id), now=moment, rules=rules,
        sent_at=sent_at, quiet_start=quiet_start, quiet_end=quiet_end,
    )
    recorded = await record_health_alerts(db, tenant_id, evaluation)
    await prune_health_alerts(db, tenant_id, now=moment)
    return {**evaluation, "health": assessment, "recorded": recorded}
