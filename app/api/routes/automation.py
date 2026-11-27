"""Department-scoped workflow definitions, approval queues and internal tasks."""

import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, audit_context, get_current_user
from app.core.errors import ConflictError, NotFoundError
from app.db.models import (
    ROLE_ADMIN,
    ROLE_MANAGER,
    AccessGroup,
    AutomationApproval,
    AutomationRun,
    AutomationWorkflow,
    DepartmentManager,
)
from app.schemas.automation import (
    WorkflowDecisionRequest,
    WorkflowDefinitionRequest,
    WorkflowDryRunOut,
    WorkflowListResponse,
    WorkflowNotificationListResponse,
    WorkflowNotificationOut,
    WorkflowOut,
    WorkflowRunListResponse,
    WorkflowRunOut,
    WorkflowRunRequest,
    WorkflowTaskListResponse,
    WorkflowTaskOut,
    WorkflowVersionRequest,
)
from app.services import automation

router = APIRouter(prefix="/api/automation", tags=["automation"], dependencies=[Depends(get_current_user)])


async def _workflow_out(db: DbSession, user: CurrentUser, workflow: AutomationWorkflow) -> WorkflowOut:
    group = await db.get(AccessGroup, workflow.department_group_id)
    if group is None:
        raise ConflictError("The workflow department no longer exists.")
    config = workflow.action_config
    try:
        if workflow.action_type in {"create_task", "assign_personal_task"}:
            assignee_id = uuid.UUID(config["assignee_user_id"])
            recipient_id = None
            task_title = config["title"]
            task_body = config["body"]
            due_days = config.get("due_days")
            notification_title = notification_body = None
        elif workflow.action_type == "notify_member":
            recipient_id = uuid.UUID(config["recipient_user_id"])
            assignee_id = None
            task_title = task_body = due_days = None
            notification_title = config["title"]
            notification_body = config["body"]
        else:
            raise ValueError("unknown workflow action")
    except (KeyError, TypeError, ValueError):
        raise ConflictError("The workflow action configuration is invalid.") from None
    manager = user.role == ROLE_ADMIN or (
        user.role == ROLE_MANAGER and await db.get(DepartmentManager, (group.id, user.id)) is not None
    )
    return WorkflowOut(
        id=workflow.id,
        definition_key=workflow.definition_key,
        version=workflow.version,
        name=workflow.name,
        department_group_id=group.id,
        department_name=group.name,
        trigger_type=workflow.trigger_type,
        interval_minutes=workflow.interval_minutes,
        action_type=workflow.action_type,
        assignee_user_id=assignee_id,
        task_title=task_title,
        task_body=task_body,
        due_days=due_days,
        notification_recipient_user_id=recipient_id,
        notification_title=notification_title,
        notification_body=notification_body,
        enabled=workflow.enabled,
        next_run_at=workflow.next_run_at,
        created_at=workflow.created_at,
        can_manage=bool(manager),
    )


async def _run_out(db: DbSession, user: CurrentUser, run: AutomationRun) -> WorkflowRunOut:
    workflow = await db.get(AutomationWorkflow, run.workflow_id)
    if workflow is None:
        raise ConflictError("The workflow definition for this run is unavailable.")
    approval = (
        await db.execute(select(AutomationApproval).where(AutomationApproval.run_id == run.id))
    ).scalar_one_or_none()
    manager = user.role == ROLE_ADMIN or (
        user.role == ROLE_MANAGER
        and await db.get(DepartmentManager, (run.department_group_id, user.id)) is not None
    )
    return WorkflowRunOut(
        id=run.id,
        workflow_id=run.workflow_id,
        workflow_name=workflow.name,
        requested_by_user_id=run.requester_user_id,
        department_group_id=run.department_group_id,
        status=run.status,
        attempts=run.attempts,
        last_error_code=run.last_error_code,
        created_at=run.created_at,
        completed_at=run.completed_at,
        approval_decision=approval.decision if approval else None,
        approver_user_id=approval.approver_user_id if approval else None,
        approval_rationale=approval.rationale if approval else None,
        approval_decided_at=approval.decided_at if approval else None,
        can_approve=bool(manager and run.requester_user_id != user.id and run.status == "pending_approval"),
        can_cancel=bool(
            (run.requester_user_id == user.id or manager) and run.status in {"pending_approval", "queued"}
        ),
        can_retry=bool(manager and run.status == "failed"),
    )


@router.get("/workflows", response_model=WorkflowListResponse)
async def list_workflows(db: DbSession, user: CurrentUser) -> WorkflowListResponse:
    rows = await automation.list_workflows(db, user)
    return WorkflowListResponse(workflows=[await _workflow_out(db, user, row) for row in rows])


@router.post("/workflows", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    payload: WorkflowDefinitionRequest, db: DbSession, user: CurrentUser, request: Request
) -> WorkflowOut:
    row = await automation.create_workflow(
        db,
        user,
        audit_context(request, user),
        name=payload.name,
        department_group_id=payload.department_group_id,
        trigger_type=payload.trigger_type,
        interval_minutes=payload.interval_minutes,
        action_type=payload.action_type,
        assignee_user_id=payload.assignee_user_id,
        title=payload.task_title,
        body=payload.task_body,
        due_days=payload.due_days,
        notification_recipient_user_id=payload.notification_recipient_user_id,
        notification_title=payload.notification_title,
        notification_body=payload.notification_body,
    )
    return await _workflow_out(db, user, row)


