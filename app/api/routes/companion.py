"""Personal AI Companion OS API (Step AN, slices 2–3).

Every route acts only on the signed-in user's own Companion rows; another
user's goals, tasks, preferences or digests return 404, never 403-with-
existence. Reads need ``companion.read``, writes need ``companion.write``
(organisation-wide platform permissions, every active member holds them).
The email digest is approval-gated: queueing creates an exact-action approval
an admin must decide, and sending consumes it exactly once.
"""

import uuid
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.deps import AppSettings, CurrentUser, DbSession, audit_context, get_current_user
from app.companion.service import CompanionService
from app.core.errors import InvalidInputError
from app.db.models import CompanionGoal, CompanionProject, CompanionTask
from app.platform.modules import org_gate, raise_unless
from app.schemas.companion import (
    BriefingOut,
    ConsentCreate,
    ConsentOut,
    DeleteAllOut,
    DigestOut,
    DigestQueueOut,
    DigestSendIn,
    GoalCreate,
    GoalOut,
    GoalProgressOut,
    GoalUpdate,
    PlanOut,
    PlannedTaskOut,
    PreferencesIn,
    PreferencesOut,
    ProgressCreate,
    ProgressOut,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    TaskCreate,
    TaskOut,
    TaskUpdate,
)

router = APIRouter(prefix="/api/companion", tags=["companion"], dependencies=[Depends(get_current_user)])


def get_service(request: Request, db: DbSession, settings: AppSettings, user: CurrentUser) -> CompanionService:
    return CompanionService(db, user, audit_context(request, user), settings)


Service = Annotated[CompanionService, Depends(get_service)]


async def _read_gate(db: DbSession, user: CurrentUser) -> None:
    raise_unless(await org_gate(db, user, "companion.read", legacy_allowed=True))


async def _write_gate(db: DbSession, user: CurrentUser) -> None:
    raise_unless(await org_gate(db, user, "companion.write", legacy_allowed=True))


def _today(on: date | None, tz: str = "UTC") -> date:
    if on is not None:
        return on
    return datetime.now(UTC).date()


def _goal_out(row: CompanionGoal) -> GoalOut:
    return GoalOut(
        id=row.id, project_id=row.project_id, title=row.title, status=row.status,
        priority=row.priority, deadline=row.deadline, created_at=row.created_at,
    )


def _project_out(row: CompanionProject) -> ProjectOut:
    return ProjectOut(
        id=row.id, name=row.name, description=row.description,
        status=row.status, created_at=row.created_at,
    )


def _task_out(row: CompanionTask) -> TaskOut:
    return TaskOut(
        id=row.id, goal_id=row.goal_id, title=row.title, status=row.status,
        priority=row.priority, due=row.due, estimated_hours=row.estimated_hours,
        action_kind=row.action_kind, created_at=row.created_at,
    )


def _plan_out(plan) -> PlanOut:
    return PlanOut(
        on=plan.on,
        tasks=[
            PlannedTaskOut(
                task_id=item.task.id, title=item.task.title, rank=item.rank,
                overdue_days=item.overdue_days, needs_approval=item.needs_approval,
                reason=item.reason, due=item.task.due, estimated_hours=item.task.estimated_hours,
            )
            for item in plan.tasks
        ],
        overdue_count=plan.overdue_count,
        total_estimated_hours=plan.total_estimated_hours,
        overloaded=plan.overloaded,
        warnings=list(plan.warnings),
    )


# ------------------------------------------------------------------ briefing + plan


@router.get("/briefing", response_model=BriefingOut)
async def get_briefing(
    service: Service, db: DbSession, user: CurrentUser, on: Annotated[date | None, Query()] = None,
) -> BriefingOut:
    await _read_gate(db, user)
    today = _today(on)
    briefing = await service.build_briefing(today)
    goals = await service.goal_progress_for(today)
    titles = {str(g.id): g.title for g in await service.list_goals()}
    return BriefingOut(
        on=briefing.on,
        plan=_plan_out(briefing.plan),
        goals=[
            GoalProgressOut(
                goal_id=uuid.UUID(summary.goal_id), title=titles.get(summary.goal_id, summary.goal_id),
                tasks_total=summary.tasks_total, tasks_done=summary.tasks_done,
                percent=summary.percent, last_progress_on=summary.last_progress_on,
                stalled=summary.stalled,
            )
            for summary in goals
        ],
        stalled_goal_ids=[uuid.UUID(goal_id) for goal_id in briefing.stalled_goal_ids],
        leave_off=briefing.leave_off,
    )


@router.get("/plan", response_model=PlanOut)
async def get_plan(
    service: Service, db: DbSession, user: CurrentUser, on: Annotated[date | None, Query()] = None,
) -> PlanOut:
    await _read_gate(db, user)
    return _plan_out(await service.plan_for(_today(on)))


# ------------------------------------------------------------------ preferences


