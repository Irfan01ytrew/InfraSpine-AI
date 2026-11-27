"""REST API endpoints for controlled agent action proposals, human approvals, and execution."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.deps import CurrentUser, DbSession, audit_context, get_current_user
from app.db.models import ROLE_ADMIN, ROLE_MANAGER, AgentActionProposal, DepartmentManager
from app.schemas.agent_actions import (
    ActionDecisionRequest,
    ActionExecutionResponse,
    ActionProposalCreate,
    ActionProposalListResponse,
    ActionProposalOut,
)
from app.services import agent_actions

router = APIRouter(prefix="/api/agents/actions", tags=["agent-actions"], dependencies=[Depends(get_current_user)])


async def _to_out(db: DbSession, user: CurrentUser, p: AgentActionProposal) -> ActionProposalOut:
    can_approve = False
    if p.status == "pending_approval" and p.requester_user_id != user.id and not (p.expires_at < agent_actions._utcnow()):
        if user.role == ROLE_ADMIN:
            can_approve = True
        elif user.role == ROLE_MANAGER and p.department_id is not None:
            mgr = await db.get(DepartmentManager, (p.department_id, user.id))
            can_approve = mgr is not None

    return ActionProposalOut(
        id=p.id,
        agent_profile_id=p.agent_profile_id,
        requester_user_id=p.requester_user_id,
        department_id=p.department_id,
        action_type=p.action_type,
        parameters=p.parameters,
        rationale=p.rationale,
        idempotency_key=p.idempotency_key,
        status=p.status,
        approver_user_id=p.approver_user_id,
        approval_rationale=p.approval_rationale,
        requested_at=p.requested_at,
        expires_at=p.expires_at,
        reviewed_at=p.reviewed_at,
        executed_at=p.executed_at,
        execution_result=p.execution_result,
        is_expired=p.expires_at < agent_actions._utcnow(),
        can_approve=can_approve,
    )


@router.post("/proposals", response_model=ActionProposalOut, status_code=status.HTTP_201_CREATED)
async def propose_agent_action(
    payload: ActionProposalCreate,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> ActionProposalOut:
    """Propose an action requiring human approval before execution."""
    proposal = await agent_actions.propose_action(
        db,
        user,
        audit_context(request, user),
        action_type=payload.action_type,
        parameters=payload.parameters,
        rationale=payload.rationale,
        idempotency_key=payload.idempotency_key,
        agent_profile_id=payload.agent_profile_id,
        department_id=payload.department_id,
        ttl_hours=payload.ttl_hours,
    )
    return await _to_out(db, user, proposal)


@router.get("/proposals", response_model=ActionProposalListResponse)
async def list_agent_action_proposals(
    db: DbSession,
    user: CurrentUser,
    department_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> ActionProposalListResponse:
    """List proposals accessible to the caller."""
    items = await agent_actions.list_proposals(
        db,
        user,
        department_id=department_id,
        status=status_filter,
    )
    return ActionProposalListResponse(
        proposals=[await _to_out(db, user, p) for p in items],
        total=len(items),
    )


@router.get("/proposals/{proposal_id}", response_model=ActionProposalOut)
async def get_agent_action_proposal(
    proposal_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
) -> ActionProposalOut:
    """Get single proposal details."""
    proposal = await agent_actions.get_proposal(db, user, proposal_id)
    return await _to_out(db, user, proposal)


@router.post("/proposals/{proposal_id}/approve", response_model=ActionProposalOut)
async def approve_agent_action(
    proposal_id: uuid.UUID,
    payload: ActionDecisionRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> ActionProposalOut:
    """Authorize a proposed action. Requesters cannot self-approve."""
    proposal = await agent_actions.approve_action(
        db,
        user,
        audit_context(request, user),
        proposal_id,
        rationale=payload.rationale,
    )
    return await _to_out(db, user, proposal)


@router.post("/proposals/{proposal_id}/reject", response_model=ActionProposalOut)
async def reject_agent_action(
    proposal_id: uuid.UUID,
    payload: ActionDecisionRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> ActionProposalOut:
    """Reject a proposed action."""
    proposal = await agent_actions.reject_action(
        db,
        user,
        audit_context(request, user),
        proposal_id,
        rationale=payload.rationale,
    )
    return await _to_out(db, user, proposal)


@router.post("/proposals/{proposal_id}/execute", response_model=ActionExecutionResponse)
async def execute_agent_action(
    proposal_id: uuid.UUID,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> ActionExecutionResponse:
    """Execute an approved action through registered idempotent adapters."""
    proposal = await agent_actions.execute_action(
        db,
        user,
        audit_context(request, user),
        proposal_id,
    )
    return ActionExecutionResponse(
        proposal_id=proposal.id,
        status=proposal.status,
        action_type=proposal.action_type,
        executed_at=proposal.executed_at,
        execution_result=proposal.execution_result or {},
    )
