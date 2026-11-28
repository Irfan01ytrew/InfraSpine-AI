"""Audit-completeness check for WhatsApp runs (Step F slice 6).

Every WhatsApp inbound must end with exactly one terminal audit event:
the run was accepted (delivered, or re-opened with a template when the
24-hour window had expired) or it failed. This module answers, for one
inbound, whether that terminal event is present in the append-only audit
log — a library check for an admin review or a scheduled audit, not part
of the send path (the worker must never block a reply on an audit query).

A still-running inbound (``queued``/``processing``) has no terminal event
yet, so its verdict is ``pending`` — not incomplete.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.agent_studio_whatsapp_models import AgentStudioWhatsAppInbound as Inbound
from app.db.models import AuditLog

# Terminal events, as groups of alternatives: at least one event of the
# group must be present for the inbound's terminal state.
TERMINAL_EVENT_GROUPS: dict[str, list[list[str]]] = {
    "completed": [
        ["integration.whatsapp_accepted", "integration.whatsapp_template_reopen"],
    ],
    "failed": [
        ["integration.whatsapp_failed", "integration.whatsapp_stale_message"],
    ],
}


def expected_event_groups(inbound_status: str) -> list[list[str]]:
    """The audit-event groups a terminal inbound must have (empty while running)."""
    return [list(group) for group in TERMINAL_EVENT_GROUPS.get(inbound_status, [])]


async def check_audit_completeness(db: AsyncSession, inbound_id: uuid.UUID) -> dict:
    """The audit verdict for one inbound: complete / pending / incomplete.

    ``missing`` names the first event of each group with nothing present;
    ``present`` is every WhatsApp audit event recorded for the inbound.
    """
    inbound = await db.get(Inbound, inbound_id)
    if inbound is None:
        raise NotFoundError("WhatsApp message not found.")
    rows = await db.scalars(
        select(AuditLog.action).where(AuditLog.target == str(inbound_id))
    )
    present = sorted({action for action in rows if action.startswith("integration.whatsapp")})
    present_set = set(present)
    missing = [
        group[0]
        for group in expected_event_groups(inbound.status)
        if not any(event in present_set for event in group)
    ]
    if not expected_event_groups(inbound.status):
        status = "pending"
    elif missing:
        status = "incomplete"
    else:
        status = "complete"
    return {
        "inbound_id": str(inbound_id),
        "inbound_status": inbound.status,
        "status": status,
        "expected": expected_event_groups(inbound.status),
        "present": present,
        "missing": missing,
    }
