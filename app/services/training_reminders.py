"""Idempotent scheduled reminders for in-app employee training assignments."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import TrainingAssignment, TrainingReminder

logger = logging.getLogger(__name__)

REMINDER_WINDOW = timedelta(days=3)
REMINDER_POLL_SECONDS = 3600


async def process_training_reminders(
    sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime | None = None
) -> int:
    """Create one due-soon or overdue reminder per assignment. Safe across concurrent worker processes."""
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("The reminder scheduler requires a timezone-aware timestamp.")
    created = 0
    async with sessionmaker() as db:
        rows = list(
            (
                await db.execute(
                    select(TrainingAssignment)
                    .where(
                        TrainingAssignment.status == "assigned",
                        TrainingAssignment.due_at.is_not(None),
                        TrainingAssignment.due_at <= current + REMINDER_WINDOW,
                        ~exists(
                            select(TrainingReminder.id).where(
                                TrainingReminder.assignment_id == TrainingAssignment.id,
                                TrainingReminder.reminder_type
                                == case((TrainingAssignment.due_at <= current, "overdue"), else_="due_soon"),
                            )
                        ),
                    )
                    .order_by(TrainingAssignment.due_at, TrainingAssignment.id)
                    .with_for_update(skip_locked=True)
                    .limit(500)
                )
            ).scalars()
        )
        for assignment in rows:
            reminder_type = "overdue" if assignment.due_at <= current else "due_soon"
            result = await db.execute(
                insert(TrainingReminder)
                .values(
                    id=uuid.uuid4(),
                    assignment_id=assignment.id,
                    recipient_user_id=assignment.assignee_user_id,
                    reminder_type=reminder_type,
                    due_at=assignment.due_at,
                )
                .on_conflict_do_nothing(constraint="uq_training_reminders_assignment_type")
            )
            created += max(0, result.rowcount or 0)
        await db.commit()
    return created


async def run_training_reminders(
    sessionmaker: async_sessionmaker[AsyncSession],
    stop: asyncio.Event,
    *,
    interval_seconds: float = REMINDER_POLL_SECONDS,
) -> None:
    """Background scheduler; retries on the next tick and logs only exception types."""
    while not stop.is_set():
        try:
            await process_training_reminders(sessionmaker)
        except Exception as exc:
            logger.warning("Training reminder scheduler failed (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
