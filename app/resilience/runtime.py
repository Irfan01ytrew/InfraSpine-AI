"""Step AH slice 4 — recovery plans as unified-engine workflow runs.

A plan built by :mod:`app.resilience.plans` becomes one workflow run:

* the definition is a code template pinned by digest (``origin='system'``,
  ``origin_ref='resilience.recovery_plan:<plan id>'`` — one definition per
  plan, the same mirror pattern Step E uses for automation rules and
  governance proposals);
* the run carries ``origin='resilience'`` and ``origin_ref=<plan id>`` so
  every plan is listed, paused, resumed and verified in one place;
* every approval-bound step first waits on its platform approval (exact
  action hash, expiring, single use, separation of duties — the approver
  can never be the plan's proposer); sensitive steps (supplier switch)
  need ``platform.manage`` (owner / administrators only);
* every execute step runs through :func:`app.platform.execution.execute_once`
  with a per-step idempotency key and read-back verification against the
  audit trail, so a step can never execute twice and a crash resumes by
  verifying again, never by dispatching again.

What "performing" a step means: recovery steps are human-mediated actions
(ask a backup to cover, reassign, switch a supplier, use a workaround,
escalate). Performing one records *who* did it and *when*, with the
approval bound to the exact action, in the audit trail and the run's
hash-chained evidence. The engine verifies the record by reading it back
from the audit log. Nothing here moves money, restores data, switches a
supplier in any external system, or grants access — those effects happen
outside the system and are attested by the performer taking the step.

Pausing or stopping the run blocks execution through every path (the
engine refuses to dispatch a step of a run that is not ``running``, and
:func:`guard` refuses API execution while the run is paused, cancelled
or failed). Resuming continues from the last verified step, exactly like
every other unified-engine run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.errors import ConflictError, NotFoundError
from app.db.models import AuditLog, User
from app.db.workflow_engine_models import WorkflowRun, WorkflowStep, WorkflowVersion
from app.platform.access import principal_for
from app.platform.approvals import ActionSpec, request_approval
from app.resilience.audit import publish_resilience_event, record_resilience_event
from app.platform.execution import VerifiedResult, execute_once
from app.platform.models import PlatformApprovalRequest
from app.resilience.plans import (
    STEP_DUTY,
    plan_fingerprint,
    plan_progress_from_steps,
    plan_spec,
    step_action_params,
    step_subject,
)
from app.services.audit import AuditContext
from app.workflows.actions import ActionContext, ActionOutcome, register
from app.workflows.definition import Definition

TEMPLATE_REF_PREFIX = "resilience.recovery_plan"
ORIGIN = "resilience"


def _step_key(plan_id, step_id):
    return f"{plan_id}:{step_id}"


async def _platform_scope_id(db, scope_id) -> uuid.UUID:
    """The platform scope id for a department (``platform_scope_for`` already returns the id)."""
    from app.workflows.action_runtime import platform_scope_for

    platform_scope_id = await platform_scope_for(db, scope_id)
    if platform_scope_id is None:
        raise ConflictError("The department has no platform scope.")
    return platform_scope_id


async def _plan_from_version(db, run):
    version = await db.get(WorkflowVersion, run.version_id)
    if version is None:
        raise NotFoundError("The plan's workflow version is missing.")
    spec = Definition.model_validate(version.spec)
    steps = []
    for node in spec.nodes:
        if node.type != "action" or not node.id.startswith("exec-"):
            continue
        values = dict(node.config.values or {})
        steps.append(
            {
                "id": values.get("step_id") or node.id[5:],
                "kind": values.get("kind") or "manual_workaround",
                "action": values.get("action") or node.label,
                "covers": list(values.get("covers") or []),
                "owner": values.get("owner"),
                "eta_days": values.get("eta_days"),
                "requires_approval": bool(values.get("requires_approval")),
                "sensitive": bool(values.get("sensitive")),
                "approval_permission": None,
                "status": "pending",
            }
        )
    steps.sort(key=lambda item: item["id"])
    return {"id": run.origin_ref, "event": {}, "severity": None, "money_at_risk": {}, "steps": steps}


async def plan_run(db, plan_id):
    """The unified run carrying a recovery plan (newest first)."""
    from app.workflows.unified import origin_run

    return await origin_run(db, ORIGIN, plan_id)


async def guard(db, plan_id):
    """The unified run carrying this plan, after refusing execution while it is paused or stopped."""
    run = await plan_run(db, plan_id)
    if run is None:
        return None
    if run.status == "paused":
        raise ConflictError("This recovery plan is paused in the workflow runtime. Resume the run first.")
    if run.status in {"cancelled", "failed"}:
        raise ConflictError("This recovery plan was stopped in the workflow runtime and cannot be executed.")
    return run


async def start_plan(db, user, cipher, ctx, *, scope_id, plan):
    """Start a recovery plan as a unified-engine workflow run.

    Requests the platform approvals for the approval-bound steps (exact
    action hash, expiring, single use) *after* triggering the run, so the
    approval params can carry the run id — deciding the approval then wakes
    the waiting approval step immediately. Returns
    ``(run, approvals)`` where approvals maps step id -> approval id.
    """
    from app.workflows.service import WorkflowService
    from app.workflows.unified import ensure_system_definition

    spec = plan_spec(plan)
    Definition.model_validate(spec)  # fail fast on a malformed plan spec
    existing = await plan_run(db, plan["id"])
    if existing is not None:
        # Starting the same plan twice returns the original run and its
        # approvals instead of duplicating them (same idempotency key).
        approvals = {}
        for step in plan["steps"]:
            if not step["requires_approval"]:
                continue
            row = await _approval_for_step(db, plan["id"], step["id"])
            if row is not None:
                approvals[step["id"]] = str(row.id)
        return existing, approvals
    definition = await ensure_system_definition(
        db,
        scope_id=scope_id,
        owner_id=user.id,
        origin_ref=f"{TEMPLATE_REF_PREFIX}:{plan['id']}",
        name=f"Recovery plan {plan['id']}",
        description="Company Resilience AI recovery plan: approval-bound steps on the unified workflow runtime.",
        spec=spec,
    )
    if not definition.enabled:
        raise ConflictError("The recovery-plan workflow is disabled in this department.")
    service = WorkflowService(db, user, cipher, ctx)
    run = await service.trigger(
        scope_id,
        definition.id,
        {"plan_id": plan["id"]},
        f"resilience-plan:{plan['id']}",
        origin=ORIGIN,
        origin_ref=plan["id"],
    )
    platform_scope = await _platform_scope_id(db, scope_id)
    principal = principal_for(user, channel="web")
    approvals = {}
    for step in plan["steps"]:
        if not step["requires_approval"]:
            continue
        request = await request_approval(
            db,
            principal,
            ActionSpec(
                "resilience.plan_step",
                platform_scope,
                step_action_params(plan, step, run_id=run.id),
                subject=step_subject(plan["id"], step["id"]),
            ),
            required_permission=step["approval_permission"],
        )
        approvals[step["id"]] = str(request.id)
    await publish_resilience_event(
        db,
        principal,
        "plan_started",
        platform_scope,
        {"plan_id": plan["id"], "run_id": str(run.id), "steps": len(plan["steps"]),
         "fingerprint": plan_fingerprint(plan)},
    )
    record_resilience_event(
        db,
        principal,
        "plan_started",
        target=plan["id"],
        details={"run_id": str(run.id), "steps": len(plan["steps"]), "severity": plan.get("severity"),
                 "scope_id": str(scope_id)},
    )
    return run, approvals


async def plan_progress(db, scope_id, plan_id):
    """Progress of a recovery plan: run status + per-step detail."""
    from app.workflows.service import WorkflowService

    scope_id = uuid.UUID(str(scope_id))  # callers may pass the id as text
    run = await plan_run(db, plan_id)
    if run is None or run.scope_id != scope_id:
        raise NotFoundError("Recovery plan not found in this scope.")
    plan = await _plan_from_version(db, run)
    rows = (await db.scalars(select(WorkflowStep).where(WorkflowStep.run_id == run.id))).all()
    step_rows = [
        {
            "node_id": row.node_id,
            "status": row.status,
            "acting_user_id": str(row.acting_user_id) if row.acting_user_id else None,
            "verified_at": row.verified_at.isoformat() if row.verified_at else None,
        }
        for row in rows
    ]
    progress = plan_progress_from_steps(plan, step_rows)
    progress["run_id"] = str(run.id)
    progress["run_status"] = run.status
    progress["started_at"] = run.started_at.isoformat() if run.started_at else None
    progress["finished_at"] = run.finished_at.isoformat() if run.finished_at else None
    progress["failure_code"] = run.failure_code
    return progress


async def control_plan(db, user, cipher, ctx, scope_id, plan_id, command):
    """Pause / resume / stop a recovery plan through the workflow runtime."""
    from app.workflows.service import WorkflowService

    scope_id = uuid.UUID(str(scope_id))  # callers may pass the id as text
    run = await plan_run(db, plan_id)
    if run is None or run.scope_id != scope_id:
        raise NotFoundError("Recovery plan not found in this scope.")
    service = WorkflowService(db, user, cipher, ctx)
    run = await service.control(scope_id, run.id, command)
    platform_scope = await _platform_scope_id(db, scope_id)
    principal = principal_for(user, channel="web")
    await publish_resilience_event(
        db,
        principal,
        f"plan_{command}",
        platform_scope,
        {"plan_id": plan_id, "run_id": str(run.id), "run_status": run.status},
    )
    record_resilience_event(
        db,
        principal,
        f"plan_{command}",
        target=plan_id,
        details={"run_id": str(run.id), "run_status": run.status, "scope_id": str(scope_id)},
    )
    return run


async def _approval_for_step(db, plan_id, step_id):
    return await db.scalar(
        select(PlatformApprovalRequest)
        .where(PlatformApprovalRequest.subject == step_subject(plan_id, step_id))
        .order_by(PlatformApprovalRequest.created_at.desc())
        .limit(1)
    )


class StepApprovalHandler:
    """Poll the step's platform approval: waits until it is decided."""

    name = "resilience.step_approval"
    label = "Approval of one recovery-plan step (exact action)"
    module = "AH"
    permission = None

    async def run(self, ctx: ActionContext) -> ActionOutcome:
        plan_id = str(ctx.values.get("plan_id") or "")
        step_id = str(ctx.values.get("step_id") or "")
        row = await _approval_for_step(ctx.db, plan_id, step_id)
        out = {"plan_id": plan_id, "step_id": step_id}
        if row is None:
            return ActionOutcome("waiting", out, poll_seconds=30)
        out["approval_id"] = str(row.id)
        out["status"] = row.status
        if row.status == "approved":
            return ActionOutcome("done", out | {"approver_id": str(row.decided_by) if row.decided_by else None})
        if row.status == "rejected":
            return ActionOutcome("failed", out, "approval_rejected")
        if row.status in {"expired", "consumed"} or row.expires_at <= datetime.now(UTC):
            return ActionOutcome("failed", out, "approval_expired")
        return ActionOutcome("waiting", out, poll_seconds=30)

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult:
        row = await _approval_for_step(ctx.db, str(output.get("plan_id") or ""), str(output.get("step_id") or ""))
        ok = (
            row is not None
            and row.status in {"approved", "consumed"}
            and row.decided_by is not None
            and row.decided_by != row.requested_by
        )
        return VerifiedResult(
            ok,
            {
                "plan_id": output.get("plan_id"),
                "step_id": output.get("step_id"),
                "status": row.status if row else None,
                "approver_id": str(row.decided_by) if row and row.decided_by else None,
            },
        )


