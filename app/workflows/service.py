"""Transactional workflow lifecycle and human decisions, using existing identity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import AccessGroup, AccessGroupMember, DepartmentManager, User
from app.db.workflow_engine_models import (Workflow, WorkflowVersion, WorkflowRun, WorkflowStep,
    WorkflowDecision, WorkflowEvent, WorkflowDispatch, WorkflowSignal)
from app.services.audit import AuditContext, record
from app.workflows.definition import Definition, Node, digest, lookup, resolve, validate_form, validate_graph, bounded_data
from app.workflows.events import event, verify_chain
from app.workflows.security import (decision_authority, is_manager, require_member, require_owner,
    require_scope, seal, unseal)

ACTIVE_RUNS = ('queued', 'running', 'paused', 'compensating')


def action_data(node, context):
    return {'node': node.model_dump(mode='json'), 'values': resolve(node.config.values, context),
            'amount': lookup(node.config.amount_variable, context) if node.config.amount_variable else None}


def action_hash(run, step, node, context):
    return digest({'run_id': str(run.id), 'step_id': str(step.id), 'version_id': str(run.version_id),
                   'requester_id': str(run.requester_id), 'action': action_data(node, context)})


def required_approvals(node):
    if node.config.approval_chain and node.config.approver_ids:
        return max(len(node.config.approver_ids), 2 if node.risk == 'critical' else 1)
    return 2 if node.risk == 'critical' else 1


async def approved_authorities(db, run, step, node, context):
    expected = action_hash(run, step, node, context)
    decisions = list((await db.scalars(select(WorkflowDecision).where(
        WorkflowDecision.scope_id == run.scope_id, WorkflowDecision.step_id == step.id,
        WorkflowDecision.action_digest == expected, WorkflowDecision.approved.is_(True),
    ).order_by(WorkflowDecision.created_at))).all())
    valid, actors = [], set()
    for decision in decisions:
        actor = await db.get(User, decision.actor_id)
        if actor is None or actor.id in actors:
            continue
        try:
            await decision_authority(db, actor, run.scope_id, run.requester_id, node, context, decision.authority_id)
        except (PermissionDeniedError, InvalidInputError, NotFoundError):
            continue
        valid.append(decision.authority_id)
        actors.add(actor.id)
    return expected, valid


async def run_context(db, cipher, run):
    steps = list((await db.scalars(select(WorkflowStep).where(
        WorkflowStep.run_id == run.id, WorkflowStep.scope_id == run.scope_id))).all())
    return {'input': unseal(cipher, run.scope_id, 'run', run.id, run.payload),
            'steps': {s.node_id: unseal(cipher, run.scope_id, 'step', s.id, s.output)
                      for s in steps if s.output is not None}}, steps


class WorkflowService:
    def __init__(self, db, actor, cipher, ctx=None):
        self.db, self.actor, self.cipher = db, actor, cipher
        self.ctx = ctx or AuditContext(actor.id, actor.email, None, None)

    async def scopes(self):
        groups = select(AccessGroup).where(AccessGroup.kind == 'department')
        if self.actor.role != 'admin':
            memberships = select(AccessGroupMember.group_id).where(AccessGroupMember.user_id == self.actor.id)
            managers = select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == self.actor.id)
            groups = groups.where(AccessGroup.id.in_(memberships.union(managers)))
        result = []
        for group in (await self.db.scalars(groups.order_by(AccessGroup.name))).all():
            result.append({'id': str(group.id), 'name': group.name,
                           'can_manage': await is_manager(self.db, self.actor, group.id)})
        return result

    async def workflow(self, scope_id, workflow_id, *, lock=False):
        await require_scope(self.db, self.actor, scope_id)
        query = select(Workflow).where(Workflow.scope_id == scope_id, Workflow.id == workflow_id)
        if lock: query = query.with_for_update()
        row = await self.db.scalar(query)
        if row is None: raise NotFoundError('Workflow not found in this scope.')
        return row

    async def version(self, scope_id, version_id):
        row = await self.db.scalar(select(WorkflowVersion).where(
            WorkflowVersion.scope_id == scope_id, WorkflowVersion.id == version_id))
        if row is None: raise NotFoundError('Workflow version not found in this scope.')
        return row

    async def run(self, scope_id, run_id, *, lock=False):
        await require_scope(self.db, self.actor, scope_id)
        query = select(WorkflowRun).where(WorkflowRun.scope_id == scope_id, WorkflowRun.id == run_id)
        if lock: query = query.with_for_update()
        row = await self.db.scalar(query)
        if row is None: raise NotFoundError('Execution not found in this scope.')
        # Department managers see team execution; members only see requested or assigned runs.
        if row.requester_id != self.actor.id and not await is_manager(self.db, self.actor, scope_id):
            assigned = await self.db.scalar(select(WorkflowStep.id).where(
                WorkflowStep.run_id == row.id, WorkflowStep.scope_id == scope_id,
                WorkflowStep.assignee_id == self.actor.id).limit(1))
            if assigned is None:
                # Named approval authorities may review the run to make an informed decision.
                version = await self.version(scope_id, row.version_id)
                if not any(str(self.actor.id) in n['config'].get('approver_ids', []) for n in version.spec['nodes']):
                    raise NotFoundError('Execution not found in your task scope.')
        return row

    async def check_definition(self, scope_id, spec, *, publishing=False):
        await require_scope(self.db, self.actor, scope_id)
        errors = validate_graph(spec)
        from app.workflows.adapters import ADAPTERS
        for node in spec.nodes:
            try:
                for person in [node.config.assignee_id, *node.config.approver_ids]:
                    if person: await require_member(self.db, scope_id, person)
                if node.config.child_version_id:
                    child = await self.version(scope_id, node.config.child_version_id)
                    if child.status != 'published':
                        raise InvalidInputError('Child workflow must pin a published version.')
                if node.config.adapter:
                    adapter = ADAPTERS.get(node.config.adapter)
                    if adapter is None:
                        raise InvalidInputError('Adapter is not installed. Module contracts cannot be executed until registered.')
                    if node.risk not in adapter.allowed_risks:
                        raise InvalidInputError('Node risk is below the adapter approval policy.')
                    if publishing:
                        await adapter.authorize(self.db, self.actor, scope_id, node.config.resource_id, node.config.values)
                if node.config.compensation_adapter and node.config.compensation_adapter not in ADAPTERS:
                    raise InvalidInputError('Compensation adapter is not installed.')
            except (InvalidInputError, PermissionDeniedError, NotFoundError) as exc:
                errors.append({'code': 'scope_or_contract', 'message': str(exc), 'node_id': node.id})
        return errors

    async def save(self, scope_id, name, description, spec, *, workflow_id=None, base_version=None):
        await require_scope(self.db, self.actor, scope_id)
        if workflow_id:
            workflow = await self.workflow(scope_id, workflow_id, lock=True)
            await require_owner(self.db, self.actor, workflow)
            if base_version != workflow.latest_version:
                raise ConflictError('A newer draft exists. Reload before saving.')
            number = workflow.latest_version + 1
            workflow.latest_version, workflow.name, workflow.description = number, name, description
            workflow.updated_at = datetime.now(UTC)
        else:
            number = 1
            workflow = Workflow(id=uuid4(), scope_id=scope_id, owner_id=self.actor.id,
                                name=name, description=description, latest_version=1)
            self.db.add(workflow)
            await self.db.flush()
        data = spec.model_dump(mode='json')
        version = WorkflowVersion(id=uuid4(), scope_id=scope_id, workflow_id=workflow.id,
            number=number, spec=data, digest=digest(data), author_id=self.actor.id, status='draft')
        self.db.add(version)
        record(self.db, self.ctx, 'workflow.draft_saved', target=str(workflow.id),
               details={'scope_id': str(scope_id), 'version': number, 'digest': version.digest})
        await self.db.flush()
        return workflow, version

    async def review(self, scope_id, workflow_id, version_id):
        workflow = await self.workflow(scope_id, workflow_id, lock=True)
        await require_owner(self.db, self.actor, workflow)
        version = await self.version(scope_id, version_id)
        if version.workflow_id != workflow.id or version.status != 'draft':
            raise ConflictError('Only a draft of this workflow can enter review.')
        errors = await self.check_definition(scope_id, Definition.model_validate(version.spec), publishing=True)
        if errors: raise InvalidInputError('Resolve workflow validation errors before review.', details={'errors': errors})
        version.status = 'review'
        record(self.db, self.ctx, 'workflow.review_requested', target=str(version.id), details={'digest': version.digest})
        return version

    async def review_decision(self, scope_id, workflow_id, version_id, approved, note):
        workflow = await self.workflow(scope_id, workflow_id, lock=True)
        await require_scope(self.db, self.actor, scope_id, manage=True)
        version = await self.version(scope_id, version_id)
        if version.workflow_id != workflow.id or version.status != 'review':
            raise ConflictError('This workflow version is not awaiting review.')
        if self.actor.id in {workflow.owner_id, version.author_id}:
            raise PermissionDeniedError('A different authorized manager must review the definition.')
        if await self.db.scalar(select(WorkflowDecision.id).where(
            WorkflowDecision.version_id == version.id, WorkflowDecision.authority_id == self.actor.id)):
            raise ConflictError('Your immutable review decision is already recorded.')
        decision_id = uuid4()
        self.db.add(WorkflowDecision(id=decision_id, scope_id=scope_id, version_id=version.id,
            actor_id=self.actor.id, authority_id=self.actor.id, approved=approved, action_digest=version.digest,
            evidence=seal(self.cipher, scope_id, 'decision', decision_id, {'note': note})))
        if not approved: version.status = 'rejected'
        record(self.db, self.ctx, 'workflow.review_decided', target=str(version.id), details={'approved': approved, 'digest': version.digest})
        return version

    async def publish(self, scope_id, workflow_id, version_id):
        workflow = await self.workflow(scope_id, workflow_id, lock=True)
        await require_owner(self.db, self.actor, workflow)
        version = await self.version(scope_id, version_id)
        if version.workflow_id != workflow.id or version.status not in {'review', 'published'}:
            raise ConflictError('A reviewed executable version is required.')
        spec = Definition.model_validate(version.spec)
        errors = await self.check_definition(scope_id, spec, publishing=True)
        if errors: raise InvalidInputError('Workflow cannot be published.', details={'errors': errors})
        reviewers = (await self.db.scalars(select(WorkflowDecision).where(
            WorkflowDecision.version_id == version.id, WorkflowDecision.approved.is_(True),
            WorkflowDecision.action_digest == version.digest))).all()
        valid = set()
        for decision in reviewers:
            actor = await self.db.get(User, decision.actor_id)
            if actor and actor.id not in {workflow.owner_id, version.author_id} and await is_manager(self.db, actor, scope_id):
                valid.add(actor.id)
        quorum = 2 if any(n.risk == 'critical' for n in spec.nodes) else 1
        if len(valid) < quorum:
            raise PermissionDeniedError(f'{quorum} current independent manager approval(s) are required.')
        version.status, version.published_at = 'published', version.published_at or datetime.now(UTC)
        workflow.active_version, workflow.enabled = version.number, True
        workflow.next_run_at = datetime.fromisoformat(spec.trigger.start_at) if spec.trigger.type in {'schedule', 'interval'} else None
        record(self.db, self.ctx, 'workflow.published', target=str(workflow.id), details={'version': version.number, 'digest': version.digest})
        return version

    async def trigger(self, scope_id, workflow_id, inputs, key, *, simulation=False, version_id=None,
                      parent_run_id=None, priority=0, source='manual'):
        workflow = await self.workflow(scope_id, workflow_id, lock=True)
        if version_id:
            version = await self.version(scope_id, version_id)
            if version.workflow_id != workflow.id: raise NotFoundError('Version does not belong to this workflow.')
        else:
            version = await self.db.scalar(select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow.id, WorkflowVersion.number == workflow.active_version))
        if version is None or (not simulation and (version.status != 'published' or not workflow.enabled)):
            raise ConflictError('Workflow must be reviewed, published and enabled before execution.')
        if not simulation and parent_run_id is None and source != 'manual':
            spec_trigger = Definition.model_validate(version.spec).trigger
            if spec_trigger.type != source: raise ConflictError('Trigger source does not match the published definition.')
        spec = Definition.model_validate(version.spec)
        errors = await self.check_definition(scope_id, spec)
        if errors: raise InvalidInputError('Workflow is not executable.', details={'errors': errors})
        try: validate_form(spec.input_form, inputs)
        except ValueError as exc: raise InvalidInputError(str(exc)) from None
        input_hash = digest({'inputs': inputs, 'version_id': str(version.id), 'simulation': simulation,
                             'requester_id': str(self.actor.id), 'source': source})
        previous = await self.db.scalar(select(WorkflowRun).where(
            WorkflowRun.workflow_id == workflow.id, WorkflowRun.idempotency_key == key))
        if previous:
            if previous.input_digest != input_hash: raise ConflictError('Idempotency key was used for a different request.')
            return previous
        count = await self.db.scalar(select(func.count()).select_from(WorkflowRun).where(
            WorkflowRun.workflow_id == workflow.id, WorkflowRun.status.in_(ACTIVE_RUNS)))
        if count >= 1000: raise ConflictError('Workflow queue quota reached (1000 unfinished runs).')
        if parent_run_id:
            ancestor = await self.db.get(WorkflowRun, parent_run_id)
            seen, depth = {version.id}, 0
            while ancestor:
                if ancestor.scope_id != scope_id or ancestor.version_id in seen or depth >= 5:
                    raise InvalidInputError('Recursive or over-depth subworkflow execution is forbidden.')
                seen.add(ancestor.version_id)
                depth += 1
                ancestor = await self.db.get(WorkflowRun, ancestor.parent_run_id) if ancestor.parent_run_id else None
        now, run_id = datetime.now(UTC), uuid4()
        run = WorkflowRun(id=run_id, scope_id=scope_id, workflow_id=workflow.id, version_id=version.id,
            requester_id=self.actor.id, parent_run_id=parent_run_id, idempotency_key=key,
            input_digest=input_hash, payload=seal(self.cipher, scope_id, 'run', run_id, inputs),
            status='queued', simulation=simulation, priority=priority, event_sequence=0, event_hash='0' * 64, wake_at=now)
        self.db.add(run)
        await self.db.flush()
        for node in spec.nodes:
            self.db.add(WorkflowStep(id=uuid4(), scope_id=scope_id, run_id=run.id, node_id=node.id,
                status='ready' if node.type == 'start' else 'blocked', assignee_id=node.config.assignee_id,
                available_at=now, attempts=0, iteration=0))
        event(self.db, run, 'run.created', actor_id=self.actor.id, detail={'version': version.number, 'simulation': simulation, 'source': source})
        record(self.db, self.ctx, 'workflow.run_requested', target=str(run.id), details={'scope_id': str(scope_id), 'version_id': str(version.id)})
        await self.db.flush()
        return run

    async def step_subject(self, scope_id, run_id, step_id):
        run = await self.run(scope_id, run_id, lock=True)
        step = await self.db.scalar(select(WorkflowStep).where(
            WorkflowStep.scope_id == scope_id, WorkflowStep.run_id == run.id, WorkflowStep.id == step_id).with_for_update())
        if step is None: raise NotFoundError('Task not found in this execution.')
        version = await self.version(scope_id, run.version_id)
        node = next(n for n in Definition.model_validate(version.spec).nodes if n.id == step.node_id)
        context, _ = await run_context(self.db, self.cipher, run)
        return run, step, node, context

    async def complete_task(self, scope_id, run_id, step_id, output):
        run, step, node, _ = await self.step_subject(scope_id, run_id, step_id)
        if run.status != 'running' or step.status != 'human': raise ConflictError('Task is not awaiting completion.')
        if step.assignee_id != self.actor.id: raise PermissionDeniedError('Only the current assigned member can complete this task.')
        try: validate_form(node.config.form, output)
        except ValueError as exc: raise InvalidInputError(str(exc)) from None
        step.status, step.finished_at = 'completed', datetime.now(UTC)
        step.output = seal(self.cipher, scope_id, 'step', step.id, output)
        run.wake_at = datetime.now(UTC)
        event(self.db, run, 'task.completed', node_id=node.id, actor_id=self.actor.id)
        return step

    async def decide(self, scope_id, run_id, step_id, approved, note, *, on_behalf_of=None, evidence=None):
        run, step, node, context = await self.step_subject(scope_id, run_id, step_id)
        if run.status != 'running' or step.status != 'approval': raise ConflictError('Action is not awaiting approval.')
        if step.deadline_at and datetime.now(UTC) >= step.deadline_at: raise ConflictError('Approval deadline expired; recover this execution explicitly.')
        authority = await decision_authority(self.db, self.actor, scope_id, run.requester_id, node, context, on_behalf_of)
        expected, valid = await approved_authorities(self.db, run, step, node, context)
        previous = (await self.db.scalars(select(WorkflowDecision).where(WorkflowDecision.step_id == step.id,
            WorkflowDecision.action_digest == expected))).all()
        if any(d.actor_id == self.actor.id or d.authority_id == authority.id for d in previous):
            raise ConflictError('Each person and authority can decide this exact action only once.')
        if node.config.approval_chain and node.config.approver_ids:
            next_authority = next((p for p in node.config.approver_ids if p not in valid), None)
            if next_authority != authority.id: raise PermissionDeniedError('An earlier approval-chain authority must decide first.')
        bounded_data(evidence or {})
        decision_id = uuid4()
        self.db.add(WorkflowDecision(id=decision_id, scope_id=scope_id, step_id=step.id,
            actor_id=self.actor.id, authority_id=authority.id, approved=approved, action_digest=expected,
            evidence=seal(self.cipher, scope_id, 'decision', decision_id, {'note': note, 'evidence': evidence or {}})))
        event(self.db, run, 'approval.approved' if approved else 'approval.rejected', node_id=node.id,
              actor_id=self.actor.id, detail={'action_digest': expected, 'authority_id': str(authority.id)})
        if not approved:
            step.status, step.failure_code = 'failed', 'approval_rejected'
            run.status, run.failure_code, run.finished_at = 'failed', 'approval_rejected', datetime.now(UTC)
        elif len(set(valid + [authority.id])) >= required_approvals(node):
            step.approved_digest = expected
            step.status = 'ready'
        run.wake_at = datetime.now(UTC)
        return step

    async def reassign(self, scope_id, run_id, step_id, assignee_id, note):
        run, step, _, _ = await self.step_subject(scope_id, run_id, step_id)
        if run.status != 'running' or step.status != 'human': raise ConflictError('Only an active human task can be reassigned.')
        if self.actor.id != step.assignee_id:
            await require_scope(self.db, self.actor, scope_id, manage=True)
        await require_member(self.db, scope_id, assignee_id)
        previous, step.assignee_id = step.assignee_id, assignee_id
        event(self.db, run, 'task.reassigned', node_id=step.node_id, actor_id=self.actor.id,
              detail={'from': str(previous), 'to': str(assignee_id), 'reason_digest': digest(note)})
        return step

    async def control(self, scope_id, run_id, command):
        run = await self.run(scope_id, run_id, lock=True)
        if self.actor.id != run.requester_id:
            await require_scope(self.db, self.actor, scope_id, manage=True)
        steps = (await self.db.scalars(select(WorkflowStep).where(WorkflowStep.run_id == run.id))).all()
        dispatches = (await self.db.scalars(select(WorkflowDispatch).where(
            WorkflowDispatch.scope_id == scope_id, WorkflowDispatch.step_id.in_([s.id for s in steps])))).all()
        now = datetime.now(UTC)
        if command == 'pause' and run.status in {'queued', 'running'}:
            run.status = 'paused'
        elif command == 'resume' and run.status == 'paused':
            run.status = 'queued' if run.started_at is None else 'running'
        elif command == 'cancel' and run.status in ACTIVE_RUNS:
            if any(d.status in {'running', 'unknown'} for d in dispatches):
                raise ConflictError('Reconcile in-flight or ambiguous external actions before cancellation.')
            run.status, run.finished_at = 'cancelled', now
            for step in steps:
                if step.status not in {'completed', 'skipped', 'compensated'}:
                    step.status, step.claim_token, step.lease_until = 'cancelled', None, None
            for dispatch in dispatches:
                if dispatch.status == 'pending': dispatch.status = 'cancelled'
        elif command == 'retry' and run.status == 'failed':
            if any(d.status == 'unknown' for d in dispatches):
                raise ConflictError('Ambiguous external actions require a verified receipt; blind retry is forbidden.')
            if run.failure_code in {'approval_rejected', 'approval_expired'}:
                raise ConflictError('Rejected or expired actions require a new run and new approvals.')
            for step in steps:
                if step.status == 'failed':
                    step.status, step.failure_code, step.attempts = 'ready', None, 0
                    step.claim_token, step.lease_until, step.available_at = None, None, now
            for dispatch in dispatches:
                if dispatch.status == 'failed':
                    dispatch.status, dispatch.attempts, dispatch.available_at = 'pending', 0, now
            run.status, run.failure_code, run.finished_at = 'queued', None, None
        elif command == 'compensate' and run.status in {'failed', 'cancelled', 'completed'}:
            await require_scope(self.db, self.actor, scope_id, manage=True)
            if any(d.status in {'running', 'unknown'} for d in dispatches):
                raise ConflictError('Reconcile external action outcomes before compensation.')
            version = await self.version(scope_id, run.version_id)
            nodes = {n.id: n for n in Definition.model_validate(version.spec).nodes}
            if any(s.status == 'completed' and nodes[s.node_id].type in {'agent', 'integration'}
                   and not nodes[s.node_id].config.compensation_adapter for s in steps):
                raise ConflictError('A completed external action has no approved compensating adapter.')
            run.status, run.finished_at = 'compensating', None
        else:
            raise ConflictError('This control is unavailable in the current execution state.')
        run.wake_at = now
        event(self.db, run, f'run.{command}', actor_id=self.actor.id)
        return run

    async def signal(self, scope_id, run_id, name, payload, key):
        run = await self.run(scope_id, run_id, lock=True)
        if run.status not in ACTIVE_RUNS: raise ConflictError('Execution no longer accepts signals.')
        if self.actor.id != run.requester_id: await require_scope(self.db, self.actor, scope_id, manage=True)
        bounded_data(payload)
        expected = digest({'name': name, 'payload': payload})
        previous = await self.db.scalar(select(WorkflowSignal).where(
            WorkflowSignal.run_id == run.id, WorkflowSignal.idempotency_key == key))
        if previous:
            if previous.digest != expected: raise ConflictError('Signal key was already used for another payload.')
            return previous
        pending = await self.db.scalar(select(func.count()).select_from(WorkflowSignal).where(
            WorkflowSignal.run_id == run.id, WorkflowSignal.consumed_by.is_(None)))
        if pending >= 100: raise ConflictError('Execution signal queue is full.')
        row_id = uuid4()
        row = WorkflowSignal(id=row_id, scope_id=scope_id, run_id=run.id, name=name, idempotency_key=key,
            digest=expected, payload=seal(self.cipher, scope_id, 'signal', row_id, payload))
        self.db.add(row)
        run.wake_at = datetime.now(UTC)
        event(self.db, run, 'signal.received', actor_id=self.actor.id, detail={'name': name, 'digest': expected})
        return row

    async def detail(self, scope_id, run_id):
        run = await self.run(scope_id, run_id)
        version = await self.version(scope_id, run.version_id)
        context, steps = await run_context(self.db, self.cipher, run)
        events = (await self.db.scalars(select(WorkflowEvent).where(WorkflowEvent.run_id == run.id,
            WorkflowEvent.scope_id == scope_id).order_by(WorkflowEvent.sequence))).all()
        decisions = (await self.db.scalars(select(WorkflowDecision).where(WorkflowDecision.scope_id == scope_id,
            WorkflowDecision.step_id.in_([s.id for s in steps])).order_by(WorkflowDecision.created_at))).all()
        return {'run': row_dict(run, exclude={'payload'}), 'spec': version.spec, 'inputs': context['input'],
                'steps': [row_dict(s, exclude={'output', 'claim_token'}) | {'output': context['steps'].get(s.node_id)} for s in steps],
                'events': [row_dict(e) for e in events], 'audit_verified': verify_chain(events) and
                    len(events) == run.event_sequence and (events[-1].hash if events else '0' * 64) == run.event_hash,
                'decisions': [row_dict(d, exclude={'evidence'}) | {'evidence': unseal(self.cipher, scope_id, 'decision', d.id, d.evidence)} for d in decisions]}


def row_dict(row, *, exclude=frozenset()):
    return {col.name: getattr(row, col.name) for col in row.__table__.columns if col.name not in exclude}
