"""Scoped, auditable direct assignments for the personal work inbox."""

from datetime import UTC, datetime
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import (
    ROLE_ADMIN,
    ROLE_EMPLOYEE,
    ROLE_MANAGER,
    ROLE_USER,
    AccessGroup,
    AccessGroupMember,
    DepartmentManager,
    PersonalTask,
    PersonalTaskReminder,
    User,
)
from app.schemas.personal_tasks import (
    PersonalTaskAssigneeOut,
    PersonalTaskCreate,
    PersonalTaskDepartmentOut,
    PersonalTaskOptionsOut,
    PersonalTaskReminderOut,
)
from app.services import audit
from app.services.audit import AuditContext


async def _department_access(db: AsyncSession, actor: User, department_id: uuid.UUID) -> AccessGroup:
    group = await db.scalar(
        select(AccessGroup).where(
            AccessGroup.id == department_id,
            AccessGroup.kind == "department",
            AccessGroup.source == "local",
        )
    )
    if group is None:
        raise NotFoundError("Task department not found.")
    if actor.role == ROLE_ADMIN:
        return group
    membership = await db.get(AccessGroupMember, (group.id, actor.id))
    manager = await db.get(DepartmentManager, (group.id, actor.id)) if actor.role == ROLE_MANAGER else None
    if membership is None and manager is None:
        raise NotFoundError("Task department not found.")
    return group


async def _has_department_access(db: AsyncSession, actor: User, department_id: uuid.UUID) -> bool:
    if actor.role == ROLE_ADMIN:
        return True
    if await db.get(AccessGroupMember, (department_id, actor.id)) is not None:
        return True
    return actor.role == ROLE_MANAGER and await db.get(DepartmentManager, (department_id, actor.id)) is not None


async def options(db: AsyncSession, actor: User) -> PersonalTaskOptionsOut:
    """Only expose members of departments the current Admin or Manager may assign within."""
    if actor.role == ROLE_ADMIN:
        groups = list(
            (
                await db.scalars(
                    select(AccessGroup)
                    .where(AccessGroup.kind == "department", AccessGroup.source == "local")
                    .order_by(AccessGroup.name)
                )
            ).all()
        )
    elif actor.role == ROLE_MANAGER:
        groups = list(
            (
                await db.scalars(
                    select(AccessGroup)
                    .join(DepartmentManager, DepartmentManager.group_id == AccessGroup.id)
                    .where(
                        DepartmentManager.manager_user_id == actor.id,
                        AccessGroup.kind == "department",
                        AccessGroup.source == "local",
                    )
                    .order_by(AccessGroup.name)
                )
            ).all()
        )
    else:
        return PersonalTaskOptionsOut(departments=[])

    result: list[PersonalTaskDepartmentOut] = []
    for group in groups:
        members = list(
            (
                await db.scalars(
                    select(User)
                    .join(AccessGroupMember, AccessGroupMember.user_id == User.id)
                    .where(
                        AccessGroupMember.group_id == group.id,
                        User.is_active.is_(True),
                        User.role.in_((ROLE_EMPLOYEE, ROLE_USER)),
                    )
                    .order_by(User.email)
                )
            ).all()
        )
        result.append(
            PersonalTaskDepartmentOut(
                id=group.id,
                name=group.name,
                members=[PersonalTaskAssigneeOut(user_id=user.id, email=user.email) for user in members],
            )
        )
    return PersonalTaskOptionsOut(departments=result)


async def create(
    db: AsyncSession,
    actor: User,
    payload: PersonalTaskCreate,
    *,
    ctx: AuditContext,
) -> PersonalTask:
    if not actor.is_active:
        raise PermissionDeniedError("An inactive account cannot create tasks.")
    assignee_id = payload.assignee_user_id or actor.id
    assignee = await db.scalar(select(User).where(User.id == assignee_id, User.is_active.is_(True)))
    if assignee is None:
        raise NotFoundError("Active task assignee not found.")

    department = None
    if assignee.id != actor.id:
        if actor.role not in (ROLE_ADMIN, ROLE_MANAGER):
            raise PermissionDeniedError("Only an Admin or department Manager may assign a task to another person.")
        if payload.department_group_id is None:
            raise InvalidInputError("Choose a department when assigning a task to another person.")
        department = await _department_access(db, actor, payload.department_group_id)
        if assignee.role not in (ROLE_EMPLOYEE, ROLE_USER):
            raise PermissionDeniedError("Tasks may only be assigned to active employee accounts.")
        if await db.get(AccessGroupMember, (department.id, assignee.id)) is None:
            raise PermissionDeniedError("The assignee must be an active member of the selected department.")
    elif payload.department_group_id is not None:
        department = await _department_access(db, actor, payload.department_group_id)

    task = PersonalTask(
        created_by_user_id=actor.id,
        assignee_user_id=assignee.id,
        department_group_id=department.id if department else None,
        title=payload.title,
        body=payload.body,
        due_at=payload.due_at,
    )
    db.add(task)
    await db.flush()
    audit.record(
        db,
        ctx,
        "personal_task.created",
        target=str(task.id),
        details={
            "assignee_user_id": str(assignee.id),
            "department_group_id": str(department.id) if department else None,
            "has_deadline": payload.due_at is not None,
        },
    )
    await db.commit()
    return await get(db, actor, task.id)


