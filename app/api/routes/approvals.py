"""Platform approvals inbox (step E): every pending approval the caller may decide, bound to the exact action.

The reviewer sees the canonical action (type, params, amount, currency, subject) and its hash, and decides by
presenting that hash; a changed action, an expired request, the requester themself and separation of duties are
refused by `decide_approval`. Finance payments have their own richer view under /api/finance/approvals.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.errors import NotFoundError
from app.platform.access import authorize, principal_for
from app.platform.approvals import decide_approval
from app.platform.models import PlatformApprovalRequest

router = APIRouter(prefix='/api/approvals', tags=['approvals'])


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['approve', 'reject']
    presented_hash: str = Field(min_length=64, max_length=64, pattern=r'^[0-9a-f]{64}$')
    note: str = Field(default='', max_length=500)


def _out(row: PlatformApprovalRequest, user_id) -> dict:
    return {'id': str(row.id), 'action_type': row.action_type, 'action': row.action, 'action_hash': row.action_hash,
            'subject': row.subject, 'amount': None if row.amount is None else format(row.amount, 'f'),
            'currency': row.currency, 'required_permission': row.required_permission,
            'requested_by': str(row.requested_by), 'own_request': row.requested_by == user_id,
            'expires_at': row.expires_at.isoformat(), 'status': row.status}


@router.get('')
async def inbox(db: DbSession, user: CurrentUser):
    p = principal_for(user)
    now = datetime.now(UTC)
    rows = (await db.scalars(select(PlatformApprovalRequest).where(
        PlatformApprovalRequest.status == 'pending', PlatformApprovalRequest.expires_at > now,
    ).order_by(PlatformApprovalRequest.created_at).limit(500))).all()
    out = []
    for row in rows:  # authorise each on its own scope (ancestors included); never trust the list query alone
        decision = await authorize(db, p, row.required_permission, row.scope_id, amount=row.amount)
        if decision.allowed:
            out.append(_out(row, user.id))
    return {'approvals': out}


@router.post('/{approval_id}/decide')
async def decide(approval_id: UUID, body: DecisionIn, db: DbSession, user: CurrentUser):
    p = principal_for(user)
    row = await db.get(PlatformApprovalRequest, approval_id)
    if row is None:
        raise NotFoundError('Approval not found.')
    row = await decide_approval(db, p, approval_id, body.decision, presented_hash=body.presented_hash, note=body.note)
    # Wake a workflow step waiting on this approval (platform.approval / finance handlers poll it).
    from sqlalchemy import update

    from app.db.workflow_engine_models import WorkflowRun, WorkflowStep
    params = (row.action or {}).get('params') or {}
    run_id = params.get('run_id')
    if run_id:
        try:
            rid = UUID(str(run_id))
        except ValueError:
            rid = None
        if rid:
            now = datetime.now(UTC)
            await db.execute(update(WorkflowStep).where(WorkflowStep.run_id == rid, WorkflowStep.status == 'waiting')
                             .values(available_at=now))
            await db.execute(update(WorkflowRun).where(WorkflowRun.id == rid).values(wake_at=now))
    await db.commit()
    return _out(row, user.id)
