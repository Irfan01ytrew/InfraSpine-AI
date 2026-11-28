"""Agent Studio runs on the unified workflow runtime (step E).

Every Agent Studio run (API, schedule, WhatsApp, Baileys, inline) gets one workflow run (`origin='studio'`,
`origin_ref=<agent studio run id>`) in the owner's department. The engine executes it through the
`agent_studio.execute` action handler, so Agent Studio work is listed, audited, paused, stopped and resumed in the
same place as every other process:

* each execution attempt goes through `execute_once` (key `agent_studio:<run>:attempt:<n>`, duty `agent.run`, read
  back from the Agent Studio run), so a crash or a second engine never runs the same attempt twice;
* the agent's own pause switch (step A) is honoured before every attempt; a paused *workflow run* is never executed
  because the legacy Agent Studio loop leaves unified runs to the engine (`exclude_unified`);
* Agent Studio's exact-action tool approvals (`/api/agent-studio/runs/{id}/decision`) still decide; the workflow step
  waits while the run is `awaiting_approval` and continues after the decision;
* stopping the workflow run cancels a queued or waiting Agent Studio run.

The existing Agent Studio APIs and tables are unchanged (adapter, not a rewrite). The LLM runtime and the knowledge
index live in the API process, so that process registers itself as the execution host (`configure_host`); an engine
in another process leaves the step waiting for it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import String, and_, select

from app.core.errors import AppError
from app.db.agent_studio_models import AgentStudioAgent, AgentStudioRun
from app.db.models import AccessGroup, AccessGroupMember, User
from app.db.workflow_engine_models import WorkflowRun
from app.platform.approvals import ActionSpec
from app.platform.execution import VerifiedResult, execute_once
from app.platform.models import PlatformActionExecution
from app.services.audit import AuditContext
from app.workflows.actions import ActionContext, ActionOutcome, register
from app.workflows.definition import Definition

log = logging.getLogger(__name__)

TEMPLATE_REF = 'agent_studio.runs'
TEMPLATE_NAME = 'Agent Studio runs'
TEMPLATE_DESCRIPTION = ('Agent Studio tasks on the unified runtime: pause, stop and resume here; tool approvals are '
                        'decided in Agent Studio.')
MIRROR_STATUSES = ('queued', 'running', 'awaiting_approval')


@dataclass
class StudioHost:
    sessionmaker: Any
    settings: Any
    cipher: Any
    runtime: Any
    rag: Any


HOST: StudioHost | None = None


def configure_host(host: StudioHost | None) -> None:
    """Called by the API process that owns the LLM runtime and the knowledge index (and by tests)."""
    global HOST
    HOST = host


def studio_spec(timeout_seconds: int) -> dict:
    lease = max(120, min(3600, int(timeout_seconds) + 120))
    return Definition.model_validate({
        'source': 'template',
        'input_form': [{'name': 'studio_run_id', 'label': 'Agent Studio run', 'type': 'text', 'required': True},
                       {'name': 'agent_id', 'label': 'Agent', 'type': 'text', 'required': True}],
        'nodes': [
            {'id': 'start', 'type': 'start', 'label': 'Task received', 'x': 80, 'y': 160},
            {'id': 'agent', 'type': 'action', 'label': 'Agent Studio run (verified)', 'x': 340, 'y': 160,
             'risk': 'medium', 'timeout_seconds': lease, 'max_attempts': 5,
             'config': {'handler': 'agent_studio.execute', 'auto': True,
                        'values': {'studio_run_id': '$input.studio_run_id'}}},
            {'id': 'end', 'type': 'end', 'label': 'Answer verified', 'x': 600, 'y': 160},
        ],
        'edges': [{'source': 'start', 'target': 'agent'}, {'source': 'agent', 'target': 'end'}],
    }).model_dump(mode='json')


async def scope_for_owner(db, owner: User):
    """The owner's department for their Agent Studio runs: the first (by name) department they may use workflows in."""
    from app.workflows.security import require_scope
    groups = (await db.scalars(select(AccessGroup).join(AccessGroupMember, AccessGroupMember.group_id == AccessGroup.id)
                               .where(AccessGroupMember.user_id == owner.id, AccessGroup.kind == 'department')
                               .order_by(AccessGroup.name, AccessGroup.id))).all()
    for group in groups:
        try:
            async with db.begin_nested():
                await require_scope(db, owner, group.id)
            return group.id
        except AppError:
            continue
    return None


