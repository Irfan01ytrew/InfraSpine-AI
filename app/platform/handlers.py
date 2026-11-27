"""Built-in outbox consumers. Delivery is at-least-once, so every handler is idempotent."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.models import PlatformOutbox
from app.platform.outbox import register

logger = logging.getLogger("app.platform.handlers")


async def cancel_queued_work(db: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    """Stop work queued for a user who lost access: agent runs and their pending tool approvals, and connector
    jobs not yet dispatched. Work already dispatched is left to its own reconciliation (never blindly undone)."""
    from app.db.agent_studio_models import AgentStudioApproval, AgentStudioRun
    from app.integrations.models import IntegrationJob

    now = datetime.now(UTC)
    runs = await db.execute(update(AgentStudioRun).where(
        AgentStudioRun.owner_id == user_id, AgentStudioRun.status.in_(("queued", "awaiting_approval"))
    ).values(status="cancelled", failure_code="access_revoked", finished_at=now))
    approvals = await db.execute(update(AgentStudioApproval).where(
        AgentStudioApproval.owner_id == user_id, AgentStudioApproval.status == "pending"
    ).values(status="rejected", decision_note="Access revoked", decided_at=now))
    jobs = await db.execute(update(IntegrationJob).where(
        IntegrationJob.owner_id == user_id, IntegrationJob.status.in_(("pending_approval", "queued"))
    ).values(status="cancelled", last_error="access_revoked"))
    return {"agent_runs": runs.rowcount or 0, "agent_approvals": approvals.rowcount or 0,
            "connector_jobs": jobs.rowcount or 0}


@register("identity.session_revoked")
async def _on_access_revoked(db: AsyncSession, event: PlatformOutbox) -> None:
    # The revoke already did this in its own transaction; repeat it for work queued in the meantime.
    await cancel_queued_work(db, uuid.UUID(event.payload["user_id"]))


@register("document.acl_changed")
@register("document.deleted")
async def _on_document_access_changed(db: AsyncSession, event: PlatformOutbox) -> None:
    # Retrieval, citations and stored answers are authorised live in SQL (app.rag.access.readable_by,
    # app.services.conversations), so nothing cached can outlive the change. Drop this process's parsed-table
    # cache too, so no derived text of the document is kept in memory.
    from app.rag import tables

    tables._cache.clear()
    logger.info("document access changed: %s %s", event.event_type, event.payload.get("document_id"))
