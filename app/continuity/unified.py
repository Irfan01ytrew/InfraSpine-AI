"""Continuity activations on the unified workflow runtime (Step G slice 4).

Every approved continuity activation gets one workflow run
(`origin='continuity'`, `origin_ref=<continuity run id>`) in the plan's
department. The engine executes it through the `continuity.execute`
action handler, so an activation is listed, audited, paused, stopped and
resumed in the same place as every other process:

* the handler advances the activation's plan steps: the next ready step
  with a bound workflow is dispatched through `execute_once` (key
  ``continuity:<run>:task:<task>:dispatch``, duty ``workflow.access``,
  read back from the continuity task), so a crash or a second engine
  never dispatches the same step twice;
* steps without a bound workflow are manual: the handler waits for the
  owner to submit evidence through the existing continuity API — it
  never invents completion;
* the activation's own approval (exact-action, digest-bound, separation
  of duties) still decides in continuity; the unified run never
  approves, pays, or grants anything on its own;
* stopping the workflow run suspends the continuity run
  (`on_workflow_cancelled`).

The existing continuity APIs and tables are unchanged (adapter, not a
rewrite). The engine tick's pending sweep mirrors activations whose
creation-time mirror was skipped (e.g. no department group, or the
requester is not a workflow-scope member); the legacy API keeps
working for those.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import String, and_, select

from app.continuity.models import Node, Plan, Run, Task
from app.core.errors import AppError
from app.db.models import User
from app.db.workflow_engine_models import WorkflowRun
from app.platform.approvals import ActionSpec
from app.platform.execution import VerifiedResult, execute_once
from app.services.audit import AuditContext
from app.workflows.actions import ActionContext, ActionOutcome, register
from app.workflows.definition import Definition

log = logging.getLogger(__name__)

TEMPLATE_REF = "continuity.runs"
TEMPLATE_NAME = "Continuity activations"
TEMPLATE_DESCRIPTION = (
    "Approved continuity plan activations on the unified runtime: "
    "steps dispatch here; evidence and verification stay in continuity."
)
DUTY = "workflow.access"


def continuity_spec() -> dict:
    return Definition.model_validate({
        "source": "template",
        "input_form": [{"name": "continuity_run_id", "label": "Continuity run", "type": "text", "required": True}],
        "nodes": [
            {"id": "start", "type": "start", "label": "Activation approved", "x": 80, "y": 160},
            {"id": "activate", "type": "action", "label": "Continuity activation (verified)", "x": 340, "y": 160,
             "risk": "medium", "timeout_seconds": 600, "max_attempts": 5,
             "config": {"handler": "continuity.execute", "auto": True,
                        "values": {"continuity_run_id": "$input.continuity_run_id"}}},
            {"id": "end", "type": "end", "label": "Recovery verified", "x": 600, "y": 160},
        ],
        "edges": [{"source": "start", "target": "activate"}, {"source": "activate", "target": "end"}],
    }).model_dump(mode="json")


async def scope_group_for_plan(db, plan: Plan) -> uuid.UUID | None:
    """The department access group a plan belongs to (its department node)."""
    if plan.department_id is None:
        return None
    node = await db.get(Node, plan.department_id)
    if node is None or node.tenant_id != plan.tenant_id:
        return None
    return node.access_group_id


async def mirror_continuity_run(db, cipher, run: Run) -> WorkflowRun | None:
    """Mirror one approved activation to a unified workflow run (idempotent).

    All writes happen inside a SAVEPOINT: a mirror failure (bad spec,
    missing scope, a race on the definition) rolls back only the
    savepoint, so the caller's transaction — e.g. the activation
    decision that triggered the mirror — stays usable.
    """
    from app.workflows.service import WorkflowService
    from app.workflows.unified import ensure_system_definition, origin_run

    existing = await origin_run(db, "continuity", run.id)
    if existing is not None:
        return existing
    if run.status != "active":
        return None
    owner = await db.get(User, run.requested_by)
    if owner is None or not owner.is_active:
        return None
    plan = await db.get(Plan, run.plan_id)
    if plan is None:
        return None
    group_id = await scope_group_for_plan(db, plan)
    if group_id is None:
        return None
    from app.workflows.security import require_scope

    try:
        async with db.begin_nested():
            # The requester must be a member of the department's workflow
            # scope; otherwise the unified run could never dispatch, so
            # leave the activation on the legacy path instead of
            # mirroring a stuck run.
            try:
                await require_scope(db, owner, group_id)
            except AppError:
                return None
            definition = await ensure_system_definition(
                db, scope_id=group_id, owner_id=owner.id, origin="continuity", origin_ref=TEMPLATE_REF,
                name=TEMPLATE_NAME, description=TEMPLATE_DESCRIPTION, spec=continuity_spec())
            if not definition.enabled:
                return None  # a department manager turned the unified path off
            service = WorkflowService(db, owner, cipher, AuditContext(owner.id, owner.email, None, None))
            return await service.trigger(group_id, definition.id, {"continuity_run_id": str(run.id)},
                                         f"continuity:{run.id}", origin="continuity", origin_ref=str(run.id))
    except AppError as exc:
        log.info("Continuity run %s not mirrored (%s)", run.id, exc.code)
        return None


async def mirror_activation(db, cipher, run: Run, plan: Plan) -> WorkflowRun | None:
    """Best effort right after an activation is approved; never breaks it."""
    try:
        return await mirror_continuity_run(db, cipher, run)
    except Exception as exc:  # never break continuity because the runtime could not record the run
        log.info("Continuity run %s not mirrored yet (%s)", run.id, type(exc).__name__)
        return None


_UNMIRRORABLE: set[uuid.UUID] = set()


async def mirror_pending_continuity_runs(sessionmaker, cipher, *, limit: int = 25) -> int:
    """Attach active activations that have no unified run yet (engine sweep)."""
    count = 0
    async with sessionmaker() as db:
        query = (select(Run).outerjoin(WorkflowRun, and_(
            WorkflowRun.origin == "continuity", WorkflowRun.origin_ref == Run.id.cast(String),
        )).where(WorkflowRun.id.is_(None), Run.status == "active"))
        if _UNMIRRORABLE:
            query = query.where(Run.id.not_in(list(_UNMIRRORABLE)[-5000:]))
        for run in (await db.scalars(query.order_by(Run.created_at).limit(limit))).all():
            try:
                async with db.begin_nested():
                    if await mirror_continuity_run(db, cipher, run) is not None:
                        count += 1
                    else:
                        _UNMIRRORABLE.add(run.id)
            except AppError as exc:
                _UNMIRRORABLE.add(run.id)
                log.info("Continuity run %s not mirrored (%s)", run.id, exc.code)
        await db.commit()
    return count


async def on_workflow_cancelled(db, cipher, run: WorkflowRun) -> None:
    """Stop at the next step boundary: cancelling the unified run suspends the activation."""
    try:
        continuity_id = uuid.UUID(str(run.origin_ref))
    except ValueError:
        return
    row = await db.scalar(select(Run).where(Run.id == continuity_id).with_for_update())
    if row is None or row.status != "active":
        return
    row.status = "suspended"


def _attempt_spec(ctx: ActionContext, run: Run, task: Task) -> ActionSpec:
    return ActionSpec("continuity.run", ctx.platform_scope_id,
                      {"continuity_run_id": str(run.id), "task_id": str(task.id),
                       "step_key": task.step_key, "workflow_run_id": str(ctx.run_id)},
                      subject=f"continuity_run:{run.id}")


class ContinuityExecuteHandler:
    name = "continuity.execute"
    label = "Continuity activation (steps dispatched through execute_once, verified by read-back)"
    module = "08"
    permission = None  # the activation requester acts; execute_once checks `workflow.access` on the department

    async def _load(self, ctx: ActionContext) -> Run | None:
        try:
            run_id = uuid.UUID(str(ctx.values.get("continuity_run_id")))
        except ValueError:
            return None
        row = await ctx.db.scalar(select(Run).where(Run.id == run_id)
                                  .execution_options(populate_existing=True))
        # Owner binding: a workflow can only carry the requester's own activation.
        return row if row is not None and row.requested_by == ctx.principal.user_id else None

    async def _tasks(self, ctx: ActionContext, run: Run) -> tuple[list[dict[str, Any]], list[Task]]:
        plan = await ctx.db.get(Plan, run.plan_id)
        steps = list(plan.data.get("steps", [])) if plan is not None else []
        tasks = list(await ctx.db.scalars(
            select(Task).where(Task.run_id == run.id).order_by(Task.step_key)))
        return steps, tasks

    async def _dispatch_task(self, ctx: ActionContext, run: Run, task: Task) -> ActionOutcome:
        from app.continuity import service as continuity_service
        from app.continuity.access import scope_for

        user = await ctx.db.get(User, ctx.principal.user_id)
        if user is None:
            return ActionOutcome("failed", {"continuity_run_id": str(run.id)}, "continuity_actor_missing")
        key = f"continuity:{run.id}:task:{task.id}:dispatch"
        spec = _attempt_spec(ctx, run, task)
        task_id = task.id
        run_id = run.id
        tenant_id = run.tenant_id

        async def dispatch() -> str:
            scope = await scope_for(ctx.db, tenant_id, user, write=True)
            audit_ctx = AuditContext(user.id, user.email, None, None)
            await continuity_service.dispatch(ctx.db, scope, task_id, audit_ctx)
            return f"{task_id}:dispatched"

        async def verify(ref: str) -> VerifiedResult:
            status = await ctx.db.scalar(select(Task.status).where(Task.id == task_id)
                                         .execution_options(populate_existing=True))
            return VerifiedResult(status == "dispatched",
                                  {"task_id": str(task_id), "status": status, "ref": ref})

        result = await execute_once(ctx.db, ctx.principal, key, spec, duty=DUTY,
                                    dispatch=dispatch, verify=verify)
        if result.status == "claimed" and result.replayed:
            return ActionOutcome("waiting", {"continuity_run_id": str(run_id)}, poll_seconds=5)
        if result.status in {"dispatched", "verified"}:
            ctx.note("continuity.task_dispatched", continuity_run_id=str(run_id), task_id=str(task_id))
            return ActionOutcome("more", {"continuity_run_id": str(run_id)})
        return ActionOutcome("failed", {"continuity_run_id": str(run_id)},
                             "continuity_dispatch_failed")

    async def run(self, ctx: ActionContext) -> ActionOutcome:
        row = await self._load(ctx)
        if row is None:
            return ActionOutcome("failed", failure_code="continuity_run_missing")
        out = {"continuity_run_id": str(row.id), "status": row.status}
        if row.status == "rejected":
            return ActionOutcome("failed", out, "continuity_rejected")
        if row.status == "closed":
            return ActionOutcome("done", out)
        if row.status == "suspended":
            return ActionOutcome("waiting", out, poll_seconds=30)
        if row.status != "active":
            return ActionOutcome("waiting", out, poll_seconds=15)
        steps, tasks = await self._tasks(ctx, row)
        by_key = {task.step_key: task for task in tasks}
        if tasks and all(task.status == "verified" for task in tasks):
            return ActionOutcome("done", out)
        verified_keys = {task.step_key for task in tasks if task.status == "verified"}
        for step in steps:
            task = by_key.get(step["key"])
            if task is None or task.status != "pending":
                continue
            if not set(step.get("depends_on", [])) <= verified_keys:
                continue
            if not step.get("workflow_id"):
                # Manual step: the owner completes it through the continuity
                # API (evidence + verification); the run waits here.
                return ActionOutcome("waiting", out, poll_seconds=15)
            return await self._dispatch_task(ctx, row, task)
        # No ready pending step: in-flight tasks (dispatched / evidence) are
        # being worked through the continuity API; wait for them.
        return ActionOutcome("waiting", out, poll_seconds=15)

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult:
        row = await self._load(ctx)
        if row is None:
            return VerifiedResult(False, {"continuity_run_id": output.get("continuity_run_id")})
        _, tasks = await self._tasks(ctx, row)
        ok = row.status in {"active", "closed"} and bool(tasks) and all(
            task.status == "verified" for task in tasks)
        return VerifiedResult(ok, {"continuity_run_id": str(row.id), "status": row.status,
                                   "tasks_verified": sum(1 for t in tasks if t.status == "verified"),
                                   "tasks_total": len(tasks)})


register(ContinuityExecuteHandler())