@router.get("/preferences", response_model=PreferencesOut)
async def get_preferences(service: Service, db: DbSession, user: CurrentUser) -> PreferencesOut:
    await _read_gate(db, user)
    prefs = await service.get_preferences()
    return PreferencesOut(
        language=prefs.language, timezone=prefs.timezone,
        weekly_capacity_hours=prefs.weekly_capacity_hours, quiet_start=prefs.quiet_start,
        quiet_end=prefs.quiet_end, retention_days=prefs.retention_days,
    )


@router.put("/preferences", response_model=PreferencesOut)
async def put_preferences(
    payload: PreferencesIn, service: Service, db: DbSession, user: CurrentUser,
) -> PreferencesOut:
    await _write_gate(db, user)
    row = await service.put_preferences(
        language=payload.language, timezone=payload.timezone,
        weekly_capacity_hours=payload.weekly_capacity_hours, quiet_start=payload.quiet_start,
        quiet_end=payload.quiet_end, retention_days=payload.retention_days,
    )
    return PreferencesOut(
        language=row.language, timezone=row.timezone,
        weekly_capacity_hours=row.weekly_capacity_hours, quiet_start=row.quiet_start,
        quiet_end=row.quiet_end, retention_days=row.retention_days,
    )


# ------------------------------------------------------------------ projects


@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(payload: ProjectCreate, service: Service, db: DbSession, user: CurrentUser) -> ProjectOut:
    await _write_gate(db, user)
    return _project_out(await service.create_project(name=payload.name, description=payload.description))


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(service: Service, db: DbSession, user: CurrentUser) -> list[ProjectOut]:
    await _read_gate(db, user)
    return [_project_out(row) for row in await service.list_projects()]


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(project_id: uuid.UUID, service: Service, db: DbSession, user: CurrentUser) -> ProjectOut:
    await _read_gate(db, user)
    return _project_out(await service.get_project(project_id))


@router.patch("/projects/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID, payload: ProjectUpdate, service: Service, db: DbSession, user: CurrentUser,
) -> ProjectOut:
    await _write_gate(db, user)
    return _project_out(await service.update_project(
        project_id, name=payload.name, description=payload.description, status=payload.status,
    ))


# ------------------------------------------------------------------ goals


@router.post("/goals", response_model=GoalOut, status_code=status.HTTP_201_CREATED)
async def create_goal(payload: GoalCreate, service: Service, db: DbSession, user: CurrentUser) -> GoalOut:
    await _write_gate(db, user)
    return _goal_out(await service.create_goal(
        title=payload.title, priority=payload.priority,
        deadline=payload.deadline, project_id=payload.project_id,
    ))


@router.get("/goals", response_model=list[GoalOut])
async def list_goals(service: Service, db: DbSession, user: CurrentUser) -> list[GoalOut]:
    await _read_gate(db, user)
    return [_goal_out(row) for row in await service.list_goals()]


@router.get("/goals/{goal_id}", response_model=GoalOut)
async def get_goal(goal_id: uuid.UUID, service: Service, db: DbSession, user: CurrentUser) -> GoalOut:
    await _read_gate(db, user)
    return _goal_out(await service.get_goal(goal_id))


@router.patch("/goals/{goal_id}", response_model=GoalOut)
async def update_goal(
    goal_id: uuid.UUID, payload: GoalUpdate, service: Service, db: DbSession, user: CurrentUser,
) -> GoalOut:
    await _write_gate(db, user)
    return _goal_out(await service.update_goal(
        goal_id, title=payload.title, priority=payload.priority, deadline=payload.deadline,
        project_id=payload.project_id, status=payload.status,
        clear_deadline=payload.clear_deadline, clear_project=payload.clear_project,
    ))


# ------------------------------------------------------------------ tasks


@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(payload: TaskCreate, service: Service, db: DbSession, user: CurrentUser) -> TaskOut:
    await _write_gate(db, user)
    return _task_out(await service.create_task(
        goal_id=payload.goal_id, title=payload.title, priority=payload.priority,
        due=payload.due, estimated_hours=payload.estimated_hours, action_kind=payload.action_kind,
    ))


@router.get("/tasks", response_model=list[TaskOut])
async def list_tasks(
    service: Service, db: DbSession, user: CurrentUser,
    goal_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[TaskOut]:
    await _read_gate(db, user)
    return [_task_out(row) for row in await service.list_tasks(goal_id=goal_id)]


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: uuid.UUID, service: Service, db: DbSession, user: CurrentUser) -> TaskOut:
    await _read_gate(db, user)
    return _task_out(await service.get_task(task_id))


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: uuid.UUID, payload: TaskUpdate, service: Service, db: DbSession, user: CurrentUser,
) -> TaskOut:
    await _write_gate(db, user)
    return _task_out(await service.update_task(
        task_id, title=payload.title, priority=payload.priority, due=payload.due,
        estimated_hours=payload.estimated_hours, action_kind=payload.action_kind,
        status=payload.status, clear_due=payload.clear_due,
    ))


# ------------------------------------------------------------------ progress (append-only)


