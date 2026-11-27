"""Generic action handlers shipped with the runtime."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from app.core.errors import InvalidInputError
from app.platform.approvals import ActionSpec, request_approval
from app.platform.execution import VerifiedResult
from app.platform.models import PlatformApprovalRequest
from app.workflows.actions import ActionContext, ActionOutcome, register


def approval_spec(ctx: ActionContext) -> ActionSpec:
    """The exact action an approver sees. Built only from the node's *published* values (resolved against the run
    inputs) — never from document or message text — and bound to this run and step."""
    values = ctx.values
    action_type = values.get('action_type')
    if not isinstance(action_type, str) or not 3 <= len(action_type) <= 80:
        raise InvalidInputError('Approval action type is required.')
    amount = values.get('amount')
    try:
        amount = Decimal(str(amount)) if amount is not None else None
    except InvalidOperation:
        raise InvalidInputError('Approval amount is invalid.') from None
    params = values.get('params') or {}
    if not isinstance(params, dict):
        raise InvalidInputError('Approval parameters must be an object.')
    params = {k: (str(v) if isinstance(v, float) else v) for k, v in params.items()}
    return ActionSpec(action_type, ctx.platform_scope_id, {**params, 'run_id': str(ctx.run_id), 'step_id': str(ctx.step_id)},
                      amount=amount, currency=values.get('currency'), subject=values.get('subject') or f'workflow:{ctx.run_id}')


class PlatformApprovalHandler:
    """Wait for one platform approval (request/decide/consume, exact-action hash, expiry, single use)."""

    name = 'platform.approval'
    label = 'Approval of the exact action (platform)'
    module = 'platform'
    permission = None

    async def run(self, ctx: ActionContext) -> ActionOutcome:
        spec = approval_spec(ctx)
        required = ctx.values.get('required_permission')
        if not isinstance(required, str) or not required:
            raise InvalidInputError('The approving permission is required.')
        approval_id = ctx.checkpoint.get('approval_id')
        row = await ctx.db.get(PlatformApprovalRequest, uuid.UUID(approval_id), populate_existing=True) if approval_id else None
        if row is None or row.status in {'expired', 'cancelled'} or (row.status == 'pending' and row.expires_at <= datetime.now(UTC)):
            hours = int(ctx.values.get('ttl_hours') or 24)
            row = await request_approval(ctx.db, ctx.principal, spec, required_permission=required,
                                         ttl=timedelta(hours=max(1, min(hours, 168))))
            ctx.checkpoint['approval_id'] = str(row.id)
            ctx.checkpoint['requests'] = int(ctx.checkpoint.get('requests', 0)) + 1
            ctx.note('approval.requested', approval_id=str(row.id), action_hash=row.action_hash)
            return ActionOutcome('waiting', {'approval_id': str(row.id), 'action_hash': row.action_hash}, poll_seconds=10)
        if row.status == 'pending':
            return ActionOutcome('waiting', {'approval_id': str(row.id)}, poll_seconds=10)
        if row.status == 'rejected':
            return ActionOutcome('failed', {'approval_id': str(row.id)}, 'approval_rejected', retryable=False)
        return ActionOutcome('done', {'approval_id': str(row.id), 'action_hash': row.action_hash,
                                      'approved_by': str(row.decided_by)})

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult:
        row = await ctx.db.scalar(select(PlatformApprovalRequest).where(
            PlatformApprovalRequest.id == uuid.UUID(output['approval_id'])).execution_options(populate_existing=True))
        ok = (row is not None and row.status in {'approved', 'consumed'} and row.action_hash == approval_spec(ctx).hash()
              and row.scope_id == ctx.platform_scope_id)
        return VerifiedResult(ok, {'approval_id': output['approval_id'], 'status': row.status if row else None})


register(PlatformApprovalHandler())
