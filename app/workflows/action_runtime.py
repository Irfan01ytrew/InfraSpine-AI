"""Execution of `action` steps: permissioned, checkpointed, verified, recoverable (step E).

Lifecycle of one action step (each arrow is a committed state):

    blocked -> ready -> [no actor yet] action (waiting for someone who holds the permission)
    ready -> running (claimed, lease) -> handler.run() slice
          -> more     -> ready     (checkpoint saved; next slice continues where this one stopped)
          -> waiting  -> waiting   (polled again after `poll_seconds`, e.g. approvals pending)
          -> done     -> verify    -> completed (verified_at)   | recover -> ready after backoff | failed
          -> failed   -> recover (retryable, backoff) | failed (run fails; `resume` restarts from here)

The handler runs outside the engine's savepoint because it commits per item (and `execute_once` /
`request_approval` commit by design). The claim token on the step fences every write after the slice.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.errors import AppError
from app.db.models import User
from app.db.workflow_engine_models import WorkflowRun, WorkflowStep
from app.platform.access import authorize, principal_for
from app.platform.events import publish
from app.platform.sync import group_scope_sync
from app.workflows.actions import ActionContext, ActionOutcome, get_handler
from app.workflows.definition import resolve
from app.workflows.events import event, notify
from app.workflows.security import seal, unseal

log = logging.getLogger(__name__)

MAX_RECOVERIES = 5
PERSON_BLOCKED = frozenset({'separation_of_duties', 'actor_not_permitted'})


async def platform_scope_for(db, group_id):
    return await db.run_sync(lambda s: group_scope_sync(s.connection(), group_id))


async def _acting_principal(db, run, step, node):
    """The principal for this step, or (None, reason). Auto steps act as the requester."""
    if node.config.auto:
        user = await db.get(User, run.requester_id, populate_existing=True)
        if user is None or not user.is_active:
            return None, 'requester_inactive'
        return principal_for(user, channel='workflow'), None
    if step.acting_user_id is None:
        return None, 'awaiting_actor'
    user = await db.get(User, step.acting_user_id, populate_existing=True)
    if user is None:
        return None, 'actor_missing'
    p = principal_for(user, channel='workflow')
    # The auth version captured when the person took the step: a revoke in between kills the principal.
    from dataclasses import replace
    return replace(p, auth_version=step.acting_auth_version or 0), None


async def _permitted(db, p, node, platform_scope):
    if not node.config.permission:
        return True, 'granted'
    decision = await authorize(db, p, node.config.permission, platform_scope)
    return decision.allowed, decision.reason


def _release_to_actor_queue(db, run, step, reason):
    step.status, step.claim_token, step.lease_until = 'action', None, None
    step.acting_user_id, step.acting_auth_version = None, None
    step.attempts = max(0, step.attempts - 1)
    run.blocked_reason = f'blocked: {reason}'[:120]
    event(db, run, 'action.actor_released', node_id=step.node_id, detail={'reason': reason})


async def execute_action_claim(sessionmaker, cipher, claim, node, *, run_context):
    """Run one slice of an action step owned by `claim`. Returns True when the claim was processed."""
    handler = get_handler(node.config.handler)
    async with sessionmaker() as db:
        run = await db.get(WorkflowRun, claim.run_id, with_for_update=True)
        step = await db.get(WorkflowStep, claim.step_id, with_for_update=True)
        now = datetime.now(UTC)
        if run is None or run.status != 'running' or not claim.owns(step, now):
            return False
        platform_scope = await platform_scope_for(db, run.scope_id)
        if handler is None or platform_scope is None:
            _fail(db, run, step, 'handler_unavailable' if handler is None else 'scope_unavailable')
            await db.commit()
            return True
        p, problem = await _acting_principal(db, run, step, node)
        if p is not None:
            allowed, reason = await _permitted(db, p, node, platform_scope)
            if not allowed:
                problem = 'actor_revoked' if reason in {'principal_revoked', 'user_inactive', 'session_revoked',
                                                        'not_a_member', 'permission_missing'} else reason
        if problem:
            if node.config.auto:
                _fail(db, run, step, problem)
            else:
                if problem == 'awaiting_actor':
                    step.status, step.claim_token, step.lease_until = 'action', None, None
                    step.attempts = max(0, step.attempts - 1)
                    event(db, run, 'action.awaiting_actor', node_id=step.node_id,
                          detail={'permission': node.config.permission})
                    if step.assignee_id:
                        await notify(db, step, step.assignee_id, 'task_assigned')
                else:
                    _release_to_actor_queue(db, run, step, problem)
                    await publish(db, 'workflow.blocked', platform_scope,
                                  {'run_id': str(run.id), 'node_id': step.node_id, 'reason': problem}, p.trace_id if p else run.id)
            await db.commit()
            return True
        context, _ = await run_context(db, cipher, run)
        values = resolve(node.config.values, context)
        checkpoint = dict(unseal(cipher, run.scope_id, 'checkpoint', step.id, step.checkpoint) if step.checkpoint else {})
        step.phase = 'execute'
        run.blocked_reason = None
        if run.phase in {'plan', 'recover'}:
            run.phase = 'execute'
        event(db, run, 'action.slice_started', node_id=step.node_id,
              detail={'handler': handler.name, 'actor_id': str(p.user_id), 'attempt': step.attempts})
        await db.commit()
        run_id, step_id, scope_id = run.id, step.id, run.scope_id

        async def should_stop():
            # Fresh reads: pause/cancel of the run, loss of the claim, or loss of the permission.
            current = await db.scalar(select(WorkflowRun.status).where(WorkflowRun.id == run_id)
                                      .execution_options(populate_existing=True))
            if current != 'running':
                return f'run_{current}'
            token = await db.scalar(select(WorkflowStep.claim_token).where(WorkflowStep.id == step_id))
            if token != claim.token:
                return 'claim_lost'
            ok, why = await _permitted(db, p, node, platform_scope)
            if not ok:
                return 'actor_revoked'
            return None

        ctx = ActionContext(db=db, cipher=cipher, principal=p, run_id=run_id, step_id=step_id, group_id=scope_id,
                            platform_scope_id=platform_scope, values=values, inputs=context['input'],
                            checkpoint=checkpoint, should_stop=should_stop)
        try:
            outcome = await handler.run(ctx)
        except AppError as exc:
            await db.rollback()
            outcome = ActionOutcome('failed', failure_code=exc.code, retryable=False)
        except Exception as exc:  # handlers are idempotent per item; an unexpected error is retried with backoff
            await db.rollback()
            log.warning('Action handler %s failed (%s); no payload logged', handler.name, type(exc).__name__)
            outcome = ActionOutcome('failed', failure_code='handler_exception', retryable=True)
        stop_reason = None
        if outcome.status == 'more':
            stop_reason = await should_stop()
        verified = None
        if outcome.status == 'done':
            run = await db.get(WorkflowRun, run_id, with_for_update=True, populate_existing=True)
            step = await db.get(WorkflowStep, step_id, with_for_update=True, populate_existing=True)
            if step.claim_token != claim.token:
                await db.rollback()
                return True
            step.phase = 'verify'
            run.phase = 'verify'
            event(db, run, 'action.verifying', node_id=step.node_id)
            await db.commit()
            try:
                verified = await handler.verify(ctx, outcome.output)
            except Exception as exc:
                await db.rollback()
                log.warning('Action verification %s unavailable (%s)', handler.name, type(exc).__name__)
                verified = None
                outcome = ActionOutcome('failed', outcome.output, 'verification_unavailable', retryable=True)
            else:
                if not verified.ok:
                    outcome = ActionOutcome('failed', outcome.output, 'verification_failed', retryable=True)
        run = await db.get(WorkflowRun, run_id, with_for_update=True, populate_existing=True)
        step = await db.get(WorkflowStep, step_id, with_for_update=True, populate_existing=True)
        if step.claim_token != claim.token:
            await db.rollback()
            return True
        for kind, detail in ctx.events[:200]:
            event(db, run, kind, node_id=step.node_id, actor_id=p.user_id, detail=detail)
        step.checkpoint = seal(cipher, scope_id, 'checkpoint', step.id, ctx.checkpoint)
        now = datetime.now(UTC)
        if outcome.status == 'more':
            step.status, step.claim_token, step.lease_until = 'ready', None, None
            step.attempts = max(0, step.attempts - 1)  # progress is not a failed attempt
            step.available_at = now
            run.wake_at = now
            if stop_reason == 'actor_revoked' and not node.config.auto:
                _release_to_actor_queue(db, run, step, 'actor_revoked')
                await publish(db, 'workflow.blocked', platform_scope,
                              {'run_id': str(run.id), 'node_id': step.node_id, 'reason': 'actor_revoked'}, p.trace_id)
            event(db, run, 'action.progress', node_id=step.node_id, detail={'checkpoint': _public(ctx.checkpoint),
                                                                            'stopped': stop_reason})
        elif outcome.status == 'waiting':
            step.status, step.claim_token, step.lease_until = 'waiting', None, None
            step.attempts = max(0, step.attempts - 1)
            step.available_at = now + timedelta(seconds=max(1, min(outcome.poll_seconds, 3600)))
            run.wake_at = step.available_at
            event(db, run, 'action.waiting', node_id=step.node_id, detail=_public(outcome.output))
        elif outcome.status == 'done' and verified is not None and verified.ok:
            step.status, step.finished_at = 'completed', now
            step.claim_token, step.lease_until, step.failure_code = None, None, None
            step.phase, step.verified_at = 'done', now
            step.verification = verified.details
            step.output = seal(cipher, scope_id, 'step', step.id, outcome.output)
            run.last_verified_node, run.wake_at = step.node_id, now
            run.phase = 'execute'
            event(db, run, 'step.verified', node_id=step.node_id, actor_id=p.user_id,
                  detail={'verification': verified.details})
            await publish(db, 'workflow.step_verified', platform_scope,
                          {'run_id': str(run.id), 'node_id': step.node_id, 'handler': handler.name}, p.trace_id)
        else:
            code = outcome.failure_code or 'action_failed'
            if code in PERSON_BLOCKED and not node.config.auto:
                # This person may not do this step (separation of duties, lost permission): hand the step to
                # someone else instead of failing the run. Nothing was written for the refused item.
                _release_to_actor_queue(db, run, step, code)
                await publish(db, 'workflow.blocked', platform_scope,
                              {'run_id': str(run.id), 'node_id': step.node_id, 'reason': code}, p.trace_id)
            elif outcome.retryable and step.recoveries < MAX_RECOVERIES:
                step.recoveries += 1
                step.status, step.claim_token, step.lease_until, step.failure_code = 'ready', None, None, code
                step.phase, run.phase = 'recover', 'recover'
                step.available_at = now + timedelta(seconds=min(300, 2 ** step.recoveries))
                run.wake_at = step.available_at
                event(db, run, 'step.recovering', node_id=step.node_id,
                      detail={'code': code, 'recovery': step.recoveries})
            else:
                _fail(db, run, step, code)
                await publish(db, 'workflow.blocked', platform_scope,
                              {'run_id': str(run.id), 'node_id': step.node_id, 'reason': code}, p.trace_id)
        await db.commit()
        return True


def _public(data):
    """Only small scalar progress values are copied into the (unencrypted) audit detail."""
    out = {}
    for key, value in list((data or {}).items())[:20]:
        if isinstance(value, (int, bool)) or (isinstance(value, str) and len(value) <= 80):
            out[key] = value
    return out


def _fail(db, run, step, code):
    step.status, step.failure_code, step.claim_token, step.lease_until = 'failed', code, None, None
    run.status, run.failure_code, run.finished_at = 'failed', code, datetime.now(UTC)
    run.phase = 'recover'
    event(db, run, 'step.failed', node_id=step.node_id, detail={'code': code})
