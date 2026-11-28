"""Step AH slice 6 — audit trail + export.

Every resilience alert, recommendation, approval and action is recorded
through the platform audit events (``app/platform/events.py`` →
``app/services/audit.py``) with secret redaction, and the trail is
exportable for review. This module is the single place resilience code
writes audit events, so the action names, the redaction and the scope
tagging stay consistent:

* every event action starts with ``resilience.``;
* every event's details carry ``scope_id`` (when known) so the trail can
  be filtered per department without a scope column on the audit table;
* details are scrubbed with :func:`app.recovery.policy.redact` before
  they are stored — a secret that slips into a detail is replaced, never
  persisted;
* the export is built from stored rows only, so what the owner reviews is
  exactly what was recorded.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.models import AuditLog
from app.platform.events import audit, publish
from app.recovery.policy import redact

EXPORT_COLUMNS = ("created_at", "actor_email", "action", "target", "outcome", "details")

# Financial identifiers are never audit data: the snapshot builder never
# copies them, and the trail must not either. The shared redactor covers
# secret keys; this covers account identifiers specifically.
_FORBIDDEN_DETAIL_KEY = re.compile(
    r"payee.?account|bank.?account|account.?number|iban|routing.?number", re.I
)


def _scrub_details(details):
    """Redact secrets (shared redactor) and blank financial identifiers."""
    clean = redact(details or {})
    if isinstance(clean, dict):
        return {
            key: ("[redacted]" if _FORBIDDEN_DETAIL_KEY.search(str(key)) else value)
            for key, value in clean.items()
        }
    return clean


def record_resilience_event(db, principal, event, *, target=None, details=None, outcome="success"):
    """Record one resilience audit event and publish its outbox twin.

    ``event`` is the short name (``plan_started``); the stored action is
    ``resilience.<event>`` and the outbox event type is the same. Returns
    the redacted details that were stored.
    """
    clean = _scrub_details(details)
    if not isinstance(clean, dict):
        clean = {"value": clean}
    audit(db, principal, f"resilience.{event}", target=target, details=clean, outcome=outcome)
    return clean


async def publish_resilience_event(db, principal, event, scope_id, payload):
    """Publish one resilience outbox event (delivery is the outbox worker's job)."""
    await publish(db, f"resilience.{event}", scope_id, payload, principal.trace_id)


async def resilience_audit_trail(db, *, scope_id=None, limit=200):
    """Audit rows for resilience actions, newest first, redacted.

    When ``scope_id`` is given, only events tagged with that scope are
    returned (the tag lives in the stored details — the audit table has no
    scope column). Returns plain dicts, never ORM rows.
    """
    query = (
        select(AuditLog)
        .where(AuditLog.action.like("resilience.%"))
        .order_by(AuditLog.id.desc())
        .limit(max(1, min(limit, 1000)))
    )
    rows = (await db.scalars(query)).all()
    out = []
    for row in rows:
        details = _scrub_details(row.details or {})
        if not isinstance(details, dict):
            details = {"value": details}
        if scope_id is not None and details.get("scope_id") != str(scope_id):
            continue
        out.append(
            {
                "id": row.id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "actor_email": row.actor_email,
                "action": row.action,
                "target": row.target,
                "outcome": row.outcome,
                "details": details,
            }
        )
    return out


def build_audit_export(rows, *, generated_at=None):
    """Build an exportable audit report from trail rows (pure).

    ``rows`` are the dicts returned by :func:`resilience_audit_trail`.
    The export carries the counts by action and outcome, the column list,
    and one CSV-ready row per audit entry (details re-redacted, flattened
    to a short string so the CSV stays readable).
    """
    generated = generated_at or datetime.now(UTC).isoformat()
    by_action: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    export_rows = []
    for row in rows:
        by_action[row["action"]] = by_action.get(row["action"], 0) + 1
        by_outcome[row["outcome"]] = by_outcome.get(row["outcome"], 0) + 1
        details = _scrub_details(row.get("details") or {})
        if isinstance(details, dict):
            detail_text = "; ".join(f"{key}={value}" for key, value in sorted(details.items()))
        else:
            detail_text = str(details)
        export_rows.append(
            {
                "created_at": row.get("created_at") or "",
                "actor_email": row.get("actor_email") or "",
                "action": row["action"],
                "target": row.get("target") or "",
                "outcome": row["outcome"],
                "details": detail_text[:1000],
            }
        )
    return {
        "generated_at": generated,
        "total": len(export_rows),
        "by_action": dict(sorted(by_action.items())),
        "by_outcome": dict(sorted(by_outcome.items())),
        "columns": list(EXPORT_COLUMNS),
        "rows": export_rows,
    }


def export_csv(export) -> str:
    """Render an audit export as CSV text (pure)."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=export["columns"], extrasaction="ignore")
    writer.writeheader()
    for row in export["rows"]:
        writer.writerow(row)
    return buffer.getvalue()