class StepExecuteHandler:
    """Perform one recovery-plan step through execute_once.

    The dispatch records the performance (actor + time) in the audit trail
    and the outbox; the verification reads that record back from the audit
    log. A replay of the same idempotency key re-verifies instead of
    performing the step again.
    """

    name = "resilience.step_execute"
    label = "Perform one recovery-plan step (execute_once, read-back)"
    module = "AH"
    permission = STEP_DUTY

    async def run(self, ctx: ActionContext) -> ActionOutcome:
        plan_id = str(ctx.values.get("plan_id") or "")
        step_id = str(ctx.values.get("step_id") or "")
        kind = str(ctx.values.get("kind") or "")
        out = {"plan_id": plan_id, "step_id": step_id, "kind": kind}
        run = await ctx.db.get(WorkflowRun, ctx.run_id)
        if run is None:
            return ActionOutcome("failed", out, "plan_run_missing")
        plan = await _plan_from_version(ctx.db, run)
        step = next((item for item in plan["steps"] if item["id"] == step_id), None)
        if step is None:
            return ActionOutcome("failed", out, "plan_step_missing")
        principal = ctx.principal
        key = f"resilience-plan:{plan_id}:step:{step_id}"

        async def dispatch() -> str:
            record_resilience_event(
                ctx.db,
                principal,
                "plan_step_performed",
                target=step_subject(plan_id, step_id),
                details={
                    "plan_id": plan_id,
                    "step_id": step_id,
                    "kind": kind,
                    "actor_id": str(principal.user_id),
                    "scope_id": str(ctx.platform_scope_id),
                },
            )
            await publish_resilience_event(
                ctx.db,
                principal,
                "plan_step_performed",
                ctx.platform_scope_id,
                {"plan_id": plan_id, "step_id": step_id, "kind": kind,
                 "actor_id": str(principal.user_id)},
            )
            ctx.note("resilience.step_performed", step_id=step_id, step_kind=kind)
            return _step_key(plan_id, step_id)

        async def verify(ref: str) -> VerifiedResult:
            row = await ctx.db.scalar(
                select(AuditLog)
                .where(
                    AuditLog.action == "resilience.plan_step_performed",
                    AuditLog.target == step_subject(plan_id, step_id),
                )
                .order_by(AuditLog.id.desc())
                .execution_options(populate_existing=True)
            )
            ok = row is not None
            return VerifiedResult(
                ok,
                {"plan_id": plan_id, "step_id": step_id, "ref": ref,
                 "performed_at": row.created_at.isoformat() if row else None},
            )

        result = await execute_once(
            ctx.db,
            principal,
            key,
            ActionSpec(
                "resilience.plan_step",
                ctx.platform_scope_id,
                step_action_params(plan, step),
                subject=step_subject(plan_id, step_id),
            ),
            duty=STEP_DUTY,
            dispatch=dispatch,
            verify=verify,
        )
        if result.status == "failed":
            return ActionOutcome("failed", out | {"execution_status": result.status}, "step_execution_failed")
        if result.status == "claimed":
            return ActionOutcome("failed", out | {"execution_status": result.status}, "step_execution_claimed")
        return ActionOutcome("done", out | {"execution_id": str(result.execution_id), "replayed": result.replayed})

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult:
        row = await ctx.db.scalar(
            select(AuditLog)
            .where(
                AuditLog.action == "resilience.plan_step_performed",
                AuditLog.target == step_subject(str(output.get("plan_id") or ""), str(output.get("step_id") or "")),
            )
            .order_by(AuditLog.id.desc())
            .execution_options(populate_existing=True)
        )
        ok = row is not None
        return VerifiedResult(
            ok,
            {
                "plan_id": output.get("plan_id"),
                "step_id": output.get("step_id"),
                "performed_at": row.created_at.isoformat() if row else None,
            },
        )


register(StepApprovalHandler())
register(StepExecuteHandler())
