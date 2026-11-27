"""Recovery-safe worker for approval-gated internal-task workflows."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    AccessGroupMember,
    AutomationApproval,
    AutomationRun,
    AutomationTask,
    AutomationWorkflow,
    PersonalTask,
    User,
    WorkflowNotification,
)
from app.services.audit import AuditContext, record
from app.services.automation import notify_approvers

logger = logging.getLogger(__name__)
AUTOMATION_POLL_SECONDS = 5
AUTOMATION_SCHEDULE_BATCH = 100
AUTOMATION_MAX_ATTEMPTS = 3
_RUN_LEASE = timedelta(seconds=60)


@dataclass(frozen=True)
class _RunClaim:
    run_id: uuid.UUID
    attempt: int
    expires_at: datetime

    def owns(self, run: AutomationRun | None, now: datetime) -> bool:
        return (
            run is not None
            and run.status == "processing"
            and run.attempts == self.attempt
            and run.locked_until == self.expires_at
            and now < self.expires_at
        )


class WorkflowExecutionError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def schedule_due_workflows(
    sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime | None = None
) -> int:
    """Create idempotent, approval-pending runs for due interval workflows."""
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("The workflow scheduler requires a timezone-aware timestamp.")
    created = 0
    async with sessionmaker() as db:
        workflows = list(
            (
                await db.execute(
                    select(AutomationWorkflow)
                    .where(
                        AutomationWorkflow.enabled.is_(True),
                        AutomationWorkflow.trigger_type == "interval",
                        AutomationWorkflow.next_run_at.is_not(None),
                        AutomationWorkflow.next_run_at <= current,
                    )
                    .order_by(AutomationWorkflow.next_run_at, AutomationWorkflow.id)
                    .with_for_update(skip_locked=True)
                    .limit(AUTOMATION_SCHEDULE_BATCH)
                )
            ).scalars()
        )
        for workflow in workflows:
            scheduled_at = workflow.next_run_at
            assert scheduled_at is not None and workflow.interval_minutes is not None
            key = f"interval:{workflow.id}:{scheduled_at.astimezone(UTC).isoformat()}"
            run_id = uuid.uuid4()
            result = await db.execute(
                insert(AutomationRun)
                .values(
                    id=run_id,
                    workflow_id=workflow.id,
                    requester_user_id=workflow.created_by_user_id,
                    department_group_id=workflow.department_group_id,
                    idempotency_key=key,
                    status="pending_approval",
                    attempts=0,
                    available_at=current,
                    created_at=current,
                )
                .on_conflict_do_nothing(constraint="uq_automation_runs_idempotency_key")
            )
            if result.rowcount:
                created += 1
                await notify_approvers(
                    db,
                    run_id=run_id,
                    department_group_id=workflow.department_group_id,
                    requester_user_id=workflow.created_by_user_id,
                )
            # Skip missed intervals: one catch-up run avoids a burst after a long outage.
            workflow.next_run_at = current + timedelta(minutes=workflow.interval_minutes)
            record(
                db,
                AuditContext(None, "automation-worker", None, None),
                "automation.scheduled_run_created",
                target=str(workflow.id),
                details={"version": workflow.version, "scheduled_at": scheduled_at.isoformat()},
            )
        await db.commit()
    return created


async def _claim_next(sessionmaker: async_sessionmaker[AsyncSession], now: datetime) -> _RunClaim | None:
    async with sessionmaker() as db:
        run = (
            await db.execute(
                select(AutomationRun)
                .where(
                    or_(
                        (AutomationRun.status == "queued") & (AutomationRun.available_at <= now),
                        (AutomationRun.status == "processing")
                        & or_(AutomationRun.locked_until.is_(None), AutomationRun.locked_until < now),
                    )
                )
                .order_by(AutomationRun.available_at, AutomationRun.created_at, AutomationRun.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if run is None:
            return None
        if run.status == "processing" and run.attempts >= AUTOMATION_MAX_ATTEMPTS:
            run.status = "failed"
            run.locked_until = None
            run.last_error_code = "lease_expired"
            await db.commit()
            return None
        run.status = "processing"
        run.attempts += 1
        run.locked_until = now + _RUN_LEASE
        run.last_error_code = None
        claim = _RunClaim(run.id, run.attempts, run.locked_until)
        await db.commit()
        return claim


async def _execute(sessionmaker: async_sessionmaker[AsyncSession], claim: _RunClaim) -> None:
    async with sessionmaker() as db:
        run = (
            await db.execute(select(AutomationRun).where(AutomationRun.id == claim.run_id).with_for_update())
        ).scalar_one_or_none()
        if run is None or not claim.owns(run, datetime.now(UTC)):
            return
        task = (
            await db.execute(select(AutomationTask).where(AutomationTask.run_id == run.id))
        ).scalar_one_or_none()
        if task is not None:
            run.status = "completed"
            run.completed_at = task.created_at
            run.locked_until = None
            await db.commit()
            return

        workflow = await db.get(AutomationWorkflow, run.workflow_id)
        if workflow is None or workflow.action_type not in {"create_task", "notify_member", "assign_personal_task"}:
            raise WorkflowExecutionError("action_not_allowlisted")
        if workflow.action_type == "assign_personal_task":
            existing_assignment = (
                await db.execute(
                    select(PersonalTask).where(PersonalTask.automation_run_id == run.id)
                )
            ).scalar_one_or_none()
            if existing_assignment is not None:
                run.status = "completed"
                run.completed_at = existing_assignment.created_at
                run.locked_until = None
                await db.commit()
                return
        if workflow.action_type == "notify_member":
            existing_notice = (
                await db.execute(
                    select(WorkflowNotification).where(
                        WorkflowNotification.run_id == run.id,
                        WorkflowNotification.notification_type == "workflow_message",
                    )
                )
            ).scalar_one_or_none()
            if existing_notice is not None:
                run.status = "completed"
                run.completed_at = existing_notice.created_at
                run.locked_until = None
                await db.commit()
                return
        approval = (
            await db.execute(select(AutomationApproval).where(AutomationApproval.run_id == run.id))
        ).scalar_one_or_none()
        if approval is None or approval.decision != "approved":
            raise WorkflowExecutionError("approval_missing")
        config = workflow.action_config
        if workflow.action_type == "notify_member":
            try:
                recipient_id = uuid.UUID(config["recipient_user_id"])
                title = config["title"]
                body = config["body"]
            except (KeyError, TypeError, ValueError):
                raise WorkflowExecutionError("invalid_action_config") from None
            if (
                not isinstance(title, str)
                or not 1 <= len(title.strip()) <= 180
                or not isinstance(body, str)
                or not 1 <= len(body.strip()) <= 2000
            ):
                raise WorkflowExecutionError("invalid_action_config")
            recipient = await db.get(User, recipient_id)
            membership = await db.get(AccessGroupMember, (run.department_group_id, recipient_id))
            if recipient is None or not recipient.is_active or membership is None:
                raise WorkflowExecutionError("recipient_membership_revoked")
            current = datetime.now(UTC)
            db.add(
                WorkflowNotification(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    task_id=None,
                    recipient_user_id=recipient_id,
                    notification_type="workflow_message",
                    message_title=" ".join(title.split()),
                    message_body=" ".join(body.split()),
                )
            )
            # The database run-state trigger validates durable completion evidence.
            await db.flush()
            run.status = "completed"
            run.completed_at = current
            run.locked_until = None
            run.last_error_code = None
            record(
                db,
                AuditContext(None, "automation-worker", None, None),
                "automation.member_message_created",
                target=str(run.id),
                details={"department_id": str(run.department_group_id), "recipient_id": str(recipient_id)},
            )
            await db.commit()
            return
        try:
            assignee_id = uuid.UUID(config["assignee_user_id"])
            title = config["title"]
            body = config["body"]
            due_days = config.get("due_days")
        except (KeyError, TypeError, ValueError):
            raise WorkflowExecutionError("invalid_action_config") from None
        if (
            not isinstance(title, str)
            or not 1 <= len(title.strip()) <= 180
            or not isinstance(body, str)
            or not 1 <= len(body.strip()) <= 4000
            or (due_days is not None and (not isinstance(due_days, int) or not 0 <= due_days <= 365))
        ):
            raise WorkflowExecutionError("invalid_action_config")
        assignee = await db.get(User, assignee_id)
        membership = await db.get(AccessGroupMember, (run.department_group_id, assignee_id))
        if assignee is None or not assignee.is_active or membership is None:
            raise WorkflowExecutionError("assignee_membership_revoked")
        current = datetime.now(UTC)
        if workflow.action_type == "assign_personal_task":
            creator = await db.get(User, workflow.created_by_user_id) if workflow.created_by_user_id else None
            if creator is None or not creator.is_active:
                raise WorkflowExecutionError("workflow_owner_unavailable")
            assignment = PersonalTask(
                automation_run_id=run.id,
                created_by_user_id=creator.id,
                assignee_user_id=assignee_id,
                department_group_id=run.department_group_id,
                title=" ".join(title.split()),
                body=" ".join(body.split()),
                due_at=current + timedelta(days=due_days) if due_days is not None else None,
            )
            db.add(assignment)
            await db.flush()
            run.status = "completed"
            run.completed_at = current
            run.locked_until = None
            run.last_error_code = None
            record(
                db,
                AuditContext(None, "automation-worker", None, None),
                "automation.personal_task_assigned",
                target=str(run.id),
                details={
                    "department_id": str(run.department_group_id),
                    "assignee_id": str(assignee_id),
                    "personal_task_id": str(assignment.id),
                },
            )
            await db.commit()
            return
        task = AutomationTask(
            run_id=run.id,
            assignee_user_id=assignee_id,
            department_group_id=run.department_group_id,
            title=" ".join(title.split()),
            body=" ".join(body.split()),
            due_at=current + timedelta(days=due_days) if due_days is not None else None,
        )
        db.add(task)
        # Insert the task while the run is still processing; the migration trigger validates that state.
        await db.flush()
        await db.execute(
            insert(WorkflowNotification)
            .values(
                id=uuid.uuid4(),
                run_id=run.id,
                task_id=task.id,
                recipient_user_id=assignee_id,
                notification_type="task_assigned",
            )
            .on_conflict_do_nothing(constraint="uq_workflow_notifications_run_recipient_type")
        )
        run.status = "completed"
        run.completed_at = current
        run.locked_until = None
        run.last_error_code = None
        record(
            db,
            AuditContext(None, "automation-worker", None, None),
            "automation.task_created",
            target=str(run.id),
            details={"department_id": str(run.department_group_id), "assignee_id": str(assignee_id)},
        )
        # Task creation and run completion share one transaction; unique(run_id) prevents duplicates.
        await db.commit()


async def _record_failure(
    sessionmaker: async_sessionmaker[AsyncSession], claim: _RunClaim, code: str, now: datetime
) -> None:
    async with sessionmaker() as db:
        run = (
            await db.execute(select(AutomationRun).where(AutomationRun.id == claim.run_id).with_for_update())
        ).scalar_one_or_none()
        if run is None or not claim.owns(run, now):
            return
        run.last_error_code = code[:64]
        run.locked_until = None
        if run.attempts >= AUTOMATION_MAX_ATTEMPTS:
            run.status = "failed"
        else:
            run.status = "queued"
            run.available_at = now + timedelta(seconds=min(900, 15 * (2 ** (run.attempts - 1))))
        await db.commit()


async def process_next_workflow_run(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    now = datetime.now(UTC)
    claim = await _claim_next(sessionmaker, now)
    if claim is None:
        return False
    try:
        await _execute(sessionmaker, claim)
    except WorkflowExecutionError as exc:
        logger.warning("Workflow run %s failed with code %s", claim.run_id, exc.code)
        await _record_failure(sessionmaker, claim, exc.code, datetime.now(UTC))
    except Exception as exc:
        # Provider responses, action contents and database error strings can contain sensitive values.
        code = type(exc).__name__[:64]
        logger.warning("Workflow run %s failed (%s)", claim.run_id, code)
        await _record_failure(sessionmaker, claim, "worker_error", datetime.now(UTC))
    return True


async def process_workflows_once(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """Process due schedules and currently approved work; pending approvals stay untouched."""
    scheduled = await schedule_due_workflows(sessionmaker)
    processed = 0
    while await process_next_workflow_run(sessionmaker):
        processed += 1
    return scheduled + processed


async def run_automation_worker(
    sessionmaker: async_sessionmaker[AsyncSession], stop: asyncio.Event, *, interval_seconds: float = AUTOMATION_POLL_SECONDS
) -> None:
    while not stop.is_set():
        try:
            await schedule_due_workflows(sessionmaker)
            worked = await process_next_workflow_run(sessionmaker)
        except Exception as exc:
            logger.warning("Workflow worker iteration failed (%s)", type(exc).__name__)
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass

