"""Controlled agent actions, human approvals, and idempotent execution adapters."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AgentActionUnavailableError,
    ConflictError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
from app.db.models import (
    ACTION_APPROVED,
    ACTION_EXECUTED,
    ACTION_EXPIRED,
    ACTION_PENDING_APPROVAL,
    ACTION_REJECTED,
    ACTION_STATUSES,
    ACTION_TYPES,
    ACL_ACTIVE,
    ROLE_ADMIN,
    ROLE_MANAGER,
    SOURCE_UPLOAD,
    AccessGroup,
    AccessGroupMember,
    AgentActionProposal,
    DepartmentManager,
    Document,
    User,
    VISIBILITY_SHARED,
)
from app.services import audit
from app.services.audit import AuditContext
from app.rag.access import can_modify, can_publish

logger = logging.getLogger(__name__)

_NOT_FOUND = "Action proposal not found."


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _validate_action_params(action_type: str, parameters: dict) -> None:
    if not isinstance(parameters, dict):
        raise InvalidInputError("Action parameters must be a dictionary.")
    if action_type == "webhook_dispatch":
        url = parameters.get("url")
        if not url or not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
            raise InvalidInputError("Webhook dispatch requires a valid HTTP or HTTPS destination url.")
        if "payload" not in parameters or not isinstance(parameters["payload"], dict):
            raise InvalidInputError("Webhook dispatch requires a dictionary payload.")
    elif action_type == "document_publish":
        doc_id = parameters.get("document_id")
        if not doc_id:
            raise InvalidInputError("Document publish requires a document_id parameter.")
        try:
            uuid.UUID(str(doc_id))
        except ValueError:
            raise InvalidInputError("document_id must be a valid UUID.") from None
    elif action_type == "notification_broadcast":
        title = parameters.get("title")
        message = parameters.get("message")
        if not title or not isinstance(title, str) or len(title.strip()) < 2:
            raise InvalidInputError("Notification broadcast requires a non-empty title.")
        if not message or not isinstance(message, str) or len(message.strip()) < 2:
            raise InvalidInputError("Notification broadcast requires a non-empty message.")
    else:
        raise InvalidInputError(f"Unsupported action type '{action_type}'.")


async def propose_action(
    db: AsyncSession,
    actor: User,
    ctx: AuditContext,
    *,
    action_type: str,
    parameters: dict,
    rationale: str,
    idempotency_key: str,
    agent_profile_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    ttl_hours: int = 24,
) -> AgentActionProposal:
    """Propose an action requiring out-of-model human approval."""
    if action_type not in ACTION_TYPES:
        raise InvalidInputError(f"Unsupported action type '{action_type}'. Supported: {', '.join(sorted(ACTION_TYPES))}.")
    
    clean_rationale = rationale.strip()
    if len(clean_rationale) < 3 or len(clean_rationale) > 1000:
        raise InvalidInputError("Rationale must be between 3 and 1000 characters.")
    
    clean_key = idempotency_key.strip()
    if len(clean_key) < 8 or len(clean_key) > 128:
        raise InvalidInputError("Idempotency key must be between 8 and 128 characters.")
    
    _validate_action_params(action_type, parameters)

    # Idempotency check: if proposal exists with identical idempotency key, return it
    existing = (
        await db.execute(
            select(AgentActionProposal).where(AgentActionProposal.idempotency_key == clean_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    if department_id is not None:
        group = await db.get(AccessGroup, department_id)
        if group is None or group.kind != "department":
            raise NotFoundError("The specified department does not exist.")

    expires_at = _utcnow() + timedelta(hours=ttl_hours)
    proposal = AgentActionProposal(
        agent_profile_id=agent_profile_id,
        requester_user_id=actor.id,
        department_id=department_id,
        action_type=action_type,
        parameters=parameters,
        rationale=clean_rationale,
        idempotency_key=clean_key,
        status=ACTION_PENDING_APPROVAL,
        expires_at=expires_at,
    )
    db.add(proposal)
    await db.flush()

    audit.record(
        db,
        ctx,
        "agent.action_proposed",
        target=str(proposal.id),
        details={
            "action_type": action_type,
            "department_id": str(department_id) if department_id else None,
            "idempotency_key": clean_key,
            "ttl_hours": ttl_hours,
        },
    )
    await db.commit()
    await db.refresh(proposal)
    return proposal


async def _can_manage_proposal(db: AsyncSession, actor: User, proposal: AgentActionProposal) -> bool:
    if actor.role == ROLE_ADMIN:
        return True
    if actor.role == ROLE_MANAGER and proposal.department_id is not None:
        manager_grant = await db.get(DepartmentManager, (proposal.department_id, actor.id))
        return manager_grant is not None
    return False


async def list_proposals(
    db: AsyncSession,
    actor: User,
    *,
    department_id: uuid.UUID | None = None,
    status: str | None = None,
) -> list[AgentActionProposal]:
    """List proposals accessible to the caller, expiring overdue items."""
    query = select(AgentActionProposal)
    
    if actor.role == ROLE_ADMIN:
        if department_id is not None:
            query = query.where(AgentActionProposal.department_id == department_id)
    elif actor.role == ROLE_MANAGER:
        managed = (
            await db.execute(
                select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == actor.id)
            )
        ).scalars().all()
        query = query.where(
            (AgentActionProposal.department_id.in_(managed))
            | (AgentActionProposal.requester_user_id == actor.id)
        )
        if department_id is not None:
            if department_id not in managed:
                return []
            query = query.where(AgentActionProposal.department_id == department_id)
    else:
        query = query.where(AgentActionProposal.requester_user_id == actor.id)
        if department_id is not None:
            query = query.where(AgentActionProposal.department_id == department_id)

    if status is not None:
        if status not in ACTION_STATUSES:
            raise InvalidInputError("Invalid status filter.")
        query = query.where(AgentActionProposal.status == status)

    query = query.order_by(AgentActionProposal.requested_at.desc())
    items = list((await db.execute(query)).scalars().all())

    # Expire any pending items whose TTL has passed
    now = _utcnow()
    updated = False
    for item in items:
        if item.status == ACTION_PENDING_APPROVAL and item.expires_at < now:
            item.status = ACTION_EXPIRED
            updated = True
    if updated:
        await db.commit()

    return items


async def get_proposal(db: AsyncSession, actor: User, proposal_id: uuid.UUID) -> AgentActionProposal:
    proposal = await db.get(AgentActionProposal, proposal_id)
    if proposal is None:
        raise NotFoundError(_NOT_FOUND)
    
    can_access = (
        actor.role == ROLE_ADMIN
        or proposal.requester_user_id == actor.id
        or await _can_manage_proposal(db, actor, proposal)
    )
    if not can_access:
        raise PermissionDeniedError("Access to this action proposal is denied.")
    
    # Auto-expire if overdue
    if proposal.status == ACTION_PENDING_APPROVAL and proposal.expires_at < _utcnow():
        proposal.status = ACTION_EXPIRED
        await db.commit()
        await db.refresh(proposal)

    return proposal


async def approve_action(
    db: AsyncSession,
    actor: User,
    ctx: AuditContext,
    proposal_id: uuid.UUID,
    *,
    rationale: str,
) -> AgentActionProposal:
    """Authorize a proposed action. Enforces non-self approval, role, and TTL checks."""
    proposal = await db.get(AgentActionProposal, proposal_id)
    if proposal is None:
        raise NotFoundError(_NOT_FOUND)
    
    can_manage = await _can_manage_proposal(db, actor, proposal)
    if not can_manage:
        raise PermissionDeniedError("Only an administrator or department manager can approve actions.")
    
    # NON-SELF APPROVAL RULE: Requesters cannot approve their own action proposals
    if proposal.requester_user_id == actor.id:
        raise PermissionDeniedError("Requesters cannot approve their own action proposals.")

    # Expiration check
    if proposal.expires_at < _utcnow():
        proposal.status = ACTION_EXPIRED
        await db.commit()
        raise ConflictError("This action proposal has expired and can no longer be approved.")

    if proposal.status != ACTION_PENDING_APPROVAL:
        raise ConflictError(f"Action proposal cannot be approved in status '{proposal.status}'.")

    clean_rationale = rationale.strip()
    if len(clean_rationale) < 3 or len(clean_rationale) > 1000:
        raise InvalidInputError("Approval rationale must be between 3 and 1000 characters.")

    proposal.status = ACTION_APPROVED
    proposal.approver_user_id = actor.id
    proposal.approval_rationale = clean_rationale
    proposal.reviewed_at = _utcnow()
    await db.flush()

    audit.record(
        db,
        ctx,
        "agent.action_approved",
        target=str(proposal.id),
        details={"action_type": proposal.action_type, "approver_id": str(actor.id)},
    )
    await db.commit()
    await db.refresh(proposal)
    return proposal


async def reject_action(
    db: AsyncSession,
    actor: User,
    ctx: AuditContext,
    proposal_id: uuid.UUID,
    *,
    rationale: str,
) -> AgentActionProposal:
    """Reject an action proposal. Enforces role and state checks."""
    proposal = await db.get(AgentActionProposal, proposal_id)
    if proposal is None:
        raise NotFoundError(_NOT_FOUND)
    
    can_manage = await _can_manage_proposal(db, actor, proposal)
    if not can_manage:
        raise PermissionDeniedError("Only an administrator or department manager can reject actions.")

    if proposal.status != ACTION_PENDING_APPROVAL:
        raise ConflictError(f"Action proposal cannot be rejected in status '{proposal.status}'.")

    clean_rationale = rationale.strip()
    if len(clean_rationale) < 3 or len(clean_rationale) > 1000:
        raise InvalidInputError("Rejection rationale must be between 3 and 1000 characters.")

    proposal.status = ACTION_REJECTED
    proposal.approver_user_id = actor.id
    proposal.approval_rationale = clean_rationale
    proposal.reviewed_at = _utcnow()
    await db.flush()

    audit.record(
        db,
        ctx,
        "agent.action_rejected",
        target=str(proposal.id),
        details={"action_type": proposal.action_type, "approver_id": str(actor.id)},
    )
    await db.commit()
    await db.refresh(proposal)
    return proposal


async def _execute_adapter(
    db: AsyncSession, proposal: AgentActionProposal, actor: User | None = None
) -> dict:
    """Idempotent adapter execution outside the LLM."""
    action_type = proposal.action_type
    params = proposal.parameters

    if action_type == "webhook_dispatch":
        # Never turn an unimplemented external side effect into a successful execution record.
        raise AgentActionUnavailableError()

    if action_type == "document_publish":
        doc_id = uuid.UUID(str(params["document_id"]))
        doc = await db.get(Document, doc_id)
        if doc is None:
            raise NotFoundError("The document to publish was not found.")
        if doc.source_system != SOURCE_UPLOAD or doc.acl_status != ACL_ACTIVE:
            raise PermissionDeniedError("Only active uploaded documents can be published organization-wide.")
        if actor is None or not can_publish(actor, VISIBILITY_SHARED) or not can_modify(actor, doc):
            raise PermissionDeniedError("You do not have permission to publish this document organization-wide.")
        doc.visibility = VISIBILITY_SHARED
        await db.flush()
        return {
            "published": True,
            "document_id": str(doc.id),
            "filename": doc.filename,
            "visibility": doc.visibility,
        }

    if action_type == "notification_broadcast":
        # The durable, scoped in-app delivery adapter is the approved `notify_member` workflow.
        raise AgentActionUnavailableError(
            "This broadcast adapter is not available. Use the approved department notification workflow; "
            "this proposal remains approved and was not marked executed."
        )

    raise InvalidInputError(f"Unknown action adapter '{action_type}'.")


async def execute_action(
    db: AsyncSession,
    actor: User,
    ctx: AuditContext,
    proposal_id: uuid.UUID,
) -> AgentActionProposal:
    """Execute an approved action proposal through registered idempotent adapters."""
    proposal = await db.get(AgentActionProposal, proposal_id)
    if proposal is None:
        raise NotFoundError(_NOT_FOUND)

    can_manage = await _can_manage_proposal(db, actor, proposal)
    if not can_manage:
        raise PermissionDeniedError("Only an administrator or department manager can execute approved actions.")

    # Idempotent re-execution guard: if already executed, return current record
    if proposal.status == ACTION_EXECUTED:
        return proposal

    if proposal.status != ACTION_APPROVED:
        raise ConflictError(f"Action proposal cannot be executed in status '{proposal.status}'. Must be 'approved'.")

    if proposal.expires_at < _utcnow():
        proposal.status = ACTION_EXPIRED
        await db.commit()
        raise ConflictError("This approved action has expired and cannot be executed.")

    result = await _execute_adapter(db, proposal, actor)
    proposal.status = ACTION_EXECUTED
    proposal.executed_at = _utcnow()
    proposal.execution_result = result
    await db.flush()

    audit.record(
        db,
        ctx,
        "agent.action_executed",
        target=str(proposal.id),
        details={"action_type": proposal.action_type, "executor_id": str(actor.id)},
    )
    await db.commit()
    await db.refresh(proposal)
    return proposal
