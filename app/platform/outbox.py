"""Outbox dispatcher: delivers platform_outbox events to registered consumers with retry, backoff and dead-letter.

* Several dispatchers may run at once (API index owner and the standalone worker): rows are claimed with
  FOR UPDATE SKIP LOCKED, so each event is handled by one dispatcher at a time.
* Each event's handlers run inside a savepoint: a failing handler rolls back only its own writes; the event keeps
  its attempt count, error and next attempt time, and is retried with exponential backoff and jitter.
* After MAX_ATTEMPTS failures the event is dead-lettered (dead_at set) and kept for inspection/requeue.
* An event type with no consumer is marked delivered (nothing to do), so the outbox never grows unbounded.
Delivery is at-least-once: handlers must be idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.platform.models import PlatformOutbox

logger = logging.getLogger("app.platform.outbox")

Handler = Callable[[AsyncSession, PlatformOutbox], Awaitable[None]]
HANDLERS: dict[str, list[Handler]] = {}
MAX_ATTEMPTS = 8
BASE_DELAY_SECONDS = 5.0
MAX_DELAY_SECONDS = 3600.0
POLL_SECONDS = 2.0


def register(event_type: str) -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:
        if fn not in HANDLERS.setdefault(event_type, []):
            HANDLERS[event_type].append(fn)
        return fn
    return decorator


def backoff(attempts: int, *, jitter: bool = True) -> timedelta:
    """5 s, 10 s, 20 s … capped at 1 h, plus up to 20 % jitter so failing events do not retry in lockstep."""
    delay = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * (2 ** max(0, attempts - 1)))
    if jitter:
        delay *= 1 + random.uniform(0, 0.2)
    return timedelta(seconds=delay)


def _now() -> datetime:
    return datetime.now(UTC)


async def deliver_batch(db: AsyncSession, handlers: dict[str, list[Handler]] | None = None, *, limit: int = 50,
                        now: datetime | None = None) -> dict[str, int]:
    """Claim up to `limit` due events in this session, run their handlers, record the outcome and commit."""
    _load_default_handlers()
    registry = HANDLERS if handlers is None else handlers
    now = now or _now()
    rows = list(await db.scalars(
        select(PlatformOutbox)
        .where(PlatformOutbox.dispatched_at.is_(None), PlatformOutbox.dead_at.is_(None),
               PlatformOutbox.next_attempt_at <= now)
        .order_by(PlatformOutbox.next_attempt_at, PlatformOutbox.id).limit(limit)
        .with_for_update(skip_locked=True)
    ))
    counts = {"delivered": 0, "retrying": 0, "dead": 0}
    for row in rows:
        row.attempts += 1
        try:
            async with db.begin_nested():
                for handler in registry.get(row.event_type, []):
                    await handler(db, row)
        except Exception as exc:  # the event is never lost: record, back off, retry or dead-letter
            row.last_error = f"{type(exc).__name__}: {exc}"[:500]
            if row.attempts >= MAX_ATTEMPTS:
                row.dead_at = now
                counts["dead"] += 1
                logger.error("Outbox event %s (%s) dead-lettered after %s attempts", row.id, row.event_type, row.attempts)
            else:
                row.next_attempt_at = now + backoff(row.attempts)
                counts["retrying"] += 1
                logger.warning("Outbox event %s (%s) failed attempt %s: %s", row.id, row.event_type, row.attempts,
                               type(exc).__name__)
            continue
        row.dispatched_at = now
        row.last_error = None
        counts["delivered"] += 1
    await db.commit()
    return counts


async def drain_until_empty(sessionmaker: async_sessionmaker[AsyncSession], *, limit: int = 50,
                            max_batches: int = 20) -> dict[str, int]:
    total = {"delivered": 0, "retrying": 0, "dead": 0}
    for _ in range(max_batches):
        async with sessionmaker() as db:
            counts = await deliver_batch(db, limit=limit)
        for key, value in counts.items():
            total[key] += value
        if sum(counts.values()) < limit:
            break
    return total


async def requeue_dead(db: AsyncSession, event_ids: list[int] | None = None) -> int:
    """Operator action: give dead-lettered events a fresh set of attempts."""
    statement = update(PlatformOutbox).where(PlatformOutbox.dead_at.is_not(None), PlatformOutbox.dispatched_at.is_(None))
    if event_ids:
        statement = statement.where(PlatformOutbox.id.in_(event_ids))
    result = await db.execute(statement.values(dead_at=None, attempts=0, next_attempt_at=_now()))
    await db.commit()
    return result.rowcount or 0


async def outbox_health(db: AsyncSession) -> dict:
    from sqlalchemy import func
    pending = await db.scalar(select(func.count()).where(PlatformOutbox.dispatched_at.is_(None), PlatformOutbox.dead_at.is_(None)))
    dead = await db.scalar(select(func.count()).where(PlatformOutbox.dead_at.is_not(None), PlatformOutbox.dispatched_at.is_(None)))
    oldest = await db.scalar(select(func.min(PlatformOutbox.created_at)).where(
        PlatformOutbox.dispatched_at.is_(None), or_(PlatformOutbox.dead_at.is_(None))))
    return {"pending": pending or 0, "dead_lettered": dead or 0, "oldest_pending_at": oldest.isoformat() if oldest else None}


async def run_outbox_worker(sessionmaker: async_sessionmaker[AsyncSession], stop: asyncio.Event, *,
                            interval_seconds: float = POLL_SECONDS) -> None:
    """Background dispatcher. A failed poll (database restart, network) is logged and retried on the next tick."""
    while not stop.is_set():
        busy = False
        try:
            counts = await drain_until_empty(sessionmaker)
            busy = sum(counts.values()) > 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Outbox dispatcher poll failed (%s)", type(exc).__name__)
        if busy:
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


_defaults_loaded = False


def _load_default_handlers() -> None:
    global _defaults_loaded
    if not _defaults_loaded:
        _defaults_loaded = True
        import app.platform.handlers  # noqa: F401  (registers the built-in consumers)



def main() -> None:
    """python -m app.platform.outbox  — run the dispatcher until stopped;  --once  drains due events and exits."""
    import argparse
    import os
    import signal

    parser = argparse.ArgumentParser(prog="python -m app.platform.outbox", description="Platform outbox dispatcher")
    parser.add_argument("--once", action="store_true", help="deliver every due event, then exit")
    args = parser.parse_args()

    async def _run() -> None:
        from app.core.config import get_settings
        from app.db.session import create_engine, create_sessionmaker

        settings = get_settings()
        engine = create_engine(settings.database_url.get_secret_value())
        sessionmaker = create_sessionmaker(engine)
        try:
            if args.once:
                counts = await drain_until_empty(sessionmaker)
                print(counts)
                return
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, stop.set)
            await run_outbox_worker(sessionmaker, stop, interval_seconds=settings.platform_outbox_poll_seconds)
        finally:
            await engine.dispose()

    import contextlib
    asyncio.run(_run())


if __name__ == "__main__":
    main()
