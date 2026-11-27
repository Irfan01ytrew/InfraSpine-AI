"""Direct, department-scoped personal assignments for the My Work inbox."""

import uuid

from fastapi import APIRouter, Depends, Request

from app.api.deps import CurrentUser, DbSession, audit_context, get_current_user
from app.db.models import PersonalTask
from app.schemas.personal_tasks import (
    PersonalTaskCreate,
    PersonalTaskListOut,
    PersonalTaskOptionsOut,
    PersonalTaskOut,
    PersonalTaskReminderListOut,
    PersonalTaskReminderOut,
)
from app.services import personal_tasks

router = APIRouter(prefix="/api/personal-tasks", tags=["personal-tasks"], dependencies=[Depends(get_current_user)])


@router.get("/options", response_model=PersonalTaskOptionsOut)
async def task_options(db: DbSession, user: CurrentUser) -> PersonalTaskOptionsOut:
    return await personal_tasks.options(db, user)


@router.post("", response_model=PersonalTaskOut, status_code=201)
async def create_task(
    payload: PersonalTaskCreate,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> PersonalTaskOut:
    task = await personal_tasks.create(db, user, payload, ctx=audit_context(request, user))
    return PersonalTaskOut.model_validate(task)


@router.get("", response_model=PersonalTaskListOut)
async def list_tasks(db: DbSession, user: CurrentUser) -> PersonalTaskListOut:
    rows = await personal_tasks.list_for_user(db, user)
    return PersonalTaskListOut(tasks=[PersonalTaskOut.model_validate(row) for row in rows])


@router.get("/reminders", response_model=PersonalTaskReminderListOut)
async def list_task_reminders(db: DbSession, user: CurrentUser) -> PersonalTaskReminderListOut:
    return PersonalTaskReminderListOut(reminders=await personal_tasks.reminders_for_user(db, user))


@router.post("/reminders/{reminder_id}/read", response_model=PersonalTaskReminderOut)
async def read_task_reminder(
    reminder_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
) -> PersonalTaskReminderOut:
    return await personal_tasks.mark_reminder_read(db, user, reminder_id)


@router.get("/{task_id}", response_model=PersonalTaskOut)
async def get_task(task_id: uuid.UUID, db: DbSession, user: CurrentUser) -> PersonalTaskOut:
    task = await personal_tasks.get(db, user, task_id)
    return PersonalTaskOut.model_validate(task)


@router.post("/{task_id}/complete", response_model=PersonalTaskOut)
async def complete_task(
    task_id: uuid.UUID,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> PersonalTaskOut:
    task = await personal_tasks.complete(db, user, audit_context(request, user), task_id)
    return PersonalTaskOut.model_validate(task)


@router.post("/{task_id}/cancel", response_model=PersonalTaskOut)
async def cancel_task(
    task_id: uuid.UUID,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> PersonalTaskOut:
    task = await personal_tasks.cancel(db, user, audit_context(request, user), task_id)
    return PersonalTaskOut.model_validate(task)
