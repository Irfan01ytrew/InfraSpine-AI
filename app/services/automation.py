"""Department-scoped workflow configuration and approval policy.

The initial action registry intentionally contains one local action: creating an in-app task.
No workflow can execute arbitrary code, access external URLs, or send external notifications.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import (
    ROLE_ADMIN,
    ROLE_MANAGER,
    AccessGroup,
    AccessGroupMember,
    AutomationApproval,
    AutomationRun,
    AutomationTask,
    AutomationWorkflow,
    DepartmentManager,
    ServiceRequest,
    User,
    WorkflowNotification,
)
from app.services import audit
from app.services.audit import AuditContext

_NOT_FOUND = "Workflow or task not found."


async def _group_access(
    db: AsyncSession, user: User, group_id: uuid.UUID, *, manage: bool = False
) -> AccessGroup:
    group = await db.get(AccessGroup, group_id)
    if group is None or group.kind != "department":
        raise NotFoundError(_NOT_FOUND)
    if user.role == ROLE_ADMIN:
        return group
    manager = await db.get(DepartmentManager, (group_id, user.id))
    if user.role == ROLE_MANAGER and manager is not None:
        return group
    if not manage:
        member = await db.get(AccessGroupMember, (group_id, user.id))
        if member is not None:
            return group
    raise PermissionDeniedError("Department membership or manager access is required.")


async def _validate_assignee(db: AsyncSession, group_id: uuid.UUID, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    member = await db.get(AccessGroupMember, (group_id, user_id))
    if user is None or not user.is_active or member is None:
        raise InvalidInputError("The workflow assignee must be an active member of its department.")
    return user


def _action_config(*, assignee_user_id: uuid.UUID, title: str, body: str, due_days: int | None) -> dict:
    normalized_title = " ".join(title.split())
    normalized_body = " ".join(body.split())
    if not normalized_title or len(normalized_title) > 180:
        raise InvalidInputError("Task title must contain between 1 and 180 characters.")
    if not normalized_body or len(normalized_body) > 4000:
        raise InvalidInputError("Task instructions must contain between 1 and 4000 characters.")
    if due_days is not None and not 0 <= due_days <= 365:
        raise InvalidInputError("Task due days must be between 0 and 365.")
    return {
        "assignee_user_id": str(assignee_user_id),
        "title": normalized_title,
        "body": normalized_body,
        "due_days": due_days,
    }


def _notification_action_config(*, recipient_user_id: uuid.UUID, title: str, body: str) -> dict:
    normalized_title = " ".join(title.split())
    normalized_body = " ".join(body.split())
    if not normalized_title or len(normalized_title) > 180:
        raise InvalidInputError("Notification title must contain between 1 and 180 characters.")
    if not normalized_body or len(normalized_body) > 2000:
        raise InvalidInputError("Notification message must contain between 1 and 2000 characters.")
    return {
        "recipient_user_id": str(recipient_user_id),
        "title": normalized_title,
        "body": normalized_body,
    }


async def _workflow_action_config(
    db: AsyncSession,
    group_id: uuid.UUID,
    *,
    action_type: str,
    assignee_user_id: uuid.UUID | None,
    title: str | None,
    body: str | None,
    due_days: int | None,
    notification_recipient_user_id: uuid.UUID | None,
    notification_title: str | None,
    notification_body: str | None,
) -> dict:
    if action_type in {"create_task", "assign_personal_task"}:
        if assignee_user_id is None or title is None or body is None:
            raise InvalidInputError("Task workflows require an assignee, title and instructions.")
        await _validate_assignee(db, group_id, assignee_user_id)
        return _action_config(assignee_user_id=assignee_user_id, title=title, body=body, due_days=due_days)
    if action_type == "notify_member":
        if notification_recipient_user_id is None or notification_title is None or notification_body is None:
            raise InvalidInputError("Member notifications require a recipient, title and message.")
        await _validate_assignee(db, group_id, notification_recipient_user_id)
        return _notification_action_config(
            recipient_user_id=notification_recipient_user_id,
            title=notification_title,
            body=notification_body,
        )
    raise InvalidInputError("This workflow action type is not supported.")


def _audit(db: AsyncSession, ctx: AuditContext, action: str, target: uuid.UUID, details: dict) -> None:
    audit.record(db, ctx, action, target=str(target), details=details)


async def notify_approvers(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    department_group_id: uuid.UUID,
    requester_user_id: uuid.UUID | None,
) -> None:
    """Write inbox notices for active admins and current managers in this department.

    Notifications contain only internal IDs. The inbox rechecks current department membership
    on read, so revocation immediately removes access to an existing notice.
    """
    admin_ids = set(
        (
            await db.execute(
                select(User.id).where(User.role == ROLE_ADMIN, User.is_active.is_(True))
            )
        ).scalars()
    )
    manager_ids = set(
        (
            await db.execute(
                select(DepartmentManager.manager_user_id)
                .join(User, User.id == DepartmentManager.manager_user_id)
                .where(
                    DepartmentManager.group_id == department_group_id,
                    User.is_active.is_(True),
                )
            )
        ).scalars()
    )
    recipients = admin_ids | manager_ids
    if requester_user_id is not None:
        recipients.discard(requester_user_id)
    if not recipients:
        return
    await db.execute(
        insert(WorkflowNotification)
        .values(
            [
                {
                    "id": uuid.uuid4(),
                    "run_id": run_id,
                    "task_id": None,
                    "recipient_user_id": recipient_id,
                    "notification_type": "approval_required",
                }
                for recipient_id in recipients
            ]
        )
        .on_conflict_do_nothing(constraint="uq_workflow_notifications_run_recipient_type")
    )


async def enqueue_service_request_workflows(
    db: AsyncSession,
    *,
    request_id: uuid.UUID,
    department_group_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    event_type: str = "submitted",
    now: datetime | None = None,
) -> int:
    """Queue approval-gated workflow runs for a scoped submission or response-SLA escalation.

    The caller owns the transaction. HR and unscoped requests must never call this function.
    Idempotency is bound to the source request and immutable workflow version.
    """
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("The event workflow requires a timezone-aware timestamp.")
    if event_type not in {"submitted", "sla_escalated"}:
        raise ValueError("Unsupported service-request workflow event.")
    source_conditions = [ServiceRequest.id == request_id, ServiceRequest.department_group_id == department_group_id,
                         ServiceRequest.requester_user_id == requester_user_id, ServiceRequest.category != "hr"]
    if event_type == "sla_escalated":
        source_conditions.append(ServiceRequest.sla_escalated_at.is_not(None))
    if await db.scalar(select(ServiceRequest.id).where(*source_conditions)) is None:
        return 0
    trigger_type = "service_request" if event_type == "submitted" else "request_sla"
    event_key = "service-request" if event_type == "submitted" else "service-request-sla"
    workflows = list((await db.scalars(
        select(AutomationWorkflow)
        .where(
            AutomationWorkflow.enabled.is_(True),
            AutomationWorkflow.trigger_type == trigger_type,
            AutomationWorkflow.department_group_id == department_group_id,
        )
        .order_by(AutomationWorkflow.id)
    )).all())
    created = 0
    for workflow in workflows:
        run_id = uuid.uuid4()
        result = await db.execute(
            insert(AutomationRun)
            .values(
                id=run_id,
                workflow_id=workflow.id,
                requester_user_id=requester_user_id,
                department_group_id=department_group_id,
                idempotency_key=f"{event_key}:{request_id}:{workflow.id}",
                status="pending_approval",
                attempts=0,
                available_at=current,
                created_at=current,
            )
            .on_conflict_do_nothing(constraint="uq_automation_runs_idempotency_key")
        )
        if not result.rowcount:
            continue
        created += 1
        await notify_approvers(
            db,
            run_id=run_id,
            department_group_id=department_group_id,
            requester_user_id=requester_user_id,
        )
        _audit(
            db,
            AuditContext(None, "automation-event", None, None),
            "automation.event_run_created",
            workflow.id,
            {"version": workflow.version, "event_type": f"service_request_{event_type}"},
        )
    return created


async def create_workflow(
    db: AsyncSession,
    actor: User,
    ctx: AuditContext,
    *,
    name: str,
    department_group_id: uuid.UUID,
    trigger_type: str,
    interval_minutes: int | None,
    action_type: str,
    assignee_user_id: uuid.UUID | None,
    title: str | None,
    body: str | None,
    due_days: int | None,
    notification_recipient_user_id: uuid.UUID | None,
    notification_title: str | None,
    notification_body: str | None,
) -> AutomationWorkflow:
    group = await _group_access(db, actor, department_group_id, manage=True)
    action_config = await _workflow_action_config(
        db,
        group.id,
        action_type=action_type,
        assignee_user_id=assignee_user_id,
        title=title,
        body=body,
        due_days=due_days,
        notification_recipient_user_id=notification_recipient_user_id,
        notification_title=notification_title,
        notification_body=notification_body,
    )
    if trigger_type not in {"manual", "interval", "service_request", "request_sla"}:
        raise InvalidInputError("This workflow trigger type is not supported.")
    if trigger_type != "interval" and interval_minutes is not None:
        raise InvalidInputError("Only scheduled workflows can specify an interval.")
    if trigger_type == "interval" and (interval_minutes is None or not 60 <= interval_minutes <= 10080):
        raise InvalidInputError("Scheduled intervals must be between 60 minutes and 7 days.")
    if not name.strip() or len(name.strip()) > 120:
        raise InvalidInputError("Workflow name must contain between 1 and 120 characters.")
    now = datetime.now(UTC)
    workflow = AutomationWorkflow(
        id=uuid.uuid4(),
        definition_key=uuid.uuid4(),
        version=1,
        name=" ".join(name.split()),
        department_group_id=group.id,
        trigger_type=trigger_type,
        interval_minutes=interval_minutes,
        action_type=action_type,
        action_config=action_config,
        enabled=True,
        created_by_user_id=actor.id,
        next_run_at=now + timedelta(minutes=interval_minutes) if interval_minutes is not None else None,
    )
    db.add(workflow)
    _audit(db, ctx, "automation.workflow_created", workflow.id, {"version": 1, "department_id": str(group.id)})
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("The workflow could not be saved due to a concurrent change.") from exc
    await db.refresh(workflow)
    return workflow


async def create_workflow_version(
    db: AsyncSession,
    actor: User,
    ctx: AuditContext,
    workflow_id: uuid.UUID,
    *,
    expected_version: int,
    name: str,
    trigger_type: str,
    interval_minutes: int | None,
    action_type: str,
    assignee_user_id: uuid.UUID | None,
    title: str | None,
    body: str | None,
    due_days: int | None,
    notification_recipient_user_id: uuid.UUID | None,
    notification_title: str | None,
    notification_body: str | None,
) -> AutomationWorkflow:
    current = (
        await db.execute(select(AutomationWorkflow).where(AutomationWorkflow.id == workflow_id).with_for_update())
    ).scalar_one_or_none()
    if current is None:
        raise NotFoundError(_NOT_FOUND)
    group = await _group_access(db, actor, current.department_group_id, manage=True)
    if not current.enabled or current.version != expected_version:
        raise ConflictError("The workflow version changed. Reload before editing.")
    action_config = await _workflow_action_config(
        db,
        group.id,
        action_type=action_type,
        assignee_user_id=assignee_user_id,
        title=title,
        body=body,
        due_days=due_days,
        notification_recipient_user_id=notification_recipient_user_id,
        notification_title=notification_title,
        notification_body=notification_body,
    )
    if trigger_type not in {"manual", "interval", "service_request", "request_sla"}:
        raise InvalidInputError("This workflow trigger type is not supported.")
    if trigger_type != "interval" and interval_minutes is not None:
        raise InvalidInputError("Only scheduled workflows can specify an interval.")
    if trigger_type == "interval" and (interval_minutes is None or not 60 <= interval_minutes <= 10080):
        raise InvalidInputError("Scheduled intervals must be between 60 minutes and 7 days.")
    if not name.strip() or len(name.strip()) > 120:
        raise InvalidInputError("Workflow name must contain between 1 and 120 characters.")
    current.enabled = False
    current.next_run_at = None
    # Vacate the partial unique index before inserting the next enabled version.
    await db.flush()
    now = datetime.now(UTC)
    version = AutomationWorkflow(
        id=uuid.uuid4(),
        definition_key=current.definition_key,
        version=current.version + 1,
        name=" ".join(name.split()),
        department_group_id=current.department_group_id,
        trigger_type=trigger_type,
        interval_minutes=interval_minutes,
        action_type=action_type,
        action_config=action_config,
        enabled=True,
        created_by_user_id=actor.id,
        next_run_at=now + timedelta(minutes=interval_minutes) if interval_minutes is not None else None,
    )
    db.add(version)
    _audit(db, ctx, "automation.workflow_version_created", version.id, {"version": version.version})
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("The workflow could not be versioned due to a concurrent change.") from exc
    await db.refresh(version)
    return version


async def disable_workflow(db: AsyncSession, actor: User, ctx: AuditContext, workflow_id: uuid.UUID) -> None:
    workflow = (
        await db.execute(select(AutomationWorkflow).where(AutomationWorkflow.id == workflow_id).with_for_update())
    ).scalar_one_or_none()
    if workflow is None:
        raise NotFoundError(_NOT_FOUND)
    await _group_access(db, actor, workflow.department_group_id, manage=True)
    if not workflow.enabled:
        return
    workflow.enabled = False
    workflow.next_run_at = None
    _audit(db, ctx, "automation.workflow_disabled", workflow.id, {"version": workflow.version})
    await db.commit()


async def list_workflows(db: AsyncSession, actor: User) -> list[AutomationWorkflow]:
    query = select(AutomationWorkflow).where(AutomationWorkflow.enabled.is_(True))
    if actor.role != ROLE_ADMIN:
        member_groups = select(AccessGroupMember.group_id).where(AccessGroupMember.user_id == actor.id)
        if actor.role == ROLE_MANAGER:
            member_groups = member_groups.union(
                select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == actor.id)
            )
        query = query.where(AutomationWorkflow.department_group_id.in_(member_groups))
    return list((await db.execute(query.order_by(AutomationWorkflow.name, AutomationWorkflow.version))).scalars())


async def dry_run_workflow(db: AsyncSession, actor: User, workflow_id: uuid.UUID) -> dict:
    """Validate and preview an enabled workflow without writing audit, run or task rows."""
    workflow = await db.get(AutomationWorkflow, workflow_id)
    if workflow is None or not workflow.enabled:
        raise NotFoundError(_NOT_FOUND)
    group = await _group_access(db, actor, workflow.department_group_id, manage=True)
    if workflow.trigger_type not in {"manual", "interval", "service_request", "request_sla"}:
        raise ConflictError("The workflow trigger configuration is invalid.")
    if (workflow.trigger_type != "interval" and workflow.interval_minutes is not None) or (
        workflow.trigger_type == "interval"
        and (workflow.interval_minutes is None or not 60 <= workflow.interval_minutes <= 10080)
    ):
        raise ConflictError("The workflow trigger configuration is invalid.")
    if workflow.action_type not in {"create_task", "notify_member", "assign_personal_task"} or not isinstance(workflow.action_config, dict):
        raise ConflictError("The workflow action configuration is invalid.")

    config = workflow.action_config
    task_preview = None
    notification_preview = None
    if workflow.action_type in {"create_task", "assign_personal_task"}:
        try:
            assignee_id = uuid.UUID(config["assignee_user_id"])
            title = config["title"]
            body = config["body"]
            due_days = config.get("due_days")
            normalized = _action_config(
                assignee_user_id=assignee_id,
                title=title,
                body=body,
                due_days=due_days,
            )
        except (KeyError, TypeError, ValueError, InvalidInputError, AttributeError):
            raise ConflictError("The workflow action configuration is invalid.") from None
        assignee = await _validate_assignee(db, group.id, assignee_id)
        action_id = "personal-task" if workflow.action_type == "assign_personal_task" else "task"
        action_title = (
            "Create one My Work assignment"
            if workflow.action_type == "assign_personal_task"
            else "Create one internal task"
        )
        action_description = (
            f"Add the task to {assignee.email}'s My Work inbox after approval."
            if workflow.action_type == "assign_personal_task"
            else f"Assign the task to {assignee.email} after approval."
        )
        task_preview = {
            "title": normalized["title"],
            "body": normalized["body"],
            "assignee_email": assignee.email,
            "due_days": normalized["due_days"],
        }
        target_summary = "The task assignee is active and belongs to this department."
        action_summary = (
            "The approved action creates one department-scoped My Work assignment."
            if workflow.action_type == "assign_personal_task"
            else "The approved action creates one internal task."
        )
    else:
        try:
            recipient_id = uuid.UUID(config["recipient_user_id"])
            title = config["title"]
            body = config["body"]
            normalized = _notification_action_config(
                recipient_user_id=recipient_id,
                title=title,
                body=body,
            )
        except (KeyError, TypeError, ValueError, InvalidInputError, AttributeError):
            raise ConflictError("The workflow action configuration is invalid.") from None
        recipient = await _validate_assignee(db, group.id, recipient_id)
        action_id = "notification"
        action_title = "Send one internal inbox message"
        action_description = f"Deliver a message to {recipient.email} after approval."
        notification_preview = {
            "title": normalized["title"],
            "body": normalized["body"],
            "recipient_email": recipient.email,
        }
        target_summary = "The recipient is active and belongs to this department."
        action_summary = "The approved action writes one department-scoped in-app message."

    trigger_title = (
        "Manual request" if workflow.trigger_type == "manual"
        else f"Scheduled every {workflow.interval_minutes} minutes" if workflow.trigger_type == "interval"
        else "Department help request response target missed" if workflow.trigger_type == "request_sla"
        else "Department help request submitted"
    )
    return {
        "workflow_id": workflow.id,
        "workflow_version": workflow.version,
        "valid": True,
        "side_effects_performed": False,
        "nodes": [
            {
                "id": "trigger",
                "node_type": "trigger",
                "title": trigger_title,
                "description": "A scheduled trigger only queues a request; it does not skip approval.",
            },
            {
                "id": "approval",
                "node_type": "approval",
                "title": "Independent department approval",
                "description": "An administrator or department manager must approve; the requester cannot approve their own run.",
            },
            {
                "id": action_id,
                "node_type": "action",
                "title": action_title,
                "description": action_description,
            },
            {
                "id": "completion",
                "node_type": "completion",
                "title": "Record completion",
                "description": "The run and its task are retained in department-scoped history.",
            },
        ],
        "edges": [
            {"source": "trigger", "target": "approval"},
            {"source": "approval", "target": action_id},
            {"source": action_id, "target": "completion"},
        ],
        "checks": [
            {"code": "department_access", "passed": True, "summary": f"You can manage the {group.name} department."},
            {"code": "active_assignee", "passed": True, "summary": target_summary},
            {"code": "approval_gate", "passed": True, "summary": "A separate authorized reviewer is required before execution."},
            {"code": "allowlisted_action", "passed": True, "summary": action_summary},
        ],
        "task_preview": task_preview,
        "notification_preview": notification_preview,
    }


async def request_run(
    db: AsyncSession,
    actor: User,
    ctx: AuditContext,
    workflow_id: uuid.UUID,
    idempotency_key: uuid.UUID,
) -> AutomationRun:
    workflow = await db.get(AutomationWorkflow, workflow_id)
    if workflow is None or not workflow.enabled:
        raise NotFoundError(_NOT_FOUND)
    await _group_access(db, actor, workflow.department_group_id)
    try:
        if workflow.action_type in {"create_task", "assign_personal_task"}:
            target_id = uuid.UUID(workflow.action_config.get("assignee_user_id", ""))
        elif workflow.action_type == "notify_member":
            target_id = uuid.UUID(workflow.action_config.get("recipient_user_id", ""))
        else:
            raise ValueError("unsupported action")
    except (TypeError, ValueError, AttributeError):
        raise ConflictError("The workflow action configuration is invalid.") from None
    await _validate_assignee(db, workflow.department_group_id, target_id)
    key = f"manual:{actor.id}:{idempotency_key}"
    existing = (
        await db.execute(select(AutomationRun).where(AutomationRun.idempotency_key == key))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.workflow_id != workflow.id or existing.requester_user_id != actor.id:
            raise ConflictError("This idempotency key was already used for another request.")
        return existing
    run = AutomationRun(
        id=uuid.uuid4(),
        workflow_id=workflow.id,
        requester_user_id=actor.id,
        department_group_id=workflow.department_group_id,
        idempotency_key=key,
        status="pending_approval",
    )
    db.add(run)
    await db.flush()
    await notify_approvers(
        db,
        run_id=run.id,
        department_group_id=run.department_group_id,
        requester_user_id=actor.id,
    )
    _audit(db, ctx, "automation.run_requested", run.id, {"workflow_id": str(workflow.id), "version": workflow.version})
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        existing = (
            await db.execute(select(AutomationRun).where(AutomationRun.idempotency_key == key))
        ).scalar_one_or_none()
        if existing is not None and existing.workflow_id == workflow.id and existing.requester_user_id == actor.id:
            return existing
        raise ConflictError("This workflow request could not be saved safely.") from exc
    await db.refresh(run)
    return run


async def list_runs(db: AsyncSession, actor: User) -> list[AutomationRun]:
    query = select(AutomationRun)
    if actor.role != ROLE_ADMIN:
        member_groups = select(AccessGroupMember.group_id).where(AccessGroupMember.user_id == actor.id)
        if actor.role == ROLE_MANAGER:
            member_groups = member_groups.union(
                select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == actor.id)
            )
        query = query.where(AutomationRun.department_group_id.in_(member_groups))
    return list((await db.execute(query.order_by(AutomationRun.created_at.desc()).limit(200))).scalars())


async def decide_run(
    db: AsyncSession,
    actor: User,
    ctx: AuditContext,
    run_id: uuid.UUID,
    *,
    approved: bool,
    rationale: str,
) -> AutomationRun:
    run = (
        await db.execute(select(AutomationRun).where(AutomationRun.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if run is None:
        raise NotFoundError(_NOT_FOUND)
    await _group_access(db, actor, run.department_group_id, manage=True)
    if run.requester_user_id == actor.id:
        raise PermissionDeniedError("A workflow requester cannot approve their own run.")
    if run.status != "pending_approval":
        raise ConflictError("This workflow run is no longer awaiting approval.")
    decision = AutomationApproval(
        run_id=run.id,
        approver_user_id=actor.id,
        decision="approved" if approved else "rejected",
        rationale=" ".join(rationale.split()),
    )
    db.add(decision)
    await db.flush()
    run.status = "queued" if approved else "rejected"
    run.available_at = datetime.now(UTC)
    _audit(db, ctx, "automation.run_approved" if approved else "automation.run_rejected", run.id, {})
    if run.requester_user_id is not None:
        notification_type = "run_approved" if approved else "run_rejected"
        await db.execute(
            insert(WorkflowNotification)
            .values(
                id=uuid.uuid4(),
                run_id=run.id,
                task_id=None,
                recipient_user_id=run.requester_user_id,
                notification_type=notification_type,
            )
            .on_conflict_do_nothing(constraint="uq_workflow_notifications_run_recipient_type")
        )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("A decision for this workflow run already exists.") from exc
    await db.refresh(run)
    return run


async def cancel_run(db: AsyncSession, actor: User, ctx: AuditContext, run_id: uuid.UUID) -> AutomationRun:
    run = (
        await db.execute(select(AutomationRun).where(AutomationRun.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if run is None:
        raise NotFoundError(_NOT_FOUND)
    if actor.id != run.requester_user_id:
        await _group_access(db, actor, run.department_group_id, manage=True)
    if run.status not in {"pending_approval", "queued"}:
        raise ConflictError("Only an unstarted workflow run can be cancelled.")
    run.status = "cancelled"
    run.locked_until = None
    _audit(db, ctx, "automation.run_cancelled", run.id, {})
    await db.commit()
    await db.refresh(run)
    return run


async def retry_run(db: AsyncSession, actor: User, ctx: AuditContext, run_id: uuid.UUID) -> AutomationRun:
    run = (
        await db.execute(select(AutomationRun).where(AutomationRun.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if run is None:
        raise NotFoundError(_NOT_FOUND)
    await _group_access(db, actor, run.department_group_id, manage=True)
    if run.status != "failed":
        raise ConflictError("Only failed workflow runs can be retried.")
    approval = (
        await db.execute(select(AutomationApproval).where(AutomationApproval.run_id == run.id))
    ).scalar_one_or_none()
    if approval is None or approval.decision != "approved":
        raise ConflictError("The run has no valid approval and cannot be retried.")
    task = (
        await db.execute(select(AutomationTask).where(AutomationTask.run_id == run.id))
    ).scalar_one_or_none()
    if task is not None:
        raise ConflictError("A task already exists for this run; it will not be duplicated.")
    run.status = "queued"
    run.attempts = 0
    run.available_at = datetime.now(UTC)
    run.locked_until = None
    run.last_error_code = None
    _audit(db, ctx, "automation.run_retried", run.id, {})
    await db.commit()
    await db.refresh(run)
    return run


async def list_tasks(db: AsyncSession, actor: User) -> list[AutomationTask]:
    query = select(AutomationTask)
    if actor.role != ROLE_ADMIN:
        member_groups = select(AccessGroupMember.group_id).where(AccessGroupMember.user_id == actor.id)
        if actor.role == ROLE_MANAGER:
            manager_groups = select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == actor.id)
            query = query.where(
                ((AutomationTask.assignee_user_id == actor.id) & AutomationTask.department_group_id.in_(member_groups))
                | AutomationTask.department_group_id.in_(manager_groups)
            )
        else:
            query = query.where(
                (AutomationTask.assignee_user_id == actor.id)
                & AutomationTask.department_group_id.in_(member_groups)
            )
    return list((await db.execute(query.order_by(AutomationTask.created_at.desc()).limit(200))).scalars())


async def complete_task(
    db: AsyncSession, actor: User, ctx: AuditContext, task_id: uuid.UUID
) -> AutomationTask:
    task = (
        await db.execute(select(AutomationTask).where(AutomationTask.id == task_id).with_for_update())
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError(_NOT_FOUND)
    if task.assignee_user_id != actor.id:
        raise PermissionDeniedError("Only the assigned employee can complete this task.")
    await _group_access(db, actor, task.department_group_id)
    if task.status != "open":
        raise ConflictError("This task is already complete.")
    task.status = "completed"
    task.completed_at = datetime.now(UTC)
    _audit(db, ctx, "automation.task_completed", task.id, {})
    await db.commit()
    await db.refresh(task)
    return task


async def list_notifications(db: AsyncSession, actor: User) -> list[WorkflowNotification]:
    query = (
        select(WorkflowNotification)
        .join(AutomationRun, AutomationRun.id == WorkflowNotification.run_id)
        .where(WorkflowNotification.recipient_user_id == actor.id)
    )
    if actor.role != ROLE_ADMIN:
        visible_groups = select(AccessGroupMember.group_id).where(AccessGroupMember.user_id == actor.id)
        if actor.role == ROLE_MANAGER:
            visible_groups = visible_groups.union(
                select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == actor.id)
            )
        query = query.where(AutomationRun.department_group_id.in_(visible_groups))
    return list(
        (
            await db.execute(
                query.order_by(WorkflowNotification.created_at.desc(), WorkflowNotification.id).limit(200)
            )
        ).scalars()
    )


async def mark_notification_read(
    db: AsyncSession, actor: User, notification_id: uuid.UUID
) -> WorkflowNotification:
    notification = (
        await db.execute(
            select(WorkflowNotification).where(
                WorkflowNotification.id == notification_id,
                WorkflowNotification.recipient_user_id == actor.id,
            )
        )
    ).scalar_one_or_none()
    if notification is None:
        raise NotFoundError("Workflow notification not found.")
    run = await db.get(AutomationRun, notification.run_id)
    if run is None:
        raise NotFoundError("Workflow notification not found.")
    try:
        await _group_access(db, actor, run.department_group_id)
    except (NotFoundError, PermissionDeniedError):
        raise NotFoundError("Workflow notification not found.") from None
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(notification)
    return notification

