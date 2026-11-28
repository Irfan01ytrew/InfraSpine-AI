"""Universal Personal AI Companion OS (Step AN, slices 2–3): persistence + API service.

Every method acts only on the signed-in user's own rows: a lookup that finds
nothing — including a row owned by someone else — raises NotFoundError, so
another user's goals, tasks, preferences or digests are never revealed.

Digest sending (slice 3) is approval-gated end to end:
- queueing needs an active per-integration consent (grant/revoke in this file);
- sending needs a platform approval for the exact action
  (``companion.digest.send``, params = digest id + date) consumed through
  ``execute_once`` with duty ``companion.digest.send`` — a different user
  (an admin holding ``companion.digest.approve``) must approve first, and the
  approval is single-use and hash-bound;
- revoking consent cancels pending digests and blocks future sends;
- sends are refused during the user's quiet hours.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from email.message import EmailMessage
from email.utils import parseaddr

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.companion.briefing import (
    DailyBriefing,
    build_daily_briefing,
    in_quiet_hours,
    render_digest_subject,
    render_digest_text,
)
from app.companion.planner import (
    Goal,
    Preferences,
    ProgressRecord,
    TaskInput,
    build_plan,
    goal_progress,
    record_progress as planner_record_progress,
    validate_goal,
    validate_preferences,
    validate_task,
)
from app.core.config import Settings
from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.db.models import (
    CompanionConsent,
    CompanionDigestOutbox,
    CompanionGoal,
    CompanionPreference,
    CompanionProgress,
    CompanionProject,
    CompanionTask,
    User,
)
from app.platform.access import ensure_org_scope, principal_for
from app.platform.approvals import ActionSpec, request_approval
from app.platform.execution import VerifiedResult, execute_once
from app.platform.models import PlatformApprovalRequest
from app.services.audit import AuditContext, record as audit_record
from app.services.personal_task_email import deliver_message

DIGEST_INTEGRATION = "email_digest"
DIGEST_SCOPE = "daily_digest"
DIGEST_ACTION_TYPE = "companion.digest.send"
DIGEST_APPROVE_PERMISSION = "companion.digest.approve"


def _now() -> datetime:
    return datetime.now(UTC)


def _mailbox(value: str) -> str:
    if any(ord(character) < 32 for character in value):
        raise ValueError("Email addresses must not contain control characters.")
    _, address = parseaddr(value)
    if not address or "@" not in address or address.startswith("@") or address.endswith("@"):
        raise ValueError("A valid email mailbox is required.")
    return address


def build_digest_message(sender: str, recipient: str, digest: CompanionDigestOutbox) -> EmailMessage:
    """The digest email: subject + body already built from the owner's own data."""
    message = EmailMessage()
    message["From"] = _mailbox(sender)
    message["To"] = _mailbox(recipient)
    message["Subject"] = digest.subject
    message["Message-ID"] = f"<companion-digest-{digest.id}@infraspine.local>"
    message.set_content(digest.body)
    return message


def deliver_digest(settings: Settings, message: EmailMessage) -> None:
    """Send one digest over the configured SMTP transport.

    Tests monkeypatch this module attribute (``app.companion.service.deliver_digest``)
    with a recorder; production code never sends without the approval gate above.
    """
    deliver_message(settings, message)


def digest_sender(settings: Settings) -> str:
    """Configured From address for Companion digests (empty when unconfigured).

    Tests monkeypatch this module attribute to supply a sender.
    """
    return (settings.personal_task_email_from or "").strip()


def _digest_action_spec(scope_id: uuid.UUID, digest: CompanionDigestOutbox) -> ActionSpec:
    """The exact action a digest send performs — identical at queue and send time."""
    return ActionSpec(
        type=DIGEST_ACTION_TYPE,
        scope_id=scope_id,
        params={"digest_id": str(digest.id), "digest_date": digest.digest_date.isoformat()},
        subject=f"companion-digest:{digest.user_id}:{digest.digest_date.isoformat()}",
    )


