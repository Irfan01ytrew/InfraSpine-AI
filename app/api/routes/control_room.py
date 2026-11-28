"""Step Q (slices 1–2): control-room status API + approval-gated controls.

Slice 1 is read-only (overview). Slice 2 adds controls: pausing/resuming
a unified run and pausing/resuming/assigning an Agent Studio agent. Every
control needs ``workflow.manage`` on the scope *and* an exact-action
platform approval (see ``app/control_room/controls.py``): request it with
``.../control-request``, have a different manager approve it in the
approvals inbox, then execute it with ``.../control``. No control runs
without a matching, unexpired, single-use approval.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession, audit_context
from app.control_room.controls import (
    execute_agent_control,
    execute_run_control,
    request_agent_control,
    request_run_control,
)
from app.control_room.service import control_room_overview
from app.workflows.security import require_scope
from app.workflows.service import row_dict

router = APIRouter(prefix="/api/control-room", tags=["control-room"])


class WorkerHealth(BaseModel):
    engine_on: bool


class LatestRun(BaseModel):
    id: UUID
    status: str
    failure_code: str | None = None
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AgentEntry(BaseModel):
    id: UUID
    name: str
    owner_id: UUID
    enabled: bool
    paused: bool
    latest_run: LatestRun | None = None


class RunSummary(BaseModel):
    id: UUID
    workflow_id: UUID
    workflow_name: str
    status: str
    phase: str
    origin: str
    origin_ref: str | None = None
    blocked_reason: str | None = None
    failure_code: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class BlockerEntry(BaseModel):
    kind: str
    run_id: UUID
    origin: str
    reason: str | None = None
    created_at: datetime


class ControlRoomOverview(BaseModel):
    scope_id: UUID
    worker: WorkerHealth
    runs_by_status: dict[str, int]
    runs_by_origin: dict[str, int]
    agents: list[AgentEntry]
    recent_runs: list[RunSummary]
    blockers: list[BlockerEntry]


@router.get("/scopes/{scope_id}/overview", response_model=ControlRoomOverview)
async def get_control_room_overview(
    db: DbSession,
    user: CurrentUser,
    scope_id: UUID,
    limit: int = Query(default=10, ge=1, le=50),
):
    """Read-only snapshot for one department scope.

    Authorization goes through ``app.platform`` (``require_scope`` → group
    gate, permission ``workflow.access``): unknown scope or non-member → 404,
    member without the permission → 403. No control actions live here.
    """
    await require_scope(db, user, scope_id)
    return await control_room_overview(db, scope_id=scope_id, limit=limit)


class ApprovalRequestOut(BaseModel):
    """The approval a control needs: its id and the exact action hash the
    approver must review in the approvals inbox."""

    id: UUID
    action_type: str
    action_hash: str
    status: str
    expires_at: datetime


class RunControlRequest(BaseModel):
    command: Literal["pause", "resume"]


class RunControlExecute(BaseModel):
    command: Literal["pause", "resume"]
    approval_id: UUID


class AgentControlRequest(BaseModel):
    command: Literal["pause", "resume", "assign"]
    assignee_id: UUID | None = None


class AgentControlExecute(BaseModel):
    command: Literal["pause", "resume", "assign"]
    approval_id: UUID
    assignee_id: UUID | None = None


def _approval_out(row) -> ApprovalRequestOut:
    return ApprovalRequestOut(
        id=row.id,
        action_type=row.action_type,
        action_hash=row.action_hash,
        status=row.status,
        expires_at=row.expires_at,
    )


@router.post(
    "/scopes/{scope_id}/runs/{run_id}/control-request",
    response_model=ApprovalRequestOut,
)
async def request_run_control_endpoint(
    db: DbSession,
    user: CurrentUser,
    scope_id: UUID,
    run_id: UUID,
    body: RunControlRequest,
):
    """Ask for approval to pause/resume one run. The caller needs
    ``workflow.manage``; a different manager approves the exact action
    (``POST /api/approvals/{id}/decide``) before it can run."""
    row = await request_run_control(db, user, scope_id, run_id, body.command)
    return _approval_out(row)


@router.post("/scopes/{scope_id}/runs/{run_id}/control")
async def execute_run_control_endpoint(
    db: DbSession,
    user: CurrentUser,
    scope_id: UUID,
    run_id: UUID,
    body: RunControlExecute,
    request: Request,
):
    """Pause/resume one run through the unified engine, consuming the
    exact-action approval. The approval and the state change commit
    together; a reused, expired, or mismatched approval is refused."""
    run = await execute_run_control(
        db,
        user,
        request.app.state.cipher,
        scope_id,
        run_id,
        body.command,
        body.approval_id,
        ctx=audit_context(request, user),
    )
    await db.commit()
    return row_dict(run, exclude={"payload"})


@router.post(
    "/scopes/{scope_id}/agents/{agent_id}/control-request",
    response_model=ApprovalRequestOut,
)
async def request_agent_control_endpoint(
    db: DbSession,
    user: CurrentUser,
    scope_id: UUID,
    agent_id: UUID,
    body: AgentControlRequest,
):
    """Ask for approval to pause/resume/assign one agent owned by a
    member of this scope. Assign needs an assignee who is a scope member;
    the Master Agent cannot be reassigned."""
    row = await request_agent_control(
        db, user, scope_id, agent_id, body.command, body.assignee_id
    )
    return _approval_out(row)


@router.post("/scopes/{scope_id}/agents/{agent_id}/control")
async def execute_agent_control_endpoint(
    db: DbSession,
    user: CurrentUser,
    scope_id: UUID,
    agent_id: UUID,
    body: AgentControlExecute,
    request: Request,
):
    """Pause/resume/assign one agent, consuming the exact-action approval.
    Pausing an agent stops its work at the next attempt (the unified
    handler honours the flag); assigning transfers ownership."""
    agent = await execute_agent_control(
        db,
        user,
        scope_id,
        agent_id,
        body.command,
        body.approval_id,
        assignee_id=body.assignee_id,
        ctx=audit_context(request, user),
    )
    await db.commit()
    return {
        "id": str(agent.id),
        "name": agent.name,
        "owner_id": str(agent.owner_id),
        "enabled": agent.enabled,
        "paused": agent.paused,
    }
