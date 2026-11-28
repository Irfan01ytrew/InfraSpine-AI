"""Salary-reminder scheduler for the document worker (Step AG slice 5).

Mirrors :mod:`app.services.personal_task_reminders`: ``process_*`` runs one
cycle (``--once``), ``run_*`` polls until stopped. A cycle only *creates*
reminder rows (see :mod:`app.hr.scheduler`); it never sends anything and
never moves money. Delivery (WhatsApp/email) is Step AA's scheduled-report
delivery, which is not on this branch.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.hr.scheduler import run_salary_reminder_cycle
from app.platform.timezone import org_today

logger = logging.getLogger(__name__)

# Salary reminders are due once a day; hourly polling is plenty.
SALARY_REMINDER_POLL_SECONDS = 3600.0


async def process_salary_reminders(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    today=None,
) -> int:
    """Create due salary reminders across all departments. Returns rows created."""
    async with sessionmaker() as db:
        current = today if today is not None else await org_today(db)
        result = await run_salary_reminder_cycle(db, current)
    return result["created"]


async def run_salary_reminders(
    sessionmaker: async_sessionmaker[AsyncSession],
    stop: asyncio.Event,
    *,
    interval_seconds: float = SALARY_REMINDER_POLL_SECONDS,
) -> None:
    """Poll periodically; transient failures are retried on the next tick."""
    while not stop.is_set():
        try:
            await process_salary_reminders(sessionmaker)
        except Exception as exc:
            logger.warning("Salary reminder scheduler failed (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