@router.put("/workflows/{workflow_id}", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
async def version_workflow(
    workflow_id: uuid.UUID,
    payload: WorkflowVersionRequest,
    db: DbSession,
    user: CurrentUser,
    request: Request,
) -> WorkflowOut:
    row = await automation.create_workflow_version(
        db,
        user,
        audit_context(request, user),
        workflow_id,
        expected_version=payload.expected_version,
        name=payload.name,
        trigger_type=payload.trigger_type,
        interval_minutes=payload.interval_minutes,
        action_type=payload.action_type,
        assignee_user_id=payload.assignee_user_id,
        title=payload.task_title,
        body=payload.task_body,
        due_days=payload.due_days,
        notification_recipient_user_id=payload.notification_recipient_user_id,
        notification_title=payload.notification_title,
        notification_body=payload.notification_body,
    )
    return await _workflow_out(db, user, row)


@router.post("/workflows/{workflow_id}/disable", status_code=status.HTTP_204_NO_CONTENT)
async def disable_workflow(
    workflow_id: uuid.UUID, db: DbSession, user: CurrentUser, request: Request
) -> None:
    await automation.disable_workflow(db, user, audit_context(request, user), workflow_id)


@router.post("/workflows/{workflow_id}/dry-run", response_model=WorkflowDryRunOut)
async def dry_run_workflow(
    workflow_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> WorkflowDryRunOut:
    """Return a permission-checked workflow plan without persisting a run or task."""
    return WorkflowDryRunOut.model_validate(await automation.dry_run_workflow(db, user, workflow_id))


@router.post("/workflows/{workflow_id}/runs", response_model=WorkflowRunOut, status_code=status.HTTP_202_ACCEPTED)
async def request_run(
    workflow_id: uuid.UUID,
    payload: WorkflowRunRequest,
    db: DbSession,
    user: CurrentUser,
    request: Request,
) -> WorkflowRunOut:
    run = await automation.request_run(
        db, user, audit_context(request, user), workflow_id, payload.idempotency_key
    )
    return await _run_out(db, user, run)


@router.get("/runs", response_model=WorkflowRunListResponse)
async def list_runs(db: DbSession, user: CurrentUser) -> WorkflowRunListResponse:
    rows = await automation.list_runs(db, user)
    return WorkflowRunListResponse(runs=[await _run_out(db, user, row) for row in rows])


@router.post("/runs/{run_id}/decision", response_model=WorkflowRunOut)
async def decide_run(
    run_id: uuid.UUID,
    payload: WorkflowDecisionRequest,
    db: DbSession,
    user: CurrentUser,
    request: Request,
) -> WorkflowRunOut:
    run = await automation.decide_run(
        db,
        user,
        audit_context(request, user),
        run_id,
        approved=payload.approved,
        rationale=payload.rationale,
    )
    return await _run_out(db, user, run)


@router.post("/runs/{run_id}/cancel", response_model=WorkflowRunOut)
async def cancel_run(
    run_id: uuid.UUID, db: DbSession, user: CurrentUser, request: Request
) -> WorkflowRunOut:
    run = await automation.cancel_run(db, user, audit_context(request, user), run_id)
    return await _run_out(db, user, run)


@router.post("/runs/{run_id}/retry", response_model=WorkflowRunOut)
async def retry_run(
    run_id: uuid.UUID, db: DbSession, user: CurrentUser, request: Request
) -> WorkflowRunOut:
    run = await automation.retry_run(db, user, audit_context(request, user), run_id)
    return await _run_out(db, user, run)


@router.get("/tasks", response_model=WorkflowTaskListResponse)
async def list_tasks(db: DbSession, user: CurrentUser) -> WorkflowTaskListResponse:
    rows = await automation.list_tasks(db, user)
    return WorkflowTaskListResponse(tasks=[WorkflowTaskOut.model_validate(row) for row in rows])


@router.post("/tasks/{task_id}/complete", response_model=WorkflowTaskOut)
async def complete_task(
    task_id: uuid.UUID, db: DbSession, user: CurrentUser, request: Request
) -> WorkflowTaskOut:
    row = await automation.complete_task(db, user, audit_context(request, user), task_id)
    return WorkflowTaskOut.model_validate(row)


@router.get("/notifications", response_model=WorkflowNotificationListResponse)
async def list_notifications(db: DbSession, user: CurrentUser) -> WorkflowNotificationListResponse:
    rows = await automation.list_notifications(db, user)
    return WorkflowNotificationListResponse(
        items=[WorkflowNotificationOut.model_validate(row) for row in rows]
    )


@router.post("/notifications/{notification_id}/read", response_model=WorkflowNotificationOut)
async def mark_notification_read(
    notification_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> WorkflowNotificationOut:
    row = await automation.mark_notification_read(db, user, notification_id)
    return WorkflowNotificationOut.model_validate(row)

