"""One workflow runtime (step E): legacy paths become definitions and runs of the wf05 engine.

* Automation rules (`services/automation*`, kept as an API facade) -> one mirrored definition per rule
  (`origin='automation'`, `origin_ref=definition_key`) whose single step is the `automation.run` adapter. Every
  legacy run is mirrored as a workflow run (`origin_ref=<automation run id>`), so all runs are listed, audited,
  paused and verified in one place, and the action executes through one idempotent executor.
* Agent Studio runs -> one `origin='studio'` run each (`app.agent_studio.unified`): the engine executes the run
  through the `agent_studio.execute` action handler (pause / stop / resume, `execute_once` per attempt).
* Agent action proposals (governance) -> one `origin='governance'` run each (`app.services.agent_action_runtime`):
  approval step + permissioned execute step through `execute_once`.
* Designer steps can still call `agent_studio.run` / `governance.execute` adapters.
* Code-defined system templates (e.g. the invoice flow) -> `origin='system'` definitions, pinned by digest.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import String, and_, select

from app.core.errors import AppError
from app.db.models import AutomationRun, AutomationWorkflow, User
from app.db.workflow_engine_models import Workflow, WorkflowRun, WorkflowVersion
from app.services.audit import AuditContext, record
from app.workflows.definition import Definition, digest

log = logging.getLogger(__name__)


def automation_spec(name: str) -> dict:
    return Definition.model_validate({
        'source': 'template',
        'input_form': [{'name': 'automation_run_id', 'label': 'Automation run', 'type': 'text', 'required': True}],
        'nodes': [
            {'id': 'start', 'type': 'start', 'label': 'Rule triggered', 'x': 80, 'y': 160},
            {'id': 'action', 'type': 'integration', 'label': name[:120] or 'Automation action', 'x': 340, 'y': 160,
             'risk': 'low', 'config': {'adapter': 'automation.run',
                                       'values': {'automation_run_id': '$input.automation_run_id'}}},
            {'id': 'end', 'type': 'end', 'label': 'Action verified', 'x': 600, 'y': 160},
        ],
        'edges': [{'source': 'start', 'target': 'action'}, {'source': 'action', 'target': 'end'}],
    }).model_dump(mode='json')


async def ensure_definition(db, *, scope_id, owner_id, origin, origin_ref, name, description, spec) -> Workflow:
    """Idempotently create a published, enabled definition for a legacy rule or system template."""
    row = await db.scalar(select(Workflow).where(Workflow.scope_id == scope_id, Workflow.origin == origin,
                                                 Workflow.origin_ref == origin_ref))
    if row is not None:
        return row
    now = datetime.now(UTC)
    row = Workflow(id=uuid.uuid4(), scope_id=scope_id, owner_id=owner_id, name=name[:160], description=description[:2000],
                   latest_version=1, active_version=1, enabled=True, origin=origin, origin_ref=origin_ref)
    db.add(row)
    await db.flush()
    db.add(WorkflowVersion(id=uuid.uuid4(), scope_id=scope_id, workflow_id=row.id, number=1, spec=spec,
                           digest=digest(spec), status='published', author_id=owner_id, published_at=now))
    record(db, AuditContext(owner_id, None, None, None), 'workflow.system_definition_installed', target=str(row.id),
           details={'origin': origin, 'origin_ref': origin_ref, 'digest': digest(spec)})
    await db.flush()
    return row


async def ensure_automation_definition(db, rule: AutomationWorkflow) -> Workflow | None:
    owner_id = rule.created_by_user_id
    if owner_id is None:
        return None
    return await ensure_definition(db, scope_id=rule.department_group_id, owner_id=owner_id, origin='automation',
                                   origin_ref=str(rule.definition_key), name=rule.name,
                                   description='Automation rule on the unified workflow runtime (legacy API facade).',
                                   spec=automation_spec(rule.name))


async def mirror_automation_run(db, cipher, run: AutomationRun) -> WorkflowRun | None:
    from app.workflows.service import WorkflowService
    rule = await db.get(AutomationWorkflow, run.workflow_id)
    if rule is None:
        return None
    definition = await ensure_automation_definition(db, rule)
    if definition is None:
        return None
    owner = await db.get(User, definition.owner_id)
    if owner is None or not owner.is_active:
        return None
    service = WorkflowService(db, owner, cipher, AuditContext(owner.id, owner.email, None, None))
    return await service.trigger(run.department_group_id, definition.id, {'automation_run_id': str(run.id)},
                                 f'automation:{run.id}', origin='automation', origin_ref=str(run.id))


_UNMIRRORABLE: set[uuid.UUID] = set()  # per process: runs whose rule owner cannot act in the department


async def mirror_pending_automation_runs(sessionmaker, cipher, *, limit: int = 25) -> int:
    """Attach legacy automation runs that have no unified run yet (API, schedule and service-request paths)."""
    count = 0
    async with sessionmaker() as db:
        query = (select(AutomationRun).outerjoin(WorkflowRun, and_(
            WorkflowRun.origin == 'automation', WorkflowRun.origin_ref == AutomationRun.id.cast(String),
        )).where(WorkflowRun.id.is_(None), AutomationRun.status.in_(['pending_approval', 'queued', 'processing'])))
        if _UNMIRRORABLE:
            query = query.where(AutomationRun.id.not_in(list(_UNMIRRORABLE)[-5000:]))
        missing = (await db.scalars(query.order_by(AutomationRun.created_at).limit(limit))).all()
        for run in missing:
            try:
                async with db.begin_nested():
                    if await mirror_automation_run(db, cipher, run) is not None:
                        count += 1
                    else:
                        _UNMIRRORABLE.add(run.id)
            except AppError as exc:
                _UNMIRRORABLE.add(run.id)
                log.info('Automation run %s not mirrored (%s)', run.id, exc.code)
        await db.commit()
    return count


async def ensure_system_definition(db, *, scope_id, owner_id, origin_ref, name, description, spec,
                                   origin='system') -> Workflow:
    """A code-defined template pinned by digest: installed once per department, and a new immutable version is
    published when the code's template changes (running executions keep the version they started with). A manager
    who disabled the definition keeps it disabled."""
    row = await ensure_definition(db, scope_id=scope_id, owner_id=owner_id, origin=origin, origin_ref=origin_ref,
                                  name=name, description=description, spec=spec)
    current = await db.scalar(select(WorkflowVersion).where(WorkflowVersion.workflow_id == row.id,
                                                            WorkflowVersion.number == row.active_version))
    wanted = digest(spec)
    if current is not None and current.digest == wanted:
        return row
    locked = await db.scalar(select(Workflow).where(Workflow.id == row.id).with_for_update())
    if current is None or current.digest != wanted:
        number = (locked.latest_version or 0) + 1
        db.add(WorkflowVersion(id=uuid.uuid4(), scope_id=scope_id, workflow_id=locked.id, number=number, spec=spec,
                               digest=wanted, status='published', author_id=owner_id, published_at=datetime.now(UTC)))
        locked.latest_version = locked.active_version = number
        record(db, AuditContext(owner_id, None, None, None), 'workflow.system_definition_upgraded', target=str(locked.id),
               details={'origin_ref': origin_ref, 'version': number, 'digest': wanted})
    await db.flush()
    return locked


async def origin_run(db, origin: str, origin_ref, *, lock=False) -> WorkflowRun | None:
    """The unified run that carries a legacy record (newest first)."""
    query = select(WorkflowRun).where(WorkflowRun.origin == origin, WorkflowRun.origin_ref == str(origin_ref))
    if lock:
        query = query.with_for_update()
    return await db.scalar(query.order_by(WorkflowRun.created_at.desc()).limit(1))


async def wake_origin_run(db, origin: str, origin_ref) -> None:
    """A legacy API changed the record a unified run waits on: poll it now instead of at the next interval."""
    from app.db.workflow_engine_models import WorkflowStep
    run = await origin_run(db, origin, origin_ref)
    if run is None or run.status not in {'queued', 'running'}:
        return
    now = datetime.now(UTC)
    steps = (await db.scalars(select(WorkflowStep).where(WorkflowStep.run_id == run.id,
                                                          WorkflowStep.status == 'waiting'))).all()
    for step in steps:
        step.available_at = now
    run.wake_at = now
