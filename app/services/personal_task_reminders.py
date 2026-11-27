"""Idempotent scheduled in-app reminders for personal assignment deadlines."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, exists, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.models import PersonalTask, PersonalTaskReminder, User
from app.services.personal_task_email import send_personal_task_reminder_email

logger = logging.getLogger(__name__)

REMINDER_WINDOW = timedelta(days=3)
REMINDER_POLL_SECONDS = 3600
BATCH_SIZE = 500
EMAIL_BATCH_SIZE = 50
EMAIL_MAX_ATTEMPTS = 8
EMAIL_CLAIM_SECONDS = 300


def reminder_type_for_due_at(due_at: datetime, now: datetime) -> str:
    """Return the stable reminder class used by the scheduler, requiring aware timestamps."""
    if due_at.utcoffset() is None or now.utcoffset() is None:
        raise ValueError("Task reminders require timezone-aware timestamps.")
    return "overdue" if due_at <= now else "due_soon"


async def process_personal_task_reminders(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> int:
    """Create due-soon/overdue notices once per task type; safe across concurrent workers."""
    settings = settings or get_settings()
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("The reminder scheduler requires a timezone-aware timestamp.")
    reminder_type = case((PersonalTask.due_at <= current, "overdue"), else_="due_soon")
    async with sessionmaker() as db:
        rows = list(
            (
                await db.scalars(
                    select(PersonalTask)
                    .where(
                        PersonalTask.status == "open",
                        PersonalTask.due_at.is_not(None),
                        PersonalTask.due_at <= current + REMINDER_WINDOW,
                        ~exists(
                            select(PersonalTaskReminder.id).where(
                                PersonalTaskReminder.task_id == PersonalTask.id,
                                PersonalTaskReminder.reminder_type == reminder_type,
                            )
                        ),
                    )
                    .order_by(PersonalTask.due_at, PersonalTask.id)
                    .with_for_update(skip_locked=True, of=PersonalTask)
                    .limit(BATCH_SIZE)
                )
            ).all()
        )
        created = 0
        for task in rows:
            assert task.due_at is not None
            task_reminder_type = reminder_type_for_due_at(task.due_at, current)
            result = await db.execute(
                insert(PersonalTaskReminder)
                .values(
                    id=uuid.uuid4(),
                    task_id=task.id,
                    recipient_user_id=task.assignee_user_id,
                    reminder_type=task_reminder_type,
                    due_at=task.due_at,
                    email_status="pending" if settings.personal_task_email_enabled else "disabled",
                )
                .on_conflict_do_nothing(constraint="uq_personal_task_reminders_task_type")
            )
            created += max(0, result.rowcount or 0)
        await db.commit()
    await deliver_pending_personal_task_reminder_emails(sessionmaker, settings=settings, now=current)
    return created


async def deliver_pending_personal_task_reminder_emails(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    now: datetime | None = None,
) -> int:
    """Claim and send a bounded outbox batch, persisting retries without task or address logs."""
    if not settings.personal_task_email_enabled:
        return 0
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("The reminder email dispatcher requires a timezone-aware timestamp.")
    reclaim = and_(
        PersonalTaskReminder.email_status == "sending",
        PersonalTaskReminder.email_claimed_until <= current,
    )
    pending = and_(
        PersonalTaskReminder.email_status.in_(("pending", "retry")),
        or_(PersonalTaskReminder.email_next_attempt_at.is_(None), PersonalTaskReminder.email_next_attempt_at <= current),
    )
    async with sessionmaker() as db:
        rows = list(
            (
                await db.execute(
                    select(PersonalTaskReminder, User.email, PersonalTask.status)
                    .join(User, User.id == PersonalTaskReminder.recipient_user_id)
                    .join(PersonalTask, PersonalTask.id == PersonalTaskReminder.task_id)
                    .where(or_(pending, reclaim))
                    .order_by(PersonalTaskReminder.created_at, PersonalTaskReminder.id)
                    .with_for_update(skip_locked=True, of=PersonalTaskReminder)
                    .limit(EMAIL_BATCH_SIZE)
                )
            ).all()
        )
        claimed: list[tuple[uuid.UUID, str, int]] = []
        for reminder, recipient, task_status in rows:
            if reminder.email_attempts >= EMAIL_MAX_ATTEMPTS:
                reminder.email_status = "failed"
                reminder.email_claimed_until = None
                reminder.email_next_attempt_at = None
                continue
            reminder.email_claimed_until = current + timedelta(seconds=EMAIL_CLAIM_SECONDS)
            if task_status != "open":
                reminder.email_status = "cancelled"
                reminder.email_claimed_until = None
                continue
            reminder.email_status = "sending"
            reminder.email_attempts += 1
            claimed.append((reminder.id, recipient, reminder.email_attempts))
        await db.commit()

    sent = 0
    for reminder_id, recipient, attempt in claimed:
        try:
            await asyncio.to_thread(send_personal_task_reminder_email, settings, recipient, reminder_id)
        except Exception as exc:
            retry_seconds = min(6 * 60 * 60, 15 * (2 ** min(attempt - 1, 10)))
            async with sessionmaker() as db:
                reminder = await db.get(PersonalTaskReminder, reminder_id, with_for_update=True)
                if reminder is not None and reminder.email_status == "sending":
                    reminder.email_status = "retry" if attempt < EMAIL_MAX_ATTEMPTS else "failed"
                    reminder.email_last_error_code = type(exc).__name__[:64]
                    reminder.email_next_attempt_at = (
                        current + timedelta(seconds=retry_seconds) if attempt < EMAIL_MAX_ATTEMPTS else None
                    )
                    reminder.email_claimed_until = None
                    await db.commit()
            logger.warning("Personal task reminder email delivery failed (%s)", type(exc).__name__)
            continue
        async with sessionmaker() as db:
            reminder = await db.get(PersonalTaskReminder, reminder_id, with_for_update=True)
            if reminder is not None and reminder.email_status == "sending":
                reminder.email_status = "sent"
                reminder.email_sent_at = current
                reminder.email_next_attempt_at = None
                reminder.email_claimed_until = None
                reminder.email_last_error_code = None
                await db.commit()
                sent += 1
    return sent


async def run_personal_task_reminders(
    sessionmaker: async_sessionmaker[AsyncSession],
    stop: asyncio.Event,
    *,
    interval_seconds: float = REMINDER_POLL_SECONDS,
    settings: Settings | None = None,
) -> None:
    """Poll periodically; transient failures are retried on the next tick without logging task data."""
    while not stop.is_set():
        try:
            await process_personal_task_reminders(sessionmaker, settings=settings)
        except Exception as exc:
            logger.warning("Personal task reminder scheduler failed (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
