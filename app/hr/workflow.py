"""Leave requests on the unified workflow runtime (Step AG, slices 2-3).

A leave request gets one workflow run (`origin='hr'`, `origin_ref=<request id>`):

    start -> approval (waits for the department manager's exact-action decision
          on the platform approval request) -> execute (a department manager,
          permission `hr.leave.approve`; balance update through `execute_once`
          with read-back) -> end

The API keeps working the same way: `POST /api/hr/leave/{id}/decide` decides
the approval and wakes the run; `POST /api/hr/leave/{id}/execute` uses the
same execution path (same idempotency key) and then hands the workflow step
to the executor, so the workflow records the verified result instead of
applying the balance a second time. A paused or stopped workflow run blocks
execution through either path.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.core.errors import ConflictError, PermissionDeniedError
from app.db.models import DepartmentManager, User
from app.db.workflow_engine_models import WorkflowRun, WorkflowStep
from app.hr.leave import LeaveBalance
from app.hr.models import HrLeaveBalance, HrLeaveRequest
from app.platform.access import principal_for
from app.platform.approvals import ActionSpec
from app.platform.events import audit
from app.platform.execution import VerifiedResult, execute_once
from app.platform.models import PlatformActionExecution, PlatformApprovalRequest
from app.services.audit import AuditContext
from app.workflows.actions import ActionContext, ActionOutcome, register
from app.workflows.definition import Definition, digest

log = logging.getLogger(__name__)

TEMPLATE_REF = 'hr.leave_approval'
TEMPLATE_NAME = 'Leave approvals'
TEMPLATE_DESCRIPTION = ('Leave requests: department-manager approval bound to the exact request, then the balance '
                        'update by a department manager, verified by read-back.')
DUTY = 'hr.leave.approve'
RUN_ORIGIN = 'hr'


def leave_subject(request_id) -> str:
    return f'leave_request:{request_id}'


def leave_spec() -> dict:
    values = {'leave_request_id': '$input.leave_request_id'}
    return Definition.model_validate({
        'source': 'template',
        'input_form': [{'name': 'leave_request_id', 'label': 'Leave request', 'type': 'text', 'required': True}],
        'nodes': [
            {'id': 'start', 'type': 'start', 'label': 'Leave requested', 'x': 80, 'y': 160},
            {'id': 'approval', 'type': 'action', 'label': 'Manager approval', 'x': 300, 'y': 160, 'risk': 'medium',
             'timeout_seconds': 60, 'max_attempts': 5,
             'config': {'handler': 'hr.leave_approval', 'auto': True, 'values': values}},
            {'id': 'execute', 'type': 'action', 'label': 'Apply balance update', 'x': 520, 'y': 160, 'risk': 'high',
             'timeout_seconds': 120, 'max_attempts': 5,
             'config': {'handler': 'hr.leave_execute', 'permission': DUTY, 'values': values}},
            {'id': 'end', 'type': 'end', 'label': 'Leave approved and applied', 'x': 740, 'y': 160},
        ],
        'edges': [{'source': 'start', 'target': 'approval'}, {'source': 'approval', 'target': 'execute'},
                  {'source': 'execute', 'target': 'end'}],
    }).model_dump(mode='json')


async def mirror_leave_request(db, request: HrLeaveRequest, cipher=None) -> WorkflowRun | None:
    """One unified run per leave request (idempotent: an existing run is returned).

    `cipher` is the app cipher (run payloads are sealed with it); when the caller
    has no request context it is built from settings, exactly as app.main does.
    Passing None used to reach `seal(None, ...)` and raise AttributeError.
    """
    from app.workflows.service import WorkflowService
    from app.workflows.unified import ensure_system_definition, origin_run

    existing = await origin_run(db, RUN_ORIGIN, request.id)
    if existing is not None:
        return existing
    employee = await db.get(User, request.employee_id)
    if employee is None or not employee.is_active:
        return None
    definition = await ensure_system_definition(
        db, scope_id=request.group_id, owner_id=employee.id, origin_ref=TEMPLATE_REF,
        name=TEMPLATE_NAME, description=TEMPLATE_DESCRIPTION, spec=leave_spec(), origin='system')
    if not definition.enabled:
        return None
    if cipher is None:
        from app.core.config import get_settings
        from app.core.crypto import CredentialCipher
        settings = get_settings()
        cipher = CredentialCipher(settings.master_key_bytes, settings.master_encryption_key_id)
    service = WorkflowService(db, employee, cipher, AuditContext(employee.id, employee.email, None, None))
    return await service.trigger(request.group_id, definition.id, {'leave_request_id': str(request.id)},
                                 f'hr.leave:{request.id}', origin=RUN_ORIGIN, origin_ref=str(request.id))


async def mirror_new_leave_request(db, request: HrLeaveRequest) -> None:
    """Best-effort mirror at request time; a failure leaves the API record usable (the run is retried)."""
    from app.core.config import get_settings

    if not get_settings().workflow_engine_on:
        return
    try:
        async with db.begin_nested():
            await mirror_leave_request(db, request)
        await db.commit()
    except Exception as exc:
        log.info('Leave request %s not mirrored yet (%s)', request.id, type(exc).__name__)


async def guard(db, request: HrLeaveRequest) -> WorkflowRun | None:
    """The unified run carrying this request, after refusing execution while it is paused or stopped."""
    from app.workflows.unified import origin_run

    run = await origin_run(db, RUN_ORIGIN, request.id)
    if run is None:
        return None
    if run.status == 'paused':
        raise ConflictError('This leave request is paused in the workflow runtime. Resume the run first.')
    if run.status in {'cancelled', 'failed'}:
        raise ConflictError('This leave request was stopped in the workflow runtime and cannot be executed.')
    return run


def _spec(request: HrLeaveRequest, platform_scope) -> ActionSpec:
    """The exact action the manager approves: this request, these dates, this many days."""
    return ActionSpec('hr.leave.approve', platform_scope,
                      {'leave_request_id': str(request.id), 'employee_id': str(request.employee_id),
                       'leave_type': request.leave_type, 'start_date': request.start_date.isoformat(),
                       'end_date': request.end_date.isoformat(), 'days': format(request.days, 'f')},
                      subject=leave_subject(request.id))


async def _key(db, request_id) -> str:
    """One execution per request; after a refused/failed attempt a new attempt gets a new key."""
    base = f'hr.leave:{request_id}'
    failed = await db.scalar(select(func.count()).select_from(PlatformActionExecution).where(
        PlatformActionExecution.idempotency_key.like(base + '%'), PlatformActionExecution.status == 'failed'))
    return base if not failed else f'{base}:retry{failed}'


async def recompute_balance(db, request: HrLeaveRequest) -> HrLeaveBalance:
    """Recompute the employee's balance cache for the request's year/type from leave records.

    Used = approved days in the year, including this request once it is applied;
    pending = pending days in the year (this request stops counting as pending the
    moment it is approved). Entitled/carried come from the stored cache row when
    one exists, else the policy entitlement with no carry.
    """
    from decimal import Decimal

    from app.hr.models import HrLeavePolicy

    year = request.start_date.year
    rows = (await db.scalars(select(HrLeaveRequest).where(
        HrLeaveRequest.group_id == request.group_id, HrLeaveRequest.employee_id == request.employee_id,
        HrLeaveRequest.leave_type == request.leave_type,
        HrLeaveRequest.start_date >= datetime(year, 1, 1).date(),
        HrLeaveRequest.start_date < datetime(year + 1, 1, 1).date(),
        HrLeaveRequest.status.in_(('approved', 'pending'))))).all()
    used = sum((r.days for r in rows if r.status == 'approved'), Decimal('0'))
    pending = sum((r.days for r in rows if r.status == 'pending'), Decimal('0'))
    cache = await db.scalar(select(HrLeaveBalance).where(
        HrLeaveBalance.group_id == request.group_id, HrLeaveBalance.employee_id == request.employee_id,
        HrLeaveBalance.leave_type == request.leave_type, HrLeaveBalance.year == year))
    if cache is None:
        policy = await db.scalar(select(HrLeavePolicy).where(
            HrLeavePolicy.group_id == request.group_id, HrLeavePolicy.leave_type == request.leave_type))
        entitled = policy.entitlement_days if policy is not None else Decimal('0')
        cache = HrLeaveBalance(group_id=request.group_id, employee_id=request.employee_id,
                               leave_type=request.leave_type, year=year, entitled=entitled, carried=Decimal('0'))
        db.add(cache)
    cache.used, cache.pending, cache.updated_at = used, pending, datetime.now(UTC)
    return cache


async def execute_leave(db, actor: User, request: HrLeaveRequest, run: WorkflowRun | None) -> HrLeaveRequest:
    """Apply an approved leave request through execute_once: mark it applied and refresh the balance cache.

    The linked platform approval is consumed exactly once; a replay re-verifies
    instead of applying the balance again.
    """
    from app.workflows.action_runtime import platform_scope_for

    platform_scope = await platform_scope_for(db, request.group_id)
    if platform_scope is None:
        raise ConflictError('The department has no platform scope.')
    request_id = request.id

    async def dispatch() -> str:
        row = await db.get(HrLeaveRequest, request_id, populate_existing=True)
        if row.applied_at is not None:
            return str(request_id)
        if row.status != 'approved':
            raise ConflictError('Leave request is not approved.')
        row.applied_at = datetime.now(UTC)
        await recompute_balance(db, row)
        return str(request_id)

    async def verify(ref: str) -> VerifiedResult:
        row = await db.scalar(select(HrLeaveRequest).where(HrLeaveRequest.id == request_id)
                              .execution_options(populate_existing=True))
        ok = row is not None and row.status == 'approved' and row.applied_at is not None
        return VerifiedResult(ok, {'leave_request_id': ref, 'status': row.status if row else None,
                                   'applied_at': row.applied_at.isoformat() if row and row.applied_at else None})

    result = await execute_once(db, principal_for(actor, channel='workflow'), await _key(db, request_id),
                                _spec(request, platform_scope), duty=DUTY, dispatch=dispatch, verify=verify,
                                approval_id=request.approval_id)
    if result.status == 'failed':
        raise ConflictError('The leave could not be applied. Nothing was changed; see the audit log.')
    if result.status == 'claimed':
        raise ConflictError('This leave request is being applied right now.')
    return await db.get(HrLeaveRequest, request_id, populate_existing=True)


async def attach_executor(db, run: WorkflowRun, actor: User) -> None:
    """After an application through the API, the workflow's execute step is taken by the same person so the
    engine records (and re-verifies) the result instead of waiting for someone to apply it."""
    from app.workflows.events import event

    step = await db.scalar(select(WorkflowStep).where(WorkflowStep.run_id == run.id, WorkflowStep.node_id == 'execute')
                           .with_for_update())
    if step is None or step.status in {'completed', 'cancelled', 'skipped', 'failed'} or step.acting_user_id:
        return
    step.acting_user_id, step.acting_auth_version = actor.id, actor.auth_version
    if step.status in {'action', 'waiting'}:
        step.status, step.available_at = 'ready', datetime.now(UTC)
    run.wake_at = datetime.now(UTC)
    event(db, run, 'action.actor_assigned', node_id='execute', actor_id=actor.id, detail={'via': 'hr_api'})


async def _is_department_manager(db, group_id, user_id) -> bool:
    return await db.scalar(select(DepartmentManager).where(
        DepartmentManager.group_id == group_id, DepartmentManager.manager_user_id == user_id)) is not None


async def _request(ctx: ActionContext) -> HrLeaveRequest | None:
    try:
        request_id = uuid.UUID(str(ctx.values.get('leave_request_id')))
    except ValueError:
        return None
    row = await ctx.db.scalar(select(HrLeaveRequest).where(HrLeaveRequest.id == request_id)
                              .execution_options(populate_existing=True))
    return row if row is not None and row.group_id == ctx.group_id else None  # department binding


async def _sync_decision(ctx: ActionContext, row: HrLeaveRequest) -> None:
    """Mirror the platform approval's decision onto the request row, so the run progresses no matter which
    endpoint the manager used to decide."""
    if row.approval_id is None or row.status != 'pending':
        return
    approval = await ctx.db.get(PlatformApprovalRequest, row.approval_id, populate_existing=True)
    if approval is None:
        return
    if approval.status == 'approved':
        row.status, row.decided_by, row.decided_at = 'approved', approval.decided_by, approval.decided_at
    elif approval.status == 'rejected':
        row.status, row.decided_by, row.decided_at = 'rejected', approval.decided_by, approval.decided_at


class LeaveApprovalHandler:
    name = 'hr.leave_approval'
    label = 'Department-manager approval of a leave request'
    module = 'hr'
    permission = None

    async def run(self, ctx: ActionContext) -> ActionOutcome:
        row = await _request(ctx)
        if row is None:
            return ActionOutcome('failed', failure_code='leave_request_missing')
        await _sync_decision(ctx, row)
        out = {'leave_request_id': str(row.id), 'status': row.status}
        if row.status == 'approved':
            return ActionOutcome('done', out | {'approver_id': str(row.decided_by) if row.decided_by else None})
        if row.status == 'rejected':
            return ActionOutcome('failed', out, 'approval_rejected')
        if row.status == 'cancelled':
            return ActionOutcome('failed', out, 'leave_cancelled')
        if row.approval_id is not None:
            approval = await ctx.db.get(PlatformApprovalRequest, row.approval_id, populate_existing=True)
            if approval is not None and approval.status == 'expired':
                return ActionOutcome('failed', out, 'approval_expired')
        return ActionOutcome('waiting', out, poll_seconds=30)

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult:
        row = await _request(ctx)
        ok = (row is not None and row.status == 'approved' and row.decided_by is not None
              and row.decided_by != row.employee_id)
        return VerifiedResult(ok, {'leave_request_id': output.get('leave_request_id'),
                                   'status': row.status if row else None})


class LeaveExecuteHandler:
    name = 'hr.leave_execute'
    label = 'Apply an approved leave request (execute_once, read-back)'
    module = 'hr'
    permission = DUTY

    async def run(self, ctx: ActionContext) -> ActionOutcome:
        row = await _request(ctx)
        if row is None:
            return ActionOutcome('failed', failure_code='leave_request_missing')
        out = {'leave_request_id': str(row.id)}
        actor = await ctx.db.get(User, ctx.principal.user_id)
        if row.applied_at is not None:
            pass  # Applied through the API: confirm through the same idempotency key (a replay re-verifies).
        elif row.status != 'approved':
            return ActionOutcome('failed', out | {'status': row.status}, 'leave_not_approved')
        if actor is None or not await _is_department_manager(ctx.db, row.group_id, actor.id):
            return ActionOutcome('failed', out, 'actor_not_permitted')
        run = await ctx.db.get(WorkflowRun, ctx.run_id)
        try:
            await execute_leave(ctx.db, actor, row, run)
        except PermissionDeniedError:
            await ctx.db.rollback()
            return ActionOutcome('failed', out, 'actor_not_permitted')  # hand the step to someone allowed
        except Exception as exc:  # noqa: BLE001 - the engine records the failure code, it never leaks the stack
            await ctx.db.rollback()
            return ActionOutcome('failed', out, getattr(exc, 'code', type(exc).__name__)[:60])
        return ActionOutcome('done', out | {'status': 'approved', 'applied': True})

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult:
        row = await _request(ctx)
        execution = await ctx.db.scalar(select(PlatformActionExecution).where(
            PlatformActionExecution.subject == leave_subject(output.get('leave_request_id')),
            PlatformActionExecution.status == 'verified'))
        ok = row is not None and row.status == 'approved' and row.applied_at is not None and execution is not None
        return VerifiedResult(ok, {'leave_request_id': output.get('leave_request_id'),
                                   'execution_id': str(execution.id) if execution else None})


register(LeaveApprovalHandler())
register(LeaveExecuteHandler())