async def mirror_studio_run(db, cipher, run: AgentStudioRun, settings=None) -> WorkflowRun | None:
    from app.workflows.service import WorkflowService
    from app.workflows.unified import ensure_system_definition, origin_run
    existing = await origin_run(db, 'studio', run.id)
    if existing is not None:
        return existing
    owner = await db.get(User, run.owner_id)
    if owner is None or not owner.is_active:
        return None
    scope_id = await scope_for_owner(db, owner)
    if scope_id is None:
        return None
    if settings is None:
        from app.core.config import get_settings
        settings = get_settings()
    definition = await ensure_system_definition(
        db, scope_id=scope_id, owner_id=owner.id, origin='studio', origin_ref=TEMPLATE_REF, name=TEMPLATE_NAME,
        description=TEMPLATE_DESCRIPTION, spec=studio_spec(settings.agent_studio_run_timeout_seconds))
    if not definition.enabled:
        return None  # a department manager turned the unified path off: the legacy loop runs these tasks
    service = WorkflowService(db, owner, cipher, AuditContext(owner.id, owner.email, None, None))
    return await service.trigger(scope_id, definition.id, {'studio_run_id': str(run.id), 'agent_id': str(run.agent_id)},
                                 f'agent-studio:{run.id}', origin='studio', origin_ref=str(run.id))


async def mirror_new_run(db, cipher, run: AgentStudioRun, settings=None) -> None:
    """Best effort right after Agent Studio created a run; the engine's sweep catches anything missed."""
    try:
        async with db.begin_nested():
            await mirror_studio_run(db, cipher, run, settings)
        await db.commit()
    except Exception as exc:  # never break Agent Studio because the runtime could not record the run
        log.info('Agent Studio run %s not mirrored yet (%s)', run.id, type(exc).__name__)


_UNMIRRORABLE: set[uuid.UUID] = set()


async def mirror_pending_studio_runs(sessionmaker, cipher, *, limit: int = 25) -> int:
    count = 0
    async with sessionmaker() as db:
        query = (select(AgentStudioRun).outerjoin(WorkflowRun, and_(
            WorkflowRun.origin == 'studio', WorkflowRun.origin_ref == AgentStudioRun.id.cast(String),
        )).where(WorkflowRun.id.is_(None), AgentStudioRun.status.in_(MIRROR_STATUSES)))
        if _UNMIRRORABLE:
            query = query.where(AgentStudioRun.id.not_in(list(_UNMIRRORABLE)[-5000:]))
        for run in (await db.scalars(query.order_by(AgentStudioRun.created_at).limit(limit))).all():
            try:
                async with db.begin_nested():
                    if await mirror_studio_run(db, cipher, run) is not None:
                        count += 1
                    else:
                        _UNMIRRORABLE.add(run.id)
            except AppError as exc:
                _UNMIRRORABLE.add(run.id)
                log.info('Agent Studio run %s not mirrored (%s)', run.id, exc.code)
        await db.commit()
    return count


async def on_workflow_cancelled(db, cipher, run: WorkflowRun) -> None:
    """Stop at the next step boundary: a queued or approval-waiting Agent Studio run is cancelled. A run that is
    executing right now finishes its current attempt (it is never killed mid-action) and is not started again."""
    from app.agent_studio.service import _encode_state, decode_run_state
    try:
        studio_id = uuid.UUID(str(run.origin_ref))
    except ValueError:
        return
    row = await db.scalar(select(AgentStudioRun).where(AgentStudioRun.id == studio_id).with_for_update())
    if row is None or row.status not in {'queued', 'awaiting_approval'}:
        return
    if cipher is None:
        from app.core.config import get_settings
        from app.core.crypto import rotating_cipher_from_settings
        settings = get_settings()
        cipher = rotating_cipher_from_settings(settings)
    state = decode_run_state(cipher, row)
    state['answer'] = 'This task was stopped in the workflow runtime before it ran further.'
    state['awaiting_approval'] = False
    sealed = _encode_state(cipher, row.id, state)
    row.state_ciphertext, row.state_nonce, row.state_key_id = sealed.ciphertext, sealed.nonce, sealed.key_id
    row.status, row.failure_code, row.finished_at, row.claimed_at = 'cancelled', 'workflow_stopped', datetime.now(UTC), None


def _attempt_spec(ctx: ActionContext, run: AgentStudioRun, attempt: int) -> ActionSpec:
    return ActionSpec('agent_studio.run', ctx.platform_scope_id,
                      {'studio_run_id': str(run.id), 'agent_id': str(run.agent_id), 'attempt': attempt,
                       'workflow_run_id': str(ctx.run_id)},
                      subject=f'agent_studio_run:{run.id}')