@router.post("/progress", response_model=ProgressOut, status_code=status.HTTP_201_CREATED)
async def record_progress(payload: ProgressCreate, service: Service, db: DbSession, user: CurrentUser) -> ProgressOut:
    await _write_gate(db, user)
    row = await service.record_progress(
        goal_id=payload.goal_id, task_id=payload.task_id,
        percent=payload.percent, note=payload.note, on=payload.on,
    )
    return ProgressOut(
        id=row.id, goal_id=row.goal_id, task_id=row.task_id, percent=row.percent,
        note=row.note, on=row.on, created_at=row.created_at,
    )


@router.get("/progress", response_model=list[ProgressOut])
async def list_progress(
    service: Service, db: DbSession, user: CurrentUser,
    goal_id: Annotated[uuid.UUID | None, Query()] = None,
    task_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[ProgressOut]:
    await _read_gate(db, user)
    return [
        ProgressOut(
            id=row.id, goal_id=row.goal_id, task_id=row.task_id, percent=row.percent,
            note=row.note, on=row.on, created_at=row.created_at,
        )
        for row in await service.list_progress(goal_id=goal_id, task_id=task_id)
    ]


# ------------------------------------------------------------------ consent + digest (slice 3)


@router.get("/consents", response_model=list[ConsentOut])
async def list_consents(service: Service, db: DbSession, user: CurrentUser) -> list[ConsentOut]:
    await _read_gate(db, user)
    now = datetime.now(UTC)
    return [
        ConsentOut(
            id=row.id, integration=row.integration, scope=row.scope, granted_at=row.granted_at,
            expires_at=row.expires_at, revoked_at=row.revoked_at,
            active=row.revoked_at is None and (row.expires_at is None or row.expires_at > now),
        )
        for row in await service.list_consents()
    ]


@router.post("/consents", response_model=ConsentOut, status_code=status.HTTP_201_CREATED)
async def grant_consent(payload: ConsentCreate, service: Service, db: DbSession, user: CurrentUser) -> ConsentOut:
    await _write_gate(db, user)
    row = await service.grant_consent(
        integration=payload.integration, scope=payload.scope, expires_at=payload.expires_at,
    )
    now = datetime.now(UTC)
    return ConsentOut(
        id=row.id, integration=row.integration, scope=row.scope, granted_at=row.granted_at,
        expires_at=row.expires_at, revoked_at=row.revoked_at,
        active=row.revoked_at is None and (row.expires_at is None or row.expires_at > now),
    )


@router.post("/consents/{consent_id}/revoke", response_model=ConsentOut)
async def revoke_consent(consent_id: uuid.UUID, service: Service, db: DbSession, user: CurrentUser) -> ConsentOut:
    await _write_gate(db, user)
    row = await service.revoke_consent(consent_id)
    now = datetime.now(UTC)
    return ConsentOut(
        id=row.id, integration=row.integration, scope=row.scope, granted_at=row.granted_at,
        expires_at=row.expires_at, revoked_at=row.revoked_at,
        active=row.revoked_at is None and (row.expires_at is None or row.expires_at > now),
    )


@router.post("/digest/queue", response_model=DigestQueueOut, status_code=status.HTTP_201_CREATED)
async def queue_digest(
    service: Service, db: DbSession, user: CurrentUser, on: Annotated[date | None, Query()] = None,
) -> DigestQueueOut:
    await _write_gate(db, user)
    row, approval = await service.queue_digest(_today(on))
    return DigestQueueOut(
        id=row.id, digest_date=row.digest_date, subject=row.subject,
        status=row.status, approval_id=approval.id, action_hash=approval.action_hash,
    )


@router.get("/digest", response_model=list[DigestOut])
async def list_digests(service: Service, db: DbSession, user: CurrentUser) -> list[DigestOut]:
    await _read_gate(db, user)
    return [
        DigestOut(
            id=row.id, digest_date=row.digest_date, subject=row.subject, status=row.status,
            approval_id=row.approval_id, message_id=row.message_id,
            created_at=row.created_at, sent_at=row.sent_at,
        )
        for row in await service.list_digests()
    ]


@router.post("/digest/{digest_id}/send", response_model=DigestOut)
async def send_digest(
    digest_id: uuid.UUID, payload: DigestSendIn, service: Service, db: DbSession, user: CurrentUser,
) -> DigestOut:
    await _write_gate(db, user)
    await service.send_digest(digest_id, payload.approval_id)
    row = await service._digest_or_404(digest_id)
    return DigestOut(
        id=row.id, digest_date=row.digest_date, subject=row.subject, status=row.status,
        approval_id=row.approval_id, message_id=row.message_id,
        created_at=row.created_at, sent_at=row.sent_at,
    )


# ------------------------------------------------------------------ delete my Companion data


@router.delete("/data", response_model=DeleteAllOut)
async def delete_my_data(
    service: Service, db: DbSession, user: CurrentUser,
    confirm: Annotated[bool, Query()] = False,
) -> DeleteAllOut:
    await _write_gate(db, user)
    if not confirm:
        raise InvalidInputError("Add ?confirm=true to delete all of your Companion data.")
    counts = await service.delete_all()
    return DeleteAllOut(**counts)
