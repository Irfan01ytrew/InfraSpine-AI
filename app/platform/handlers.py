"""Built-in outbox consumers. Delivery is at-least-once, so every handler is idempotent."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.digital_twin import feed_records as twin_feed_records
from app.digital_twin import feed_changes as twin_feeds
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


@register("twin.proposal_action")
async def _on_twin_proposal_action(db: AsyncSession, event: PlatformOutbox) -> None:
    # Step K v2 slice 6: an approved twin proposal was dispatched.
    # The event row itself is the durable action record (the
    # proposal row carries the execution id + external ref); the
    # physical follow-up (assigning a backup, reviewing a supplier)
    # is a human/operator step the event triggers, so the handler
    # only logs receipt — the dispatch is visible in the audit
    # trail and the event is never silently swallowed.
    logger.info("twin proposal action dispatched: proposal %s action %s",
                event.payload.get("proposal_id"), event.payload.get("action_kind"))


async def _on_twin_feed_event(db: AsyncSession, event: PlatformOutbox) -> None:
    # Step K v2 slice 7: a live-sync feed event becomes a pending
    # graph proposal through the governed-change path. The twin never
    # writes the graph here — apply_feed only inserts a GraphChange
    # a human approves or rejects. An event with no resolvable graph
    # tenant (payload tenant_id, else an outbox scope_id that IS a
    # graph tenant id) is skipped with a warning: a graph tenant id
    # is not an org id, and guessing would attach the proposal to
    # the wrong company. A malformed event raises, so the outbox
    # retries it and dead-letters it for an operator — never a
    # silent drop.
    change = twin_feeds.translate_event(event.event_type, event.payload or {})
    if change is None:
        return
    tenant_id = await twin_feed_records.resolve_tenant(db, event)
    if tenant_id is None:
        logger.warning("twin feed event %s (%s) has no graph tenant; skipped",
                       event.event_type, event.id)
        return
    await twin_feed_records.apply_feed(db, tenant_id, change, observed_at=event.created_at)


for _feed_event_type in twin_feeds.FEED_EVENTS:
    register(_feed_event_type)(_on_twin_feed_event)