class AgentStudioExecuteHandler:
    name = 'agent_studio.execute'
    label = 'Agent Studio run (attempts through execute_once, verified by read-back)'
    module = '02'
    permission = None  # the run's owner is the requester; execute_once checks `agent.run` on the department

    async def _load(self, ctx: ActionContext) -> AgentStudioRun | None:
        try:
            studio_id = uuid.UUID(str(ctx.values.get('studio_run_id')))
        except ValueError:
            return None
        row = await ctx.db.scalar(select(AgentStudioRun).where(AgentStudioRun.id == studio_id)
                                  .execution_options(populate_existing=True))
        # Owner binding: a workflow can only carry the requester's own Agent Studio run.
        return row if row is not None and row.owner_id == ctx.principal.user_id else None

    async def run(self, ctx: ActionContext) -> ActionOutcome:
        row = await self._load(ctx)
        if row is None:
            return ActionOutcome('failed', failure_code='agent_run_missing')
        out = {'studio_run_id': str(row.id), 'status': row.status}
        if row.status == 'completed':
            return ActionOutcome('done', out)
        if row.status == 'cancelled':
            return ActionOutcome('failed', out, 'agent_cancelled')
        if row.status == 'failed':
            return ActionOutcome('failed', out, 'agent_failed')
        if row.status == 'awaiting_approval':
            ctx.note('agent.awaiting_approval', studio_run_id=str(row.id))
            return ActionOutcome('waiting', out, poll_seconds=15)
        if row.status == 'running':
            return ActionOutcome('waiting', out, poll_seconds=3)
        now = datetime.now(UTC)
        if row.next_attempt_at > now:
            return ActionOutcome('waiting', out, poll_seconds=max(1, int((row.next_attempt_at - now).total_seconds()) + 1))
        agent = await ctx.db.get(AgentStudioAgent, row.agent_id, populate_existing=True)
        if agent is None or not agent.enabled:
            return ActionOutcome('waiting', out | {'reason': 'agent_disabled'}, poll_seconds=30)
        if agent.paused:  # step A: a paused agent runs nothing until it is resumed
            ctx.note('agent.paused', agent_id=str(agent.id))
            return ActionOutcome('waiting', out | {'reason': 'agent_paused'}, poll_seconds=15)
        host = HOST
        if host is None:
            return ActionOutcome('waiting', out | {'reason': 'no_execution_host'}, poll_seconds=3)
        attempt = row.attempt_count + 1
        retry = int(ctx.checkpoint.get(f'retry_{attempt}', 0))
        key = f'agent_studio:{row.id}:attempt:{attempt}' + (f':r{retry}' if retry else '')
        spec = _attempt_spec(ctx, row, attempt)
        studio_id = row.id
        await ctx.db.rollback()  # no read transaction stays open while the model works

        async def dispatch() -> str:
            claim = await claim_for(host, studio_id)
            if claim is None:
                return f'{studio_id}:not_claimed'
            from app.agent_studio.worker import execute_agent_studio_claim
            await execute_agent_studio_claim(host.sessionmaker, host.settings, host.cipher, host.runtime, host.rag, claim)
            return f'{studio_id}:{claim[2]}'

        async def verify(ref: str) -> VerifiedResult:
            status = await ctx.db.scalar(select(AgentStudioRun.status).where(AgentStudioRun.id == studio_id)
                                         .execution_options(populate_existing=True))
            return VerifiedResult(status is not None and status != 'running', {'studio_run_id': str(studio_id),
                                                                               'status': status, 'ref': ref})

        result = await execute_once(ctx.db, ctx.principal, key, spec, duty='agent.run', dispatch=dispatch, verify=verify)
        if result.status == 'claimed' and result.replayed:
            started = await ctx.db.scalar(select(PlatformActionExecution.created_at)
                                          .where(PlatformActionExecution.id == result.execution_id))
            limit = (host.settings.agent_studio_run_timeout_seconds or 300) + 120
            if started is not None and datetime.now(UTC) - started > timedelta(seconds=limit):
                ctx.checkpoint[f'retry_{attempt}'] = retry + 1  # abandoned before the Agent Studio claim
            return ActionOutcome('waiting', out, poll_seconds=5)
        ctx.note('agent.attempt', studio_run_id=str(studio_id), attempt=attempt, execution=result.status)
        return ActionOutcome('more', out)

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult:
        row = await self._load(ctx)
        ok = row is not None and row.status == 'completed' and row.finished_at is not None
        return VerifiedResult(ok, {'studio_run_id': output.get('studio_run_id'), 'status': row.status if row else None})


async def claim_for(host: StudioHost, studio_id: uuid.UUID):
    from app.agent_studio.worker import claim_agent_studio_run
    return await claim_agent_studio_run(host.sessionmaker, run_id=studio_id)


register(AgentStudioExecuteHandler())