async def _get_scoped(db: AsyncSession, actor: User, task_id: uuid.UUID) -> PersonalTask:
    task = await db.scalar(
        select(PersonalTask)
        .options(selectinload(PersonalTask.created_by), selectinload(PersonalTask.assignee))
        .where(
            PersonalTask.id == task_id,
            or_(PersonalTask.created_by_user_id == actor.id, PersonalTask.assignee_user_id == actor.id),
        )
    )
    if task is None or (
        task.department_group_id is not None
        and not await _has_department_access(db, actor, task.department_group_id)
    ):
        raise NotFoundError("Task not found.")
    return task


async def get(db: AsyncSession, actor: User, task_id: uuid.UUID) -> PersonalTask:
    return await _get_scoped(db, actor, task_id)


async def list_for_user(db: AsyncSession, actor: User) -> list[PersonalTask]:
    rows = list(
        (
            await db.scalars(
                select(PersonalTask)
                .options(selectinload(PersonalTask.created_by), selectinload(PersonalTask.assignee))
                .where(
                    or_(PersonalTask.created_by_user_id == actor.id, PersonalTask.assignee_user_id == actor.id)
                )
                .order_by(PersonalTask.due_at.asc().nulls_last(), PersonalTask.created_at.desc())
                .limit(200)
            )
        ).all()
    )
    visible: list[PersonalTask] = []
    for task in rows:
        if task.department_group_id is None or await _has_department_access(db, actor, task.department_group_id):
            visible.append(task)
    return visible


async def reminders_for_user(db: AsyncSession, actor: User) -> list[PersonalTaskReminderOut]:
    """Return active reminders only while the recipient still has access to the task."""
    rows = list(
        (
            await db.execute(
                select(PersonalTaskReminder, PersonalTask)
                .join(PersonalTask, PersonalTask.id == PersonalTaskReminder.task_id)
                .where(
                    PersonalTaskReminder.recipient_user_id == actor.id,
                    PersonalTask.status == "open",
                )
                .order_by(PersonalTaskReminder.created_at.desc(), PersonalTaskReminder.id)
                .limit(100)
            )
        ).all()
    )
    result: list[PersonalTaskReminderOut] = []
    for reminder, task in rows:
        if task.department_group_id is not None and not await _has_department_access(
            db, actor, task.department_group_id
        ):
            continue
        result.append(
            PersonalTaskReminderOut(
                id=reminder.id,
                task_id=task.id,
                title=task.title,
                reminder_type=reminder.reminder_type,
                due_at=reminder.due_at,
                created_at=reminder.created_at,
                read_at=reminder.read_at,
                email_status=reminder.email_status,
            )
        )
    return result


async def mark_reminder_read(
    db: AsyncSession, actor: User, reminder_id: uuid.UUID
) -> PersonalTaskReminderOut:
    row = (
        await db.execute(
            select(PersonalTaskReminder, PersonalTask)
            .join(PersonalTask, PersonalTask.id == PersonalTaskReminder.task_id)
            .where(
                PersonalTaskReminder.id == reminder_id,
                PersonalTaskReminder.recipient_user_id == actor.id,
                PersonalTask.status == "open",
            )
            .with_for_update(of=PersonalTaskReminder)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("Personal task reminder not found.")
    reminder, task = row
    if task.department_group_id is not None and not await _has_department_access(
        db, actor, task.department_group_id
    ):
        raise NotFoundError("Personal task reminder not found.")
    if reminder.read_at is None:
        reminder.read_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(reminder)
    return PersonalTaskReminderOut(
        id=reminder.id,
        task_id=task.id,
        title=task.title,
        reminder_type=reminder.reminder_type,
        due_at=reminder.due_at,
        created_at=reminder.created_at,
        read_at=reminder.read_at,
        email_status=reminder.email_status,
    )


async def complete(db: AsyncSession, actor: User, ctx: AuditContext, task_id: uuid.UUID) -> PersonalTask:
    task = await _get_scoped(db, actor, task_id)
    if task.assignee_user_id != actor.id:
        raise PermissionDeniedError("Only the assigned employee can complete this task.")
    if task.status != "open":
        raise ConflictError("Only an open task can be completed.")
    task.status = "completed"
    task.completed_at = datetime.now(UTC)
    audit.record(db, ctx, "personal_task.completed", target=str(task.id), details={})
    await db.commit()
    return await get(db, actor, task.id)


async def cancel(db: AsyncSession, actor: User, ctx: AuditContext, task_id: uuid.UUID) -> PersonalTask:
    task = await _get_scoped(db, actor, task_id)
    if actor.id not in (task.created_by_user_id, task.assignee_user_id):
        raise NotFoundError("Task not found.")
    if task.status != "open":
        raise ConflictError("Only an open task can be cancelled.")
    task.status = "cancelled"
    task.cancelled_at = datetime.now(UTC)
    audit.record(db, ctx, "personal_task.cancelled", target=str(task.id), details={})
    await db.commit()
    return await get(db, actor, task.id)