class CompanionService:
    """Per-user Companion state. ``user`` is the signed-in user; nothing else is reachable."""

    def __init__(self, db: AsyncSession, user: User, ctx: AuditContext, settings: Settings) -> None:
        self.db = db
        self.user = user
        self.ctx = ctx
        self.settings = settings

    # ------------------------------------------------------------------ mapping

    @staticmethod
    def _goal_in(row: CompanionGoal, project_name: str | None) -> Goal:
        return Goal(
            id=str(row.id), user_id=str(row.user_id), title=row.title, status=row.status,
            priority=row.priority, deadline=row.deadline, project=project_name,
        )

    @staticmethod
    def _task_in(row: CompanionTask) -> TaskInput:
        return TaskInput(
            id=str(row.id), user_id=str(row.user_id), goal_id=str(row.goal_id), title=row.title,
            status=row.status, priority=row.priority, due=row.due,
            estimated_hours=row.estimated_hours, action_kind=row.action_kind,
        )

    @staticmethod
    def _record_in(row: CompanionProgress) -> ProgressRecord:
        return ProgressRecord(
            id=str(row.id), user_id=str(row.user_id), goal_id=str(row.goal_id),
            task_id=str(row.task_id) if row.task_id else None,
            percent=row.percent, note=row.note, on=row.on,
        )

    def _prefs_in(self, row: CompanionPreference | None) -> Preferences:
        # Server defaults only apply on INSERT; a new in-memory row still
        # carries None, so fall back to the planner defaults explicitly.
        if row is None:
            return Preferences(user_id=str(self.user.id))
        return Preferences(
            user_id=str(self.user.id),
            language=row.language if row.language is not None else "en",
            timezone=row.timezone if row.timezone is not None else "Asia/Karachi",
            weekly_capacity_hours=(
                row.weekly_capacity_hours if row.weekly_capacity_hours is not None else 40.0
            ),
            quiet_start=row.quiet_start if row.quiet_start is not None else time(22, 0),
            quiet_end=row.quiet_end if row.quiet_end is not None else time(7, 0),
            retention_days=row.retention_days if row.retention_days is not None else 90,
        )

    async def _load_inputs(self, today: date) -> tuple[list[Goal], list[TaskInput], Preferences, list[ProgressRecord]]:
        goals = list(await self.db.scalars(
            select(CompanionGoal).where(CompanionGoal.user_id == self.user.id).order_by(CompanionGoal.created_at)
        ))
        projects = {str(p.id): p.name for p in await self.db.scalars(
            select(CompanionProject).where(CompanionProject.user_id == self.user.id)
        )}
        tasks = list(await self.db.scalars(
            select(CompanionTask).where(CompanionTask.user_id == self.user.id).order_by(CompanionTask.created_at)
        ))
        records = list(await self.db.scalars(
            select(CompanionProgress).where(CompanionProgress.user_id == self.user.id).order_by(CompanionProgress.on)
        ))
        prefs_row = await self.db.get(CompanionPreference, self.user.id)
        return (
            [self._goal_in(g, projects.get(str(g.project_id))) for g in goals],
            [self._task_in(t) for t in tasks],
            self._prefs_in(prefs_row),
            [self._record_in(r) for r in records],
        )

    # ------------------------------------------------------------------ lookups (404, never leak)

    async def _goal_or_404(self, goal_id: uuid.UUID) -> CompanionGoal:
        row = await self.db.scalar(
            select(CompanionGoal).where(CompanionGoal.id == goal_id, CompanionGoal.user_id == self.user.id)
        )
        if row is None:
            raise NotFoundError("Goal not found.")
        return row

    async def _project_or_404(self, project_id: uuid.UUID) -> CompanionProject:
        row = await self.db.scalar(
            select(CompanionProject).where(CompanionProject.id == project_id, CompanionProject.user_id == self.user.id)
        )
        if row is None:
            raise NotFoundError("Project not found.")
        return row

    async def _task_or_404(self, task_id: uuid.UUID) -> CompanionTask:
        row = await self.db.scalar(
            select(CompanionTask).where(CompanionTask.id == task_id, CompanionTask.user_id == self.user.id)
        )
        if row is None:
            raise NotFoundError("Task not found.")
        return row

    async def _digest_or_404(self, digest_id: uuid.UUID) -> CompanionDigestOutbox:
        row = await self.db.scalar(
            select(CompanionDigestOutbox).where(
                CompanionDigestOutbox.id == digest_id, CompanionDigestOutbox.user_id == self.user.id
            )
        )
        if row is None:
            raise NotFoundError("Digest not found.")
        return row

    async def _consent_or_404(self, consent_id: uuid.UUID) -> CompanionConsent:
        row = await self.db.scalar(
            select(CompanionConsent).where(
                CompanionConsent.id == consent_id, CompanionConsent.user_id == self.user.id
            )
        )
        if row is None:
            raise NotFoundError("Consent not found.")
        return row

    # ------------------------------------------------------------------ preferences

    async def get_preferences(self) -> Preferences:
        return self._prefs_in(await self.db.get(CompanionPreference, self.user.id))

    async def put_preferences(
        self, *, language: str | None = None, timezone: str | None = None,
        weekly_capacity_hours: float | None = None, quiet_start: time | None = None,
        quiet_end: time | None = None, retention_days: int | None = None,
    ) -> CompanionPreference:
        row = await self.db.get(CompanionPreference, self.user.id)
        if row is None:
            row = CompanionPreference(user_id=self.user.id)
            self.db.add(row)
        if language is not None:
            row.language = language
        if timezone is not None:
            row.timezone = timezone
        if weekly_capacity_hours is not None:
            row.weekly_capacity_hours = weekly_capacity_hours
        if quiet_start is not None:
            row.quiet_start = quiet_start
        if quiet_end is not None:
            row.quiet_end = quiet_end
        if retention_days is not None:
            row.retention_days = retention_days
        row.updated_at = _now()
        try:
            validate_preferences(self._prefs_in(row))
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        await self.db.commit()
        await self.db.refresh(row)
        audit_record(self.db, self.ctx, "companion.preferences_updated", target=str(self.user.id))
        await self.db.commit()
        return row

    # ------------------------------------------------------------------ projects

    async def create_project(self, *, name: str, description: str | None = None) -> CompanionProject:
        if not name.strip() or len(name.strip()) > 120:
            raise InvalidInputError("Project name must be 1–120 characters.")
        row = CompanionProject(user_id=self.user.id, name=name.strip(), description=(description or "").strip() or None)
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        audit_record(self.db, self.ctx, "companion.project_created", target=str(row.id))
        await self.db.commit()
        return row

    async def list_projects(self) -> list[CompanionProject]:
        return list(await self.db.scalars(
            select(CompanionProject).where(CompanionProject.user_id == self.user.id)
            .order_by(CompanionProject.created_at)
        ))

    async def get_project(self, project_id: uuid.UUID) -> CompanionProject:
        return await self._project_or_404(project_id)

    async def update_project(
        self, project_id: uuid.UUID, *, name: str | None = None,
        description: str | None = None, status: str | None = None,
    ) -> CompanionProject:
        row = await self._project_or_404(project_id)
        if name is not None:
            if not name.strip() or len(name.strip()) > 120:
                raise InvalidInputError("Project name must be 1–120 characters.")
            row.name = name.strip()
        if description is not None:
            row.description = description.strip() or None
        if status is not None:
            if status not in {"active", "archived"}:
                raise InvalidInputError("Unknown project status.")
            row.status = status
        row.updated_at = _now()
        await self.db.commit()
        await self.db.refresh(row)
        return row

    # ------------------------------------------------------------------ goals

    async def create_goal(
        self, *, title: str, priority: str = "medium", deadline: date | None = None,
        project_id: uuid.UUID | None = None,
    ) -> CompanionGoal:
        if project_id is not None:
            await self._project_or_404(project_id)
        candidate = Goal(
            id=str(uuid.uuid4()), user_id=str(self.user.id), title=title.strip(),
            priority=priority, deadline=deadline,
        )
        try:
            validate_goal(candidate)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        row = CompanionGoal(
            user_id=self.user.id, project_id=project_id, title=candidate.title,
            status="active", priority=priority, deadline=deadline,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        audit_record(self.db, self.ctx, "companion.goal_created", target=str(row.id))
        await self.db.commit()
        return row

    async def list_goals(self) -> list[CompanionGoal]:
        return list(await self.db.scalars(
            select(CompanionGoal).where(CompanionGoal.user_id == self.user.id).order_by(CompanionGoal.created_at)
        ))

    async def get_goal(self, goal_id: uuid.UUID) -> CompanionGoal:
        return await self._goal_or_404(goal_id)

    async def update_goal(
        self, goal_id: uuid.UUID, *, title: str | None = None, priority: str | None = None,
        deadline: date | None = None, project_id: uuid.UUID | None = None,
        status: str | None = None, clear_deadline: bool = False, clear_project: bool = False,
    ) -> CompanionGoal:
        row = await self._goal_or_404(goal_id)
        if project_id is not None:
            await self._project_or_404(project_id)
            row.project_id = project_id
        if clear_project:
            row.project_id = None
        if title is not None:
            row.title = title.strip()
        if priority is not None:
            row.priority = priority
        if status is not None:
            row.status = status
        if clear_deadline:
            row.deadline = None
        elif deadline is not None:
            row.deadline = deadline
        row.updated_at = _now()
        try:
            validate_goal(self._goal_in(row, None))
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        await self.db.commit()
        await self.db.refresh(row)
        return row

    # ------------------------------------------------------------------ tasks

    async def create_task(
        self, *, goal_id: uuid.UUID, title: str, priority: str = "medium", due: date | None = None,
        estimated_hours: float = 0.0, action_kind: str = "internal",
    ) -> CompanionTask:
        await self._goal_or_404(goal_id)
        candidate = TaskInput(
            id=str(uuid.uuid4()), user_id=str(self.user.id), goal_id=str(goal_id),
            title=title.strip(), priority=priority, due=due,
            estimated_hours=estimated_hours, action_kind=action_kind,
        )
        try:
            validate_task(candidate)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        row = CompanionTask(
            user_id=self.user.id, goal_id=goal_id, title=candidate.title,
            priority=priority, due=due, estimated_hours=estimated_hours, action_kind=action_kind,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        audit_record(self.db, self.ctx, "companion.task_created", target=str(row.id))
        await self.db.commit()
        return row

    async def list_tasks(self, *, goal_id: uuid.UUID | None = None) -> list[CompanionTask]:
        query = select(CompanionTask).where(CompanionTask.user_id == self.user.id)
        if goal_id is not None:
            query = query.where(CompanionTask.goal_id == goal_id)
        return list(await self.db.scalars(query.order_by(CompanionTask.created_at)))

    async def get_task(self, task_id: uuid.UUID) -> CompanionTask:
        return await self._task_or_404(task_id)

    async def update_task(
        self, task_id: uuid.UUID, *, title: str | None = None, priority: str | None = None,
        due: date | None = None, estimated_hours: float | None = None,
        action_kind: str | None = None, status: str | None = None, clear_due: bool = False,
    ) -> CompanionTask:
        row = await self._task_or_404(task_id)
        if title is not None:
            row.title = title.strip()
        if priority is not None:
            row.priority = priority
        if status is not None:
            row.status = status
        if action_kind is not None:
            row.action_kind = action_kind
        if estimated_hours is not None:
            row.estimated_hours = estimated_hours
        if clear_due:
            row.due = None
        elif due is not None:
            row.due = due
        row.updated_at = _now()
        try:
            validate_task(self._task_in(row))
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        await self.db.commit()
        await self.db.refresh(row)
        return row

    # ------------------------------------------------------------------ progress (append-only)

    async def record_progress(
        self, *, goal_id: uuid.UUID, task_id: uuid.UUID | None = None,
        percent: int, note: str, on: date,
    ) -> CompanionProgress:
        await self._goal_or_404(goal_id)
        if task_id is not None:
            task = await self._task_or_404(task_id)
            if task.goal_id != goal_id:
                raise InvalidInputError("Task does not belong to this goal.")
        _, _, _, existing = await self._load_inputs(on)
        try:
            planner_record_progress(
                existing, record_id=str(uuid.uuid4()), user_id=str(self.user.id),
                goal_id=str(goal_id), task_id=str(task_id) if task_id else None,
                percent=percent, note=note, on=on,
            )
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        row = CompanionProgress(
            user_id=self.user.id, goal_id=goal_id, task_id=task_id,
            percent=percent, note=note, on=on,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        audit_record(self.db, self.ctx, "companion.progress_recorded", target=str(row.id),
                     details={"goal_id": str(goal_id), "percent": percent})
        await self.db.commit()
        return row

    async def list_progress(
        self, *, goal_id: uuid.UUID | None = None, task_id: uuid.UUID | None = None,
    ) -> list[CompanionProgress]:
        query = select(CompanionProgress).where(CompanionProgress.user_id == self.user.id)
        if goal_id is not None:
            query = query.where(CompanionProgress.goal_id == goal_id)
        if task_id is not None:
            query = query.where(CompanionProgress.task_id == task_id)
        return list(await self.db.scalars(query.order_by(CompanionProgress.on, CompanionProgress.created_at)))

    # ------------------------------------------------------------------ plan + briefing

    async def build_briefing(self, today: date) -> DailyBriefing:
        goals, tasks, prefs, records = await self._load_inputs(today)
        return build_daily_briefing(str(self.user.id), goals, tasks, prefs, records, today)

    async def plan_for(self, today: date):
        goals, tasks, prefs, _ = await self._load_inputs(today)
        return build_plan(str(self.user.id), goals, tasks, prefs, today)

    async def goal_progress_for(self, today: date):
        goals, tasks, _, records = await self._load_inputs(today)
        return goal_progress(str(self.user.id), goals, tasks, records, today)

    # ------------------------------------------------------------------ consent (slice 3)

    async def active_consent(self, integration: str) -> CompanionConsent | None:
        now = _now()
        return await self.db.scalar(
            select(CompanionConsent).where(
                CompanionConsent.user_id == self.user.id,
                CompanionConsent.integration == integration,
                CompanionConsent.revoked_at.is_(None),
                (CompanionConsent.expires_at.is_(None)) | (CompanionConsent.expires_at > now),
            ).order_by(CompanionConsent.granted_at.desc())
        )

    async def list_consents(self) -> list[CompanionConsent]:
        return list(await self.db.scalars(
            select(CompanionConsent).where(CompanionConsent.user_id == self.user.id)
            .order_by(CompanionConsent.granted_at.desc())
        ))

    async def grant_consent(
        self, *, integration: str, scope: str, expires_at: datetime | None = None,
    ) -> CompanionConsent:
        if integration != DIGEST_INTEGRATION:
            raise InvalidInputError(f"Unknown integration: {integration}")
        if not scope.strip() or len(scope.strip()) > 64:
            raise InvalidInputError("Consent scope must be 1–64 characters.")
        existing = await self.active_consent(integration)
        if existing is not None and existing.scope == scope.strip():
            return existing  # idempotent: consent already active
        if existing is not None:
            existing.revoked_at = _now()  # superseded by the new grant
        row = CompanionConsent(
            user_id=self.user.id, integration=integration, scope=scope.strip(), expires_at=expires_at,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        audit_record(self.db, self.ctx, "companion.consent_granted", target=str(row.id),
                     details={"integration": integration, "scope": row.scope})
        await self.db.commit()
        return row

    async def revoke_consent(self, consent_id: uuid.UUID) -> CompanionConsent:
        row = await self._consent_or_404(consent_id)
        if row.revoked_at is not None:
            return row
        row.revoked_at = _now()
        # Revoking stops future sends: cancel every pending digest for this user + integration.
        pending = list(await self.db.scalars(
            select(CompanionDigestOutbox).where(
                CompanionDigestOutbox.user_id == self.user.id,
                CompanionDigestOutbox.consent_id == consent_id,
                CompanionDigestOutbox.status == "pending",
            )
        ))
        for digest in pending:
            digest.status = "cancelled"
        await self.db.commit()
        await self.db.refresh(row)
        audit_record(self.db, self.ctx, "companion.consent_revoked", target=str(row.id),
                     details={"integration": row.integration, "cancelled_digests": len(pending)})
        await self.db.commit()
        return row

    # ------------------------------------------------------------------ digest outbox (slice 3)

    async def queue_digest(self, today: date) -> tuple[CompanionDigestOutbox, PlatformApprovalRequest]:
        consent = await self.active_consent(DIGEST_INTEGRATION)
        if consent is None:
            raise ConflictError("Grant email-digest consent before queueing a digest.")
        existing = await self.db.scalar(
            select(CompanionDigestOutbox).where(
                CompanionDigestOutbox.user_id == self.user.id,
                CompanionDigestOutbox.digest_date == today,
                CompanionDigestOutbox.status.in_(("pending", "sent")),
            )
        )
        if existing is not None:
            raise ConflictError(f"A digest for {today.isoformat()} already exists.")
        briefing = await self.build_briefing(today)
        row = CompanionDigestOutbox(
            user_id=self.user.id, consent_id=consent.id, digest_date=today,
            subject=render_digest_subject(briefing),
            body=render_digest_text(briefing, user_email=self.user.email),
            status="pending",
        )
        self.db.add(row)
        await self.db.flush()
        scope = await ensure_org_scope(self.db)
        spec = _digest_action_spec(scope.id, row)
        approval = await request_approval(
            self.db, principal_for(self.user), spec, required_permission=DIGEST_APPROVE_PERMISSION,
        )
        row.approval_id = approval.id
        await self.db.commit()
        await self.db.refresh(row)
        audit_record(self.db, self.ctx, "companion.digest_queued", target=str(row.id),
                     details={"digest_date": today.isoformat(), "approval_id": str(approval.id)})
        await self.db.commit()
        return row, approval

    async def list_digests(self) -> list[CompanionDigestOutbox]:
        return list(await self.db.scalars(
            select(CompanionDigestOutbox).where(CompanionDigestOutbox.user_id == self.user.id)
            .order_by(CompanionDigestOutbox.digest_date.desc())
        ))

    async def send_digest(
        self, digest_id: uuid.UUID, approval_id: uuid.UUID, *,
        now: datetime | None = None,
    ):
        """Send one queued digest. The approval is consumed exactly once via execute_once."""
        row = await self._digest_or_404(digest_id)
        if row.status != "pending":
            raise ConflictError(f"Digest is {row.status}; only a pending digest can be sent.")
        if await self.active_consent(DIGEST_INTEGRATION) is None:
            raise ConflictError("Email-digest consent is not active; the digest cannot be sent.")
        prefs = await self.get_preferences()
        moment = now or _now()
        if in_quiet_hours(moment, prefs.timezone, prefs.quiet_start, prefs.quiet_end):
            raise ConflictError("Digest sends are paused during quiet hours.")
        sender = digest_sender(self.settings)
        if not sender:
            raise ConflictError("Digest email sender is not configured.")
        scope = await ensure_org_scope(self.db)
        spec = _digest_action_spec(scope.id, row)
        message = build_digest_message(sender, self.user.email, row)

        async def dispatch() -> str:
            deliver_digest(self.settings, message)
            row.status = "sent"
            row.sent_at = _now()
            row.attempts += 1
            row.message_id = str(message["Message-ID"])
            return row.message_id

        async def verify(ref: str) -> VerifiedResult:
            fresh = await self.db.get(CompanionDigestOutbox, row.id)
            ok = fresh is not None and fresh.status == "sent" and fresh.message_id == ref
            return VerifiedResult(ok=ok, details={"digest_id": str(row.id), "status": fresh.status if fresh else None})

        result = await execute_once(
            self.db, principal_for(self.user), f"companion-digest-send:{row.id}", spec,
            duty=DIGEST_ACTION_TYPE, approval_id=approval_id, dispatch=dispatch, verify=verify,
        )
        audit_record(self.db, self.ctx, "companion.digest_sent", target=str(row.id),
                     details={"digest_date": row.digest_date.isoformat(), "message_id": row.message_id})
        await self.db.commit()
        return result

    # ------------------------------------------------------------------ retention + deletion

    async def purge_expired_progress(self, now: datetime | None = None) -> int:
        """Delete this user's progress records older than their retention setting."""
        prefs = await self.get_preferences()
        cutoff = (now or _now()) - timedelta(days=prefs.retention_days)
        result = await self.db.execute(
            delete(CompanionProgress).where(
                CompanionProgress.user_id == self.user.id, CompanionProgress.created_at < cutoff,
            )
        )
        await self.db.commit()
        return result.rowcount or 0

    async def delete_all(self) -> dict[str, int]:
        """Erase every Companion row for this user only (goals, tasks, progress, consents, digests)."""
        counts: dict[str, int] = {}
        for model, name in (
            (CompanionProgress, "progress"),
            (CompanionDigestOutbox, "digests"),
            (CompanionTask, "tasks"),
            (CompanionGoal, "goals"),
            (CompanionProject, "projects"),
            (CompanionConsent, "consents"),
            (CompanionPreference, "preferences"),
        ):
            result = await self.db.execute(delete(model).where(model.user_id == self.user.id))
            counts[name] = result.rowcount or 0
        await self.db.commit()
        audit_record(self.db, self.ctx, "companion.data_deleted", target=str(self.user.id), details=counts)
        await self.db.commit()
        return counts
