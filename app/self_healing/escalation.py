"""Step W slice 4 — escalation channel: owner report delivery.

The admin UI is the delivery destination today: an escalation row is the
record that a scrubbed, plain-language report reached the owner, and whether
a human has acknowledged it. WhatsApp/email delivery does not exist yet —
it depends on Step F / integrations — so the service only ever delivers via
``admin_ui`` and refuses other channels instead of faking them.

Out-of-band delivery (slice 2's owed DB-down path): when the database itself
is down, no row can be stored, so the scrubbed report is appended as one
JSON line to a configured file (``escalate_out_of_band``). The file path is
an explicit caller argument; with no path configured the function writes
nothing and reports that. It never contains secrets: every report passes
through the detector's ``scrub`` first.

Acknowledgement is always an explicit human act. A fault that recurs after
acknowledgement reopens the escalation (acknowledgement cleared) instead of
silently reusing the old one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError, NotFoundError
from app.platform import Principal
from app.platform.events import audit
from app.self_healing.collector import attempt_history
from app.self_healing.detector import AttemptHistory, Incident, escalation_report, scrub
from app.self_healing.models import SelfHealingEscalation, SelfHealingIncident

# The only delivery channel that exists today. WhatsApp/email arrive with
# Step F / integrations; until then they are refused, never faked.
CHANNELS = ("admin_ui",)


def build_escalation(incident: Incident, history: AttemptHistory | None = None) -> dict:
    """The scrubbed, plain-language owner report for one escalated incident."""
    report = escalation_report(incident, history)
    report["channel"] = "admin_ui"
    report["delivered_via"] = "admin UI (self-healing dashboard)"
    return report


async def record_escalation(
    db: AsyncSession,
    incident: Incident,
    history: AttemptHistory | None = None,
    *,
    channel: str = "admin_ui",
    now: datetime | None = None,
) -> SelfHealingEscalation:
    """Upsert the escalation delivery record for one incident key. Flushes;
    the caller commits. A reopened incident clears a stale acknowledgement."""
    if channel not in CHANNELS:
        raise InvalidInputError(f"Escalation channel {channel!r} is not available yet.")
    moment = now or datetime.now(UTC)
    history = history or AttemptHistory()
    row = await db.scalar(
        select(SelfHealingEscalation).where(SelfHealingEscalation.incident_key == incident.key)
    )
    if row is None:
        row = SelfHealingEscalation(
            incident_key=incident.key,
            kind=incident.kind,
            severity=incident.severity,
            component=incident.component,
            summary=incident.summary,
            evidence=scrub(incident.evidence),
            attempts=history.attempts,
            channel=channel,
            delivered_at=moment,
        )
        db.add(row)
    else:
        row.kind = incident.kind
        row.severity = incident.severity
        row.component = incident.component
        row.summary = incident.summary
        row.evidence = scrub(incident.evidence)
        row.attempts = history.attempts
        row.channel = channel
        row.delivered_at = moment
        if row.acknowledged_at is not None:
            # The fault recurred after a human acknowledged it: the old
            # acknowledgement no longer covers this escalation.
            row.acknowledged_at = None
            row.acknowledged_by = None
        row.updated_at = moment
    await db.flush()
    return row


async def list_escalations(
    db: AsyncSession,
    *,
    acknowledged: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[SelfHealingEscalation]:
    """Escalations, newest first. ``acknowledged`` filters when given."""
    if limit < 1 or limit > 200:
        raise InvalidInputError("limit must be between 1 and 200")
    if offset < 0:
        raise InvalidInputError("offset must not be negative")
    stmt = select(SelfHealingEscalation).order_by(
        SelfHealingEscalation.delivered_at.desc(), SelfHealingEscalation.incident_key
    )
    if acknowledged is True:
        stmt = stmt.where(SelfHealingEscalation.acknowledged_at.is_not(None))
    elif acknowledged is False:
        stmt = stmt.where(SelfHealingEscalation.acknowledged_at.is_(None))
    stmt = stmt.limit(limit).offset(offset)
    return list(await db.scalars(stmt))


async def acknowledge_escalation(
    db: AsyncSession,
    principal: Principal,
    incident_key: str,
    *,
    now: datetime | None = None,
) -> SelfHealingEscalation:
    """Mark one escalation acknowledged by a human. Commits. 404 when the
    incident has no escalation record."""
    moment = now or datetime.now(UTC)
    row = await db.scalar(
        select(SelfHealingEscalation).where(SelfHealingEscalation.incident_key == incident_key)
    )
    if row is None:
        raise NotFoundError("No escalation for this incident.")
    row.acknowledged_at = moment
    row.acknowledged_by = principal.user_id
    row.updated_at = moment
    audit(
        db, principal, "self_healing.escalation_acknowledged", outcome="success",
        target=scrub(incident_key)[:100],
        details={"kind": row.kind, "severity": row.severity},
    )
    await db.commit()
    return row


def escalate_out_of_band(report: dict, *, path: str | Path | None) -> bool:
    """Append one scrubbed escalation report as a JSON line to ``path``.

    This is the delivery path when the database itself is down and no row
    can be stored. Returns True when a line was written, False when no path
    was configured (nothing is written anywhere in that case).
    """
    if path is None or str(path) == "":
        return False
    safe = {
        "incident_key": scrub(str(report.get("incident_key", "")))[:100],
        "kind": scrub(str(report.get("kind", "")))[:40],
        "severity": scrub(str(report.get("severity", "")))[:16],
        "component": scrub(str(report.get("component", "")))[:32],
        "what_happened": scrub(str(report.get("what_happened", "")))[:500],
        "evidence": scrub(str(report.get("evidence", "")))[:2000],
        "auto_remedy_attempts": report.get("auto_remedy_attempts", 0),
        "needs_owner": True,
        "note": "Self-healing did not change any data, permission, approval, or payment.",
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=True) + "\n")
    return True


def escalation_for_row(row: SelfHealingIncident) -> dict:
    """The owner report for an incident row (used by the executor wiring)."""
    incident = Incident(
        key=row.key,
        kind=row.kind,
        severity=row.severity,
        component=row.component,
        summary=row.summary,
        evidence=row.evidence,
    )
    return build_escalation(incident, attempt_history(row))
