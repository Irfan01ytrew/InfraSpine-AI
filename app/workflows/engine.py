"""PostgreSQL-backed graph scheduler and fenced worker. No network effects here."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select

from app.core.errors import AppError, ConflictError, InvalidInputError
from app.db.models import DepartmentManager, User
from app.db.workflow_engine_models import (Workflow, WorkflowVersion, WorkflowRun, WorkflowStep,
    WorkflowDispatch, WorkflowSignal)
from app.workflows.adapters import ADAPTERS
from app.workflows.definition import Definition, TERMINAL, evaluate, lookup, resolve
from app.workflows.events import event, notify
from app.workflows.security import require_member, require_scope, seal, unseal
from app.workflows.service import (WorkflowService, action_data, action_hash, approved_authorities,
    required_approvals, run_context)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Claim:
    run_id: UUID
    step_id: UUID
    token: UUID
    attempt: int
    expires_at: datetime

    def owns(self, step, now):
        return (step is not None and step.status == 'running' and step.claim_token == self.token
                and step.attempts == self.attempt and step.lease_until == self.expires_at and now < self.expires_at)


async def managers(db, scope_id):
    return list((await db.scalars(select(User.id).where(User.is_active.is_(True), or_(
        User.role == 'admin', User.id.in_(select(DepartmentManager.manager_user_id).where(
            DepartmentManager.group_id == scope_id)))))).all())


def finish(db, cipher, run, step, output, *, simulated=False):
    step.status, step.finished_at = 'completed', datetime.now(UTC)
    step.claim_token, step.lease_until, step.failure_code = None, None, None
    step.phase = 'done'
    step.output = seal(cipher, run.scope_id, 'step', step.id, output)
    run.wake_at = datetime.now(UTC)
    event(db, run, 'step.simulated' if simulated else 'step.completed', node_id=step.node_id)


def fail(db, run, step, code):
    step.status, step.failure_code, step.claim_token, step.lease_until = 'failed', code, None, None
    run.status, run.failure_code, run.finished_at = 'failed', code, datetime.now(UTC)
    run.phase = 'recover'  # a failed run waits for `resume` (from the last verified step) or compensation
    event(db, run, 'step.failed', node_id=step.node_id, detail={'code': code})


async def schedule_due(sessionmaker, cipher, *, now=None, limit=25):
    now = now or datetime.now(UTC)
    if now.utcoffset() is None: raise ValueError('Scheduler requires a timezone-aware clock.')
    count = 0
    async with sessionmaker() as db:
        workflows = (await db.scalars(select(Workflow).where(Workflow.enabled.is_(True),
            Workflow.next_run_at <= now).order_by(Workflow.next_run_at).limit(limit).with_for_update(skip_locked=True))).all()
        for workflow in workflows:
            version = await db.scalar(select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow.id, WorkflowVersion.number == workflow.active_version))
            if version is None: continue
            spec = Definition.model_validate(version.spec)
            owner = await db.get(User, workflow.owner_id)
            due = workflow.next_run_at
            if owner:
                try:
                    async with db.begin_nested():
                        await WorkflowService(db, owner, cipher).trigger(workflow.scope_id, workflow.id, {},
                            f'schedule:{version.id}:{due.isoformat()}', source=spec.trigger.type)
                        count += 1
                except AppError as exc:
                    # Avoid an endlessly hot invalid schedule. Owner must explicitly republish after repair.
                    workflow.enabled = False
                    from app.services.audit import AuditContext, record
                    record(db, AuditContext(None, None, None, None), 'workflow.schedule_blocked',
                           target=str(workflow.id), details={'code': exc.code})
            if spec.trigger.type == 'interval':
                interval = timedelta(seconds=spec.trigger.interval_seconds)
                # Coalesce missed intervals into one durable run, then advance from the scheduled anchor.
                workflow.next_run_at = due + interval * (int((now - due) / interval) + 1)
            else:
                workflow.next_run_at = None
        await db.commit()
    return count


async def _advance(db, cipher, run, spec, *, now):
    context, steps = await run_context(db, cipher, run)
    nodes, by_node = {n.id: n for n in spec.nodes}, {s.node_id: s for s in steps}
    if run.deadline_at and run.deadline_at <= now and not run.simulation:
        # Whole-run timeout: stop at a step boundary; completed (verified) steps stay as they are.
        active = next((s for s in steps if s.status not in TERMINAL and s.status != 'blocked'), None) or steps[0]
        fail(db, run, active, 'run_timeout')
        return steps
    # Only immutable published definitions are executed; old versions remain pinned in-flight.
    for step in steps:
        node = nodes[step.node_id]
        if step.status == 'running' and step.lease_until and step.lease_until <= now:
            if step.attempts >= node.max_attempts:
                fail(db, run, step, 'lease_attempts_exhausted')
                return steps
            step.status, step.claim_token, step.lease_until = 'ready', None, None
            event(db, run, 'step.recovered', node_id=step.node_id, detail={'attempt': step.attempts})
        if step.status == 'waiting' and step.available_at <= now:
            step.status = 'ready'  # poll the action handler again (e.g. approvals decided)
        if step.status in {'human', 'approval', 'event', 'child', 'dispatched', 'action', 'waiting'} and step.deadline_at:
            if step.deadline_at <= now and step.escalated_at is None:
                step.escalated_at = now
                recipients = set(await managers(db, run.scope_id))
                if step.assignee_id: recipients.add(step.assignee_id)
                for recipient in recipients: await notify(db, step, recipient, 'sla_breached')
                event(db, run, 'sla.breached', node_id=step.node_id, detail={'recipients': [str(x) for x in sorted(recipients)]})
                if step.status == 'approval':
                    fail(db, run, step, 'approval_expired')
                    return steps
            elif step.started_at and now >= step.started_at + (step.deadline_at - step.started_at) / 2:
                recipients = [step.assignee_id] if step.assignee_id else await managers(db, run.scope_id)
                for recipient in recipients: await notify(db, step, recipient, 'reminder')
        if step.status == 'timer' and step.available_at <= now:
            finish(db, cipher, run, step, {'elapsed_seconds': node.config.delay_seconds})
        if step.status == 'event':
            signal = await db.scalar(select(WorkflowSignal).where(WorkflowSignal.scope_id == run.scope_id,
                WorkflowSignal.run_id == run.id, WorkflowSignal.name == node.config.event_name,
                WorkflowSignal.consumed_by.is_(None)).order_by(WorkflowSignal.created_at).limit(1))
            if signal:
                signal.consumed_by = step.id
                finish(db, cipher, run, step, unseal(cipher, run.scope_id, 'signal', signal.id, signal.payload))
        if step.status == 'child' and step.child_run_id:
            child = await db.scalar(select(WorkflowRun).where(WorkflowRun.id == step.child_run_id, WorkflowRun.scope_id == run.scope_id))
            if child and child.status in {'failed', 'cancelled'}:
                fail(db, run, step, 'subworkflow_failed')
                return steps
            if child and child.status == 'completed':
                step.iteration += 1
                if node.type == 'loop' and step.iteration < node.config.iterations:
                    step.status, step.child_run_id = 'ready', None
                else:
                    child_context, _ = await run_context(db, cipher, child)
                    finish(db, cipher, run, step, {'iterations': step.iteration, 'child_run_id': str(child.id), 'steps': child_context['steps']})
        if step.status == 'dispatched':
            dispatch = await db.scalar(select(WorkflowDispatch).where(WorkflowDispatch.step_id == step.id,
                WorkflowDispatch.compensation.is_(False), WorkflowDispatch.scope_id == run.scope_id))
            if dispatch and dispatch.status == 'completed':
                finish(db, cipher, run, step, unseal(cipher, run.scope_id, 'receipt', dispatch.id, dispatch.receipt))
                # Adapter receipts are verified completions (read back through execute_once).
                step.verified_at, step.verification = now, {'adapter': dispatch.adapter, 'dispatch_id': str(dispatch.id)}
                run.last_verified_node = step.node_id
            elif dispatch and dispatch.status in {'failed', 'unknown'}:
                fail(db, run, step, dispatch.failure_code or 'dispatch_failed')
                return steps
    # Propagate both selected paths and skipped branches; joins wait for every incoming edge to settle.
    for _ in range(len(steps)):
        changed = False
        for step in steps:
            if step.status != 'blocked': continue
            incoming = [e for e in spec.edges if e.target == step.node_id]
            if not incoming or any(by_node[e.source].status not in TERMINAL for e in incoming): continue
            active = []
            for edge in incoming:
                source = by_node[edge.source]
                selected = source.status == 'completed'
                if selected and edge.branch != 'always':
                    output = unseal(cipher, run.scope_id, 'step', source.id, source.output)
                    selected = output.get('branch') == edge.branch
                active.append(selected)
            step.status = 'ready' if any(active) else 'skipped'
            changed = True
            if step.status == 'skipped': event(db, run, 'step.skipped', node_id=step.node_id)
        if not changed: break
    if all(s.status in TERMINAL for s in steps):
        run.status, run.finished_at, run.phase = 'completed', now, 'done'
        event(db, run, 'run.completed')
    elif run.phase == 'plan':
        run.phase = 'execute'
    return steps


async def _compensate(db, cipher, run, spec):
    steps = list((await db.scalars(select(WorkflowStep).where(WorkflowStep.run_id == run.id,
        WorkflowStep.scope_id == run.scope_id).order_by(WorkflowStep.finished_at.desc().nullslast()))).all())
    nodes = {n.id: n for n in spec.nodes}
    for step in steps:
        if step.status != 'completed': continue
        node = nodes[step.node_id]
        if node.type not in {'agent', 'integration'}:
            step.status = 'compensated'
            event(db, run, 'step.compensated', node_id=step.node_id, detail={'kind': 'internal_state'})
            continue
        dispatch = await db.scalar(select(WorkflowDispatch).where(WorkflowDispatch.step_id == step.id,
            WorkflowDispatch.compensation.is_(True)))
        if dispatch is None:
            context, _ = await run_context(db, cipher, run)
            data = action_data(node, context)
            data['original_receipt'] = unseal(cipher, run.scope_id, 'step', step.id, step.output)
            dispatch_id = uuid4()
            dispatch = WorkflowDispatch(id=dispatch_id, scope_id=run.scope_id, step_id=step.id,
                adapter=node.config.compensation_adapter, compensation=True, action_digest=action_hash(run, step, node, context),
                payload=seal(cipher, run.scope_id, 'dispatch', dispatch_id, data), status='pending', attempts=0)
            db.add(dispatch)
            event(db, run, 'compensation.dispatched', node_id=step.node_id)
            return
        if dispatch.status == 'completed':
            step.status = 'compensated'
            event(db, run, 'step.compensated', node_id=step.node_id)
        elif dispatch.status in {'failed', 'unknown'}:
            run.status, run.failure_code = 'failed', 'compensation_failed'
            event(db, run, 'compensation.failed', node_id=step.node_id)
            return
        else:
            return  # reverse completion order; never compensate a dependency ahead of its consumer
    run.status, run.finished_at = 'compensated', datetime.now(UTC)
    event(db, run, 'run.compensated')


async def claim_next(sessionmaker, cipher, *, now=None):
    async with sessionmaker() as db:
        current = now or datetime.now(UTC)
        # Lock definitions before runs: concurrent workers enforce the same run-concurrency quota.
        workflow = await db.scalar(select(Workflow).where(Workflow.id.in_(select(WorkflowRun.workflow_id).where(
            WorkflowRun.status.in_(['queued', 'running', 'compensating']), WorkflowRun.wake_at <= current)))
            .order_by(Workflow.updated_at, Workflow.id).limit(1).with_for_update(skip_locked=True))
        if workflow is None: return None
        run = await db.scalar(select(WorkflowRun).where(WorkflowRun.workflow_id == workflow.id,
            WorkflowRun.status.in_(['queued', 'running', 'compensating']), WorkflowRun.wake_at <= current)
            .order_by(WorkflowRun.priority.desc(), WorkflowRun.wake_at, WorkflowRun.created_at).limit(1).with_for_update(skip_locked=True))
        if run is None: return None
        current = now or datetime.now(UTC)  # sample again after the lock wait
        version = await db.get(WorkflowVersion, run.version_id)
        spec = Definition.model_validate(version.spec)
        if run.status == 'queued':
            active = await db.scalar(select(func.count()).select_from(WorkflowRun).where(
                WorkflowRun.workflow_id == workflow.id, WorkflowRun.status.in_(['running', 'compensating'])))
            if active >= spec.max_concurrent_runs:
                run.wake_at = current + timedelta(seconds=2)
                await db.commit()
                return None
            run.status, run.started_at = 'running', run.started_at or current
            run.phase = 'execute'
            event(db, run, 'run.started', detail={'phase': 'execute'})
        run.wake_at = current + timedelta(seconds=2)
        if run.status == 'compensating':
            await _compensate(db, cipher, run, spec)
            await db.commit()
            return None
        steps = await _advance(db, cipher, run, spec, now=current)
        if run.status != 'running':
            await db.commit()
            return None
        active = sum(s.status == 'running' for s in steps)
        nodes = {n.id: n for n in spec.nodes}
        ready = sorted((s for s in steps if s.status == 'ready' and s.available_at <= current),
                       key=lambda s: (-nodes[s.node_id].priority, s.node_id))
        if not ready or active >= spec.max_parallel_steps:
            await db.commit()
            return None
        step = ready[0]
        step.status, step.attempts, step.claim_token = 'running', step.attempts + 1, uuid4()
        step.lease_until = current + timedelta(seconds=nodes[step.node_id].timeout_seconds)
        step.started_at = step.started_at or current
        step.deadline_at = step.deadline_at or current + timedelta(seconds=nodes[step.node_id].deadline_seconds)
        run.wake_at = current  # let another worker claim a parallel ready node
        claim = Claim(run.id, step.id, step.claim_token, step.attempts, step.lease_until)
        event(db, run, 'step.claimed', node_id=step.node_id, detail={'attempt': step.attempts})
        await db.commit()
        return claim


async def execute_claim(sessionmaker, cipher, claim):
    async with sessionmaker() as db:
        run = await db.get(WorkflowRun, claim.run_id, with_for_update=True)
        step = await db.get(WorkflowStep, claim.step_id, with_for_update=True)
        if run is None or run.status != 'running' or not claim.owns(step, datetime.now(UTC)):
            return False
        actor = await db.get(User, run.requester_id)
        version = await db.get(WorkflowVersion, run.version_id)
        node = next(n for n in Definition.model_validate(version.spec).nodes if n.id == step.node_id)
        if node.type == 'action' and not run.simulation:
            await db.rollback()
            from app.workflows.action_runtime import execute_action_claim
            return await execute_action_claim(sessionmaker, cipher, claim, node, run_context=run_context)
        context, _ = await run_context(db, cipher, run)
        try:
            async with db.begin_nested():
                if actor is None: raise InvalidInputError('Requester no longer exists.')
                await require_scope(db, actor, run.scope_id)
                if run.simulation and node.type not in {'start', 'end', 'task', 'parallel', 'decision'}:
                    finish(db, cipher, run, step, {'simulated': True, 'node_type': node.type,
                        'required_approvals': required_approvals(node) if node.type == 'approval' or node.risk != 'low' else 0}, simulated=True)
                else:
                    await _execute_node(db, cipher, actor, run, step, node, context)
        except AppError as exc:
            fail(db, run, step, exc.code)
        except (ValueError, TypeError, KeyError):
            fail(db, run, step, 'invalid_execution_data')
        await db.commit()
        return True


async def _execute_node(db, cipher, actor, run, step, node, context):
    if node.type == 'approval' or node.risk != 'low':
        expected, authorities = await approved_authorities(db, run, step, node, context)
        if step.approved_digest != expected or len(set(authorities)) < required_approvals(node):
            step.status, step.claim_token, step.lease_until = 'approval', None, None
            recipients = node.config.approver_ids or await managers(db, run.scope_id)
            for recipient in recipients:
                if recipient != run.requester_id: await notify(db, step, recipient, 'approval_required')
            event(db, run, 'approval.required', node_id=step.node_id, detail={'action_digest': expected})
            return
    if node.type in {'start', 'end', 'task', 'parallel', 'approval'}:
        finish(db, cipher, run, step, resolve(node.config.values, context))
    elif node.type == 'decision':
        finish(db, cipher, run, step, {'branch': 'true' if evaluate(node.config.condition, context) else 'false'})
    elif node.type == 'human':
        assignee = step.assignee_id or node.config.assignee_id
        if node.config.assignee_variable: assignee = UUID(str(lookup(node.config.assignee_variable, context)))
        if assignee is None: raise InvalidInputError('Human task has no assignee.')
        await require_member(db, run.scope_id, assignee)
        step.assignee_id, step.status = assignee, 'human'
        await notify(db, step, assignee, 'task_assigned')
        event(db, run, 'task.assigned', node_id=step.node_id, detail={'assignee_id': str(assignee)})
    elif node.type == 'timer':
        step.status, step.available_at = 'timer', datetime.now(UTC) + timedelta(seconds=node.config.delay_seconds)
        event(db, run, 'timer.waiting', node_id=step.node_id)
    elif node.type == 'event':
        step.status = 'event'
        event(db, run, 'event.waiting', node_id=step.node_id, detail={'name': node.config.event_name})
    elif node.type in {'agent', 'integration'}:
        adapter = ADAPTERS.get(node.config.adapter)
        if adapter is None or node.risk not in adapter.allowed_risks:
            raise InvalidInputError('Module adapter is unavailable or requires a stronger approval policy.')
        values = resolve(node.config.values, context)
        await adapter.authorize(db, actor, run.scope_id, node.config.resource_id, values)
        dispatch = await db.scalar(select(WorkflowDispatch).where(WorkflowDispatch.step_id == step.id,
            WorkflowDispatch.compensation.is_(False)))
        if dispatch is None:
            dispatch_id = uuid4()
            dispatch = WorkflowDispatch(id=dispatch_id, scope_id=run.scope_id, step_id=step.id, adapter=adapter.name,
                action_digest=action_hash(run, step, node, context), compensation=False, status='pending', attempts=0,
                payload=seal(cipher, run.scope_id, 'dispatch', dispatch_id, action_data(node, context)))
            db.add(dispatch)
        step.status = 'dispatched'
        event(db, run, 'action.dispatched', node_id=step.node_id, detail={'dispatch_id': str(dispatch.id), 'adapter': adapter.name})
    elif node.type in {'subworkflow', 'loop'}:
        service = WorkflowService(db, actor, cipher)
        version = await service.version(run.scope_id, node.config.child_version_id)
        child = await service.trigger(run.scope_id, version.workflow_id, resolve(node.config.values, context),
            f'child:{run.id}:{step.id}:{step.iteration}', version_id=version.id, parent_run_id=run.id,
            simulation=run.simulation, priority=run.priority)
        step.status, step.child_run_id = 'child', child.id
        event(db, run, 'subworkflow.started', node_id=step.node_id, detail={'child_run_id': str(child.id), 'iteration': step.iteration})
    else:
        raise InvalidInputError('Unknown executable node kind.')
    step.claim_token, step.lease_until = None, None
    run.wake_at = datetime.now(UTC)


async def record_failure(sessionmaker, claim, code='worker_exception'):
    async with sessionmaker() as db:
        run = await db.get(WorkflowRun, claim.run_id, with_for_update=True)
        step = await db.get(WorkflowStep, claim.step_id, with_for_update=True)
        now = datetime.now(UTC)
        if run is None or run.status != 'running' or not claim.owns(step, now): return False
        version = await db.get(WorkflowVersion, run.version_id)
        node = next(n for n in Definition.model_validate(version.spec).nodes if n.id == step.node_id)
        if step.attempts >= node.max_attempts:
            fail(db, run, step, code)
        else:
            step.status, step.claim_token, step.lease_until, step.failure_code = 'ready', None, None, code
            step.available_at = now + timedelta(seconds=min(300, 2 ** step.attempts))
            run.wake_at = step.available_at
            event(db, run, 'step.retry_scheduled', node_id=step.node_id, detail={'attempt': step.attempts, 'code': code})
        await db.commit()
        return True


async def process_dispatch(sessionmaker, cipher):
    # Only transactional module adapters are registered. Network workers belong to their owning modules.
    async with sessionmaker() as db:
        candidate = (await db.execute(select(WorkflowDispatch.id, WorkflowStep.run_id)
            .join(WorkflowStep, WorkflowStep.id == WorkflowDispatch.step_id)
            .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
            .where(WorkflowDispatch.status == 'pending', WorkflowDispatch.available_at <= datetime.now(UTC),
                   WorkflowRun.status.in_(['running', 'compensating']))
            .order_by(WorkflowDispatch.available_at).limit(1))).first()
        if candidate is None: return False
        run = await db.scalar(select(WorkflowRun).where(WorkflowRun.id == candidate.run_id).with_for_update(skip_locked=True))
        if run is None or run.status not in {'running', 'compensating'}: return False
        dispatch = await db.get(WorkflowDispatch, candidate.id, with_for_update=True)
        if dispatch.status != 'pending': return False
        step = await db.get(WorkflowStep, dispatch.step_id)
        version = await db.get(WorkflowVersion, run.version_id)
        node = next(n for n in Definition.model_validate(version.spec).nodes if n.id == step.node_id)
        actor = await db.get(User, run.requester_id)
        context, _ = await run_context(db, cipher, run)
        adapter = ADAPTERS.get(dispatch.adapter)
        try:
            if actor is None or adapter is None: raise ConflictError('Action adapter or owner is unavailable.')
            await require_scope(db, actor, run.scope_id)
            if node.type == 'approval' or node.risk != 'low':
                expected, valid = await approved_authorities(db, run, step, node, context)
                if dispatch.action_digest != expected or len(set(valid)) < required_approvals(node):
                    raise ConflictError('Approval authority changed before dispatch.')
            payload = unseal(cipher, run.scope_id, 'dispatch', dispatch.id, dispatch.payload)
            from app.platform.access import principal_for
            from app.platform.approvals import ActionSpec
            from app.platform.side_effects import dispatch_once
            from app.platform.sync import group_scope_sync
            from app.workflows.adapters import AdapterResult
            platform_scope = await db.run_sync(lambda s: group_scope_sync(s.connection(), run.scope_id))
            if platform_scope is None:
                raise ConflictError('Action adapter or owner is unavailable.')
            held = {}

            async def _dispatch():
                held['result'] = await adapter.execute(db, cipher, actor, run.scope_id, dispatch, payload)
                return str(dispatch.id)

            async def _verify(ref):
                from app.platform.execution import VerifiedResult
                item = held.get('result')
                if item is None:
                    return VerifiedResult(ok=True, details={'external_ref': ref, 'replayed': True})
                return VerifiedResult(ok=item.status in {'pending', 'completed'},
                                      details={'status': item.status, 'failure_code': item.failure_code})

            execution = await dispatch_once(
                db, principal_for(actor, channel='worker'), f'workflow:{dispatch.id}',
                ActionSpec(dispatch.adapter or 'workflow.action', platform_scope,
                           {'dispatch_id': str(dispatch.id)}, subject=f'workflow:{run.id}:{dispatch.id}'),
                duty='workflow.access', dispatch=_dispatch, verify=_verify,
            )
            result = held.get('result')
            if result is None:
                # Replayed after a previous successful dispatch: do not dispatch the adapter again.
                details = execution.verified_result or {}
                result = AdapterResult(status=details.get('status') or ('completed' if execution.status == 'verified' else 'unknown'),
                                       failure_code=details.get('failure_code'), receipt={'external_ref': execution.external_ref})
                if result.status == 'pending' and adapter.idempotent:
                    # The first dispatch was accepted but not finished. Idempotent adapters are *polled* (their
                    # inbox is keyed by dispatch.id), otherwise a pending step would never complete.
                    result = await adapter.execute(db, cipher, actor, run.scope_id, dispatch, payload)
            if result.status not in {'pending', 'completed', 'failed', 'unknown'}:
                raise InvalidInputError('Adapter returned an invalid outcome.')
            dispatch.status, dispatch.failure_code = result.status, result.failure_code
            dispatch.receipt = seal(cipher, run.scope_id, 'receipt', dispatch.id, result.receipt)
            dispatch.available_at = datetime.now(UTC) + timedelta(seconds=5)
            if result.status != 'pending':
                event(db, run, f'action.{result.status}', node_id=step.node_id, detail={'dispatch_id': str(dispatch.id)})
            run.wake_at = datetime.now(UTC)
        except AppError as exc:
            dispatch.status, dispatch.failure_code = 'failed', exc.code
            event(db, run, 'action.failed', node_id=step.node_id, detail={'code': exc.code})
        except Exception:
            # A non-idempotent adapter cannot be retried after an ambiguous outcome.
            dispatch.attempts += 1
            if not adapter or not adapter.idempotent:
                dispatch.status, dispatch.failure_code = 'unknown', 'ambiguous_action'
            elif dispatch.attempts >= node.max_attempts:
                dispatch.status, dispatch.failure_code = 'failed', 'adapter_attempts_exhausted'
            else:
                dispatch.available_at = datetime.now(UTC) + timedelta(seconds=2 ** dispatch.attempts)
                dispatch.failure_code = 'adapter_retry'
            event(db, run, 'action.error', node_id=step.node_id, detail={'code': dispatch.failure_code})
        await db.commit()
        return True


async def tick(sessionmaker, cipher, *, now=None):
    await schedule_due(sessionmaker, cipher, now=now)
    try:
        from app.workflows.unified import mirror_pending_automation_runs
        await mirror_pending_automation_runs(sessionmaker, cipher)
    except Exception as exc:  # a legacy-mirroring problem must never stop the runtime
        log.warning('Automation mirroring skipped (%s)', type(exc).__name__)
    try:
        from app.agent_studio.unified import mirror_pending_studio_runs
        await mirror_pending_studio_runs(sessionmaker, cipher)
    except Exception as exc:
        log.warning('Agent Studio mirroring skipped (%s)', type(exc).__name__)
    try:
        from app.services.agent_action_runtime import mirror_pending_proposals
        await mirror_pending_proposals(sessionmaker, cipher)
    except Exception as exc:
        log.warning('Agent action mirroring skipped (%s)', type(exc).__name__)
    try:
        from app.voice.unified import mirror_pending_voice_proposals
        await mirror_pending_voice_proposals(sessionmaker, cipher)
    except Exception as exc:
        log.warning('Voice proposal mirroring skipped (%s)', type(exc).__name__)
    claim = await claim_next(sessionmaker, cipher, now=now)
    if claim:
        try:
            await execute_claim(sessionmaker, cipher, claim)
        except Exception:
            await record_failure(sessionmaker, claim)
            log.exception('Workflow worker failed; no action payload is logged')
    dispatched = await process_dispatch(sessionmaker, cipher)
    return claim is not None or dispatched


async def run_worker(sessionmaker, cipher, stop, *, interval=1.0):
    while not stop.is_set():
        try:
            worked = await tick(sessionmaker, cipher)
        except Exception as exc:
            log.error('Workflow tick failed (%s)', type(exc).__name__)
            worked = False
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.05 if worked else interval)
        except TimeoutError:
            pass
