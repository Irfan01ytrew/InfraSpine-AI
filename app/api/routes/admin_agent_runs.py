"""Admin-only, explicit retention controls for encrypted agent-run content."""

from fastapi import APIRouter, Request

from app.api.deps import AdminUser, DbSession, audit_context
from app.schemas.agent_run_retention import (
    AgentRunRetentionPreviewRequest,
    AgentRunRetentionPreviewResponse,
    AgentRunRetentionPurgeRequest,
    AgentRunRetentionPurgeResponse,
)
from app.services.agent_run_retention import (
    count_eligible_run_content,
    purge_expired_run_content,
    retention_cutoff,
)

router = APIRouter(prefix="/api/admin/agent-runs/retention", tags=["agent-run-retention"])


@router.post("/preview", response_model=AgentRunRetentionPreviewResponse)
async def preview_agent_run_content_retention(
    payload: AgentRunRetentionPreviewRequest,
    db: DbSession,
    admin: AdminUser,
) -> AgentRunRetentionPreviewResponse:
    cutoff = retention_cutoff(payload.retention_days)
    eligible = await count_eligible_run_content(db, cutoff)
    return AgentRunRetentionPreviewResponse(
        retention_days=payload.retention_days,
        cutoff_at=cutoff,
        eligible_runs=eligible,
        preserved_fields=["status", "attempts", "failure_code", "requested_at", "started_at", "finished_at", "audit history"],
    )


@router.post("/purge", response_model=AgentRunRetentionPurgeResponse)
async def purge_agent_run_content_retention(
    payload: AgentRunRetentionPurgeRequest,
    request: Request,
    db: DbSession,
    admin: AdminUser,
) -> AgentRunRetentionPurgeResponse:
    purged, cutoff = await purge_expired_run_content(
        db,
        audit_context(request, admin),
        retention_days=payload.retention_days,
        limit=payload.batch_size,
    )
    remaining = await count_eligible_run_content(db, cutoff)
    return AgentRunRetentionPurgeResponse(
        retention_days=payload.retention_days,
        cutoff_at=cutoff,
        purged_runs=purged,
        remaining_eligible_runs=remaining,
        batch_size=payload.batch_size,
        audit_event="agent.profile_run_content_purged",
    )
