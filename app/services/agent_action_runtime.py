"""Agent action proposals (governance) on the unified workflow runtime (step E).

A proposal with a department gets one workflow run (`origin='governance'`, `origin_ref=<proposal id>`):

    start -> approval (waits for the independent human decision on /api/agent-actions/proposals/{id}/approve)
          -> execute  (a department manager or administrator, permission `workflow.manage`; `execute_once`
                       with read-back of the proposal) -> end

The existing API keeps working: `POST /proposals/{id}/execute` uses the same execution path (same idempotency key)
and then hands the workflow step to the executor, so the workflow records the verified result instead of running the
action a second time. A paused or stopped workflow run blocks execution through either path. Proposals without a
department are organisation-wide and keep the legacy path (the runtime is department scoped).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import String, and_, func, select

from app.core.errors import AppError, ConflictError, PermissionDeniedError
from app.db.models import ACTION_APPROVED, ACTION_EXECUTED, ACTION_EXPIRED, ACTION_PENDING_APPROVAL, ACTION_REJECTED
from app.db.models import AgentActionProposal, User
from app.db.workflow_engine_models import WorkflowRun, WorkflowStep
from app.platform.access import principal_for
from app.platform.approvals import ActionSpec
from app.platform.execution import VerifiedResult, execute_once
from app.platform.models import PlatformActionExecution
from app.services.audit import AuditContext
from app.workflows.actions import ActionContext, ActionOutcome, register
from app.workflows.definition import Definition, digest

log = logging.getLogger(__name__)

TEMPLATE_REF = 'governance.agent_actions'
TEMPLATE_NAME = 'Agent action approvals'
TEMPLATE_DESCRIPTION = ('Agent-proposed actions: independent approval, then execution by a department manager or '
                        'administrator, verified by read-back.')
DUTY = 'workflow.manage'


def proposal_spec() -> dict:
    values = {'proposal_id': '$input.proposal_id'}
    return Definition.model_validate({
        'source': 'template',
        'input_form': [{'name': 'proposal_id', 'label': 'Action proposal', 'type': 'text', 'required': True}],
        'nodes': [
            {'id': 'start', 'type': 'start', 'label': 'Action proposed', 'x': 80, 'y': 160},
            {'id': 'approval', 'type': 'action', 'label': 'Independent approval', 'x': 300, 'y': 160, 'risk': 'medium',
             'timeout_seconds': 60, 'max_attempts': 5,
             'config': {'handler': 'governance.proposal_approval', 'auto': True, 'values': values}},
            {'id': 'execute', 'type': 'action', 'label': 'Execute approved action', 'x': 520, 'y': 160, 'risk': 'high',
             'timeout_seconds': 120, 'max_attempts': 5,
             'config': {'handler': 'governance.proposal_execute', 'permission': DUTY, 'values': values}},
            {'id': 'end', 'type': 'end', 'label': 'Executed and verified', 'x': 740, 'y': 160},
        ],
        'edges': [{'source': 'start', 'target': 'approval'}, {'source': 'approval', 'target': 'execute'},
                  {'source': 'execute', 'target': 'end'}],
    }).model_dump(mode='json')


async def mirror_proposal(db, cipher, proposal: AgentActionProposal) -> WorkflowRun | None:
    from app.workflows.service import WorkflowService
    from app.workflows.unified import ensure_system_definition, origin_run
    if proposal.department_id is None or proposal.requester_user_id is None:
        return None
    existing = await origin_run(db, 'governance', proposal.id)
    if existing is not None:
        return existing
    requester = await db.get(User, proposal.requester_user_id)
    if requester is None or not requester.is_active:
        return None
    definition = await ensure_system_definition(
        db, scope_id=proposal.department_id, owner_id=requester.id, origin='governance', origin_ref=TEMPLATE_REF,
        name=TEMPLATE_NAME, description=TEMPLATE_DESCRIPTION, spec=proposal_spec())
    if not definition.enabled:
        return None
    service = WorkflowService(db, requester, cipher, AuditContext(requester.id, requester.email, None, None))
    return await service.trigger(proposal.department_id, definition.id, {'proposal_id': str(proposal.id)},
                                 f'agent-action:{proposal.id}', origin='governance', origin_ref=str(proposal.id))


async def mirror_new_proposal(db, proposal: AgentActionProposal) -> None:
    from app.core.config import get_settings
    if not get_settings().workflow_engine_on or proposal.department_id is None:
        return
    try:
        async with db.begin_nested():
            await mirror_proposal(db, None, proposal)
        await db.commit()
    except Exception as exc:
        log.info('Agent action %s not mirrored yet (%s)', proposal.id, type(exc).__name__)


_UNMIRRORABLE: set[uuid.UUID] = set()


async def mirror_pending_proposals(sessionmaker, cipher, *, limit: int = 25) -> int:
    count = 0
    async with sessionmaker() as db:
        query = (select(AgentActionProposal).outerjoin(WorkflowRun, and_(
            WorkflowRun.origin == 'governance', WorkflowRun.origin_ref == AgentActionProposal.id.cast(String),
        )).where(WorkflowRun.id.is_(None), AgentActionProposal.department_id.is_not(None),
                 AgentActionProposal.status.in_([ACTION_PENDING_APPROVAL, ACTION_APPROVED])))
        if _UNMIRRORABLE:
            query = query.where(AgentActionProposal.id.not_in(list(_UNMIRRORABLE)[-5000:]))
        for proposal in (await db.scalars(query.order_by(AgentActionProposal.requested_at).limit(limit))).all():
            try:
                async with db.begin_nested():
                    if await mirror_proposal(db, cipher, proposal) is not None:
                        count += 1
                    else:
                        _UNMIRRORABLE.add(proposal.id)
            except AppError as exc:
                _UNMIRRORABLE.add(proposal.id)
                log.info('Agent action %s not mirrored (%s)', proposal.id, exc.code)
        await db.commit()
    return count


async def guard(db, proposal: AgentActionProposal) -> WorkflowRun | None:
    """The unified run carrying this proposal, after refusing execution while it is paused or stopped."""
    from app.workflows.unified import origin_run
    run = await origin_run(db, 'governance', proposal.id)
    if run is None:
        return None
    if run.status == 'paused':
        raise ConflictError('This action is paused in the workflow runtime. Resume the run first.')
    if run.status in {'cancelled', 'failed'}:
        raise ConflictError('This action was stopped in the workflow runtime and cannot be executed.')
    return run


def _spec(proposal: AgentActionProposal, platform_scope) -> ActionSpec:
    return ActionSpec(f'agent_action.{proposal.action_type}', platform_scope,
                      {'proposal_id': str(proposal.id), 'action_type': proposal.action_type,
                       'parameters_digest': digest(proposal.parameters or {})},
                      subject=f'agent_action:{proposal.id}')


async def _key(db, proposal_id) -> str:
    """One execution per proposal; after a refused/failed attempt a new attempt gets a new key."""
    base = f'agent_action:{proposal_id}'
    failed = await db.scalar(select(func.count()).select_from(PlatformActionExecution).where(
        PlatformActionExecution.idempotency_key.like(base + '%'), PlatformActionExecution.status == 'failed'))
    return base if not failed else f'{base}:retry{failed}'


async def execute_proposal(db, actor: User, ctx: AuditContext, proposal: AgentActionProposal,
                           run: WorkflowRun) -> AgentActionProposal:
    from app.agent_controls.gate import gate_agent_liveness
    from app.services.agent_actions import apply_execution
    from app.workflows.action_runtime import platform_scope_for
    # Step AK: a governance proposal from a controlled-stopped profile agent
    # is refused before execution, even though it was approved earlier — the
    # kill-switch outranks a stale approval.
    if proposal.agent_profile_id is not None:
        decision = await gate_agent_liveness(db, proposal.agent_profile_id)
        if decision.outcome == "deny":
            raise PermissionDeniedError(
                "The agent that proposed this action is stopped by its controls.")
    platform_scope = await platform_scope_for(db, run.scope_id)
    if platform_scope is None:
        raise ConflictError('The department has no platform scope.')
    proposal_id = proposal.id
    captured: dict = {}

    async def dispatch() -> str:
        row = await db.get(AgentActionProposal, proposal_id, populate_existing=True)
        if row.status == ACTION_EXECUTED:
            return str(proposal_id)
        try:
            async with db.begin_nested():  # a refused adapter leaves nothing behind
                await apply_execution(db, actor, ctx, row)
        except Exception as exc:
            captured['exc'] = exc
            raise
        return str(proposal_id)

    async def verify(ref: str) -> VerifiedResult:
        row = await db.scalar(select(AgentActionProposal).where(AgentActionProposal.id == proposal_id)
                              .execution_options(populate_existing=True))
        ok = row is not None and row.status == ACTION_EXECUTED and row.execution_result is not None
        return VerifiedResult(ok, {'proposal_id': ref, 'status': row.status if row else None})

    result = await execute_once(db, principal_for(actor, channel='workflow'), await _key(db, proposal_id),
                               _spec(proposal, platform_scope), duty=DUTY, dispatch=dispatch, verify=verify)
    if result.status == 'failed':
        if isinstance(captured.get('exc'), AppError):
            raise captured['exc']
        raise ConflictError('The action could not be executed. Nothing was changed; see the audit log.')
    if result.status == 'claimed':
        raise ConflictError('This action is being executed right now.')
    return await db.get(AgentActionProposal, proposal_id, populate_existing=True)


async def attach_executor(db, run: WorkflowRun, actor: User) -> None:
    """After an execution through the legacy API, the workflow's execute step is taken by the same person so the
    engine records (and re-verifies) the result instead of waiting for someone to perform it."""
    from app.workflows.events import event
    step = await db.scalar(select(WorkflowStep).where(WorkflowStep.run_id == run.id, WorkflowStep.node_id == 'execute')
                           .with_for_update())
    if step is None or step.status in {'completed', 'cancelled', 'skipped', 'failed'} or step.acting_user_id:
        return
    step.acting_user_id, step.acting_auth_version = actor.id, actor.auth_version
    if step.status in {'action', 'waiting'}:
        step.status, step.available_at = 'ready', datetime.now(UTC)
    run.wake_at = datetime.now(UTC)
    event(db, run, 'action.actor_assigned', node_id='execute', actor_id=actor.id, detail={'via': 'agent_actions_api'})


async def _proposal(ctx: ActionContext) -> AgentActionProposal | None:
    try:
        proposal_id = uuid.UUID(str(ctx.values.get('proposal_id')))
    except ValueError:
        return None
    row = await ctx.db.scalar(select(AgentActionProposal).where(AgentActionProposal.id == proposal_id)
                              .execution_options(populate_existing=True))
    return row if row is not None and row.department_id == ctx.group_id else None  # department binding


class ProposalApprovalHandler:
    name = 'governance.proposal_approval'
    label = 'Independent approval of an agent-proposed action'
    module = '06'
    permission = None

    async def run(self, ctx: ActionContext) -> ActionOutcome:
        row = await _proposal(ctx)
        if row is None:
            return ActionOutcome('failed', failure_code='proposal_missing')
        out = {'proposal_id': str(row.id), 'status': row.status}
        if row.status in {ACTION_APPROVED, ACTION_EXECUTED}:
            return ActionOutcome('done', out | {'approver_id': str(row.approver_user_id)})
        if row.status == ACTION_REJECTED:
            return ActionOutcome('failed', out, 'approval_rejected')
        if row.status == ACTION_EXPIRED or row.expires_at <= datetime.now(UTC):
            return ActionOutcome('failed', out, 'approval_expired')
        return ActionOutcome('waiting', out, poll_seconds=30)

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult:
        row = await _proposal(ctx)
        ok = (row is not None and row.status in {ACTION_APPROVED, ACTION_EXECUTED} and row.approver_user_id is not None
              and row.approver_user_id != row.requester_user_id)
        return VerifiedResult(ok, {'proposal_id': output.get('proposal_id'), 'status': row.status if row else None})


class ProposalExecuteHandler:
    name = 'governance.proposal_execute'
    label = 'Execute an approved agent action (execute_once, read-back)'
    module = '06'
    permission = DUTY

    async def run(self, ctx: ActionContext) -> ActionOutcome:
        from app.services.agent_actions import _can_manage_proposal
        row = await _proposal(ctx)
        if row is None:
            return ActionOutcome('failed', failure_code='proposal_missing')
        out = {'proposal_id': str(row.id)}
        actor = await ctx.db.get(User, ctx.principal.user_id)
        if row.status == ACTION_EXECUTED:
            # Executed through the API: confirm through the same idempotency key (a replay re-verifies).
            pass
        elif row.status != ACTION_APPROVED:
            return ActionOutcome('failed', out | {'status': row.status}, 'proposal_not_approved')
        elif row.expires_at < datetime.now(UTC):
            return ActionOutcome('failed', out, 'approval_expired')
        if actor is None or not await _can_manage_proposal(ctx.db, actor, row):
            return ActionOutcome('failed', out, 'actor_not_permitted')
        run = await ctx.db.get(WorkflowRun, ctx.run_id)
        try:
            await execute_proposal(ctx.db, actor, AuditContext(actor.id, actor.email, None, None), row, run)
        except PermissionDeniedError:
            await ctx.db.rollback()
            return ActionOutcome('failed', out, 'actor_not_permitted')  # hand the step to someone allowed
        except AppError as exc:
            await ctx.db.rollback()
            return ActionOutcome('failed', out, exc.code[:60])
        return ActionOutcome('done', out | {'status': ACTION_EXECUTED})

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult:
        row = await _proposal(ctx)
        execution = await ctx.db.scalar(select(PlatformActionExecution).where(
            PlatformActionExecution.subject == f'agent_action:{output.get("proposal_id")}',
            PlatformActionExecution.status == 'verified'))
        ok = row is not None and row.status == ACTION_EXECUTED and execution is not None
        return VerifiedResult(ok, {'proposal_id': output.get('proposal_id'),
                                   'execution_id': str(execution.id) if execution else None})


register(ProposalApprovalHandler())
register(ProposalExecuteHandler())
