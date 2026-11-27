"""Outbox dispatcher: retry with backoff, dead-letter, SKIP LOCKED, idempotent handlers, worker loop."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import platform
from app.db.session import test_engine_options as _test_engine_options
from app.platform.models import PlatformOutbox
from app.platform.outbox import (
    MAX_ATTEMPTS,
    backoff,
    deliver_batch,
    drain_until_empty,
    outbox_health,
    requeue_dead,
    run_outbox_worker,
)
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.usefixtures("clean_tables")


def run(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


async def _publish(db, event_type, payload, *, when=None):
    row = PlatformOutbox(event_type=event_type, payload=payload, trace_id=payload.get("trace") or __import__("uuid").uuid4())
    if when is not None:
        row.next_attempt_at = when
    db.add(row)
    await db.flush()
    return row


def test_backoff_grows_and_is_capped():
    first = backoff(1, jitter=False)
    second = backoff(2, jitter=False)
    late = backoff(20, jitter=False)
    assert first == timedelta(seconds=5)
    assert second == timedelta(seconds=10)
    assert late == timedelta(seconds=3600)
    jittered = [backoff(3, jitter=True).total_seconds() for _ in range(20)]
    assert all(20 <= value <= 24 for value in jittered)


def test_deliver_success_and_events_with_no_handler_are_marked_delivered():
    def body(sm):
        async def go():
            seen = []

            async def handler(_db, row):
                seen.append(row.payload["n"])

            async with sm() as db:
                await _publish(db, "test.ok", {"n": 1})
                await _publish(db, "test.ok", {"n": 2})
                await _publish(db, "test.nobody", {"n": 9})
                await db.commit()
            async with sm() as db:
                counts = await deliver_batch(db, {"test.ok": [handler]}, limit=50)
            assert counts == {"delivered": 3, "retrying": 0, "dead": 0}
            assert seen == [1, 2]
            async with sm() as db:
                pending = await db.scalar(select(PlatformOutbox).where(PlatformOutbox.dispatched_at.is_(None)))
                assert pending is None
                assert await deliver_batch(db, {"test.ok": [handler]}) == {"delivered": 0, "retrying": 0, "dead": 0}

        return go()

    run(body)


def test_failed_handler_retries_with_backoff_then_dead_letters_and_can_be_requeued():
    def body(sm):
        async def go():
            calls = {"n": 0}

            async def boom(_db, _row):
                calls["n"] += 1
                raise RuntimeError("temporary")

            now = datetime.now(UTC)
            async with sm() as db:
                row = await _publish(db, "test.boom", {"n": 1}, when=now)
                event_id = row.id
                await db.commit()
            async with sm() as db:
                counts = await deliver_batch(db, {"test.boom": [boom]}, now=now)
            assert counts == {"delivered": 0, "retrying": 1, "dead": 0}
            assert calls["n"] == 1
            async with sm() as db:
                stored = await db.get(PlatformOutbox, event_id, populate_existing=True)
                assert stored.dispatched_at is None and stored.dead_at is None
                assert stored.attempts == 1
                assert stored.next_attempt_at > now
                assert "RuntimeError" in (stored.last_error or "")
                # Not yet due: a second drain leaves it alone.
                assert (await deliver_batch(db, {"test.boom": [boom]}, now=now))["retrying"] == 0

            # Fail the remaining attempts and dead-letter. Each call must use a later clock than the
            # backoff deadline written by the previous one.
            for attempt in range(2, MAX_ATTEMPTS + 1):
                async with sm() as db:
                    counts = await deliver_batch(db, {"test.boom": [boom]}, now=now + timedelta(days=attempt))
                expected = "dead" if attempt == MAX_ATTEMPTS else "retrying"
                assert counts[expected] == 1
            assert calls["n"] == MAX_ATTEMPTS
            async with sm() as db:
                stored = await db.get(PlatformOutbox, event_id, populate_existing=True)
                assert stored.dead_at is not None and stored.dispatched_at is None
                health = await outbox_health(db)
                assert health["dead_lettered"] >= 1
                # Requeue gives the event a fresh attempt budget.
                assert await requeue_dead(db, [event_id]) == 1
                stored = await db.get(PlatformOutbox, event_id, populate_existing=True)
                assert stored.dead_at is None and stored.attempts == 0

            # After requeue a working handler delivers it once (idempotent: running again is a no-op).
            delivered = []

            async def ok(_db, row):
                delivered.append(row.id)

            async with sm() as db:
                counts = await deliver_batch(db, {"test.boom": [ok]}, now=datetime.now(UTC) + timedelta(days=1))
            assert counts["delivered"] == 1 and delivered == [event_id]
            async with sm() as db:
                assert (await deliver_batch(db, {"test.boom": [ok]}))["delivered"] == 0

        return go()

    run(body)


def test_failing_handler_rolls_back_only_its_own_writes():
    def body(sm):
        async def go():
            from app.platform.models import OrgSettings

            async def writes_then_fails(db, _row):
                db.add(OrgSettings(id=1, time_zone="UTC", currency="USD"))
                await db.flush()
                raise RuntimeError("nope")

            async with sm() as db:
                await _publish(db, "test.tx", {})
                await db.commit()
            async with sm() as db:
                await deliver_batch(db, {"test.tx": [writes_then_fails]}, now=datetime.now(UTC))
            async with sm() as db:
                assert await db.get(OrgSettings, 1) is None
                row = await db.scalar(select(PlatformOutbox).where(PlatformOutbox.event_type == "test.tx"))
                assert row.attempts == 1 and row.dispatched_at is None

        return go()

    run(body)


def test_concurrent_dispatchers_deliver_each_event_once():
    def body(sm):
        async def go():
            seen = []

            async def handler(_db, row):
                seen.append(row.payload["n"])

            async with sm() as db:
                for n in range(8):
                    await _publish(db, "test.race", {"n": n})
                await db.commit()

            async def drain():
                async with sm() as db:
                    return await deliver_batch(db, {"test.race": [handler]}, limit=8)

            results = await asyncio.gather(*(drain() for _ in range(4)))
            total = sum(item["delivered"] for item in results)
            assert total == 8
            assert sorted(seen) == list(range(8))

        return go()

    run(body)


def test_public_drain_outbox_uses_the_same_rules():
    def body(sm):
        async def go():
            seen = []

            async def handler(row):
                if row.payload.get("fail"):
                    raise RuntimeError("x")
                seen.append(row.event_type)

            async with sm() as db:
                await platform.publish(db, "test.keep", None, {"ok": True}, __import__("uuid").uuid4())
                await platform.publish(db, "test.drop", None, {"fail": True}, __import__("uuid").uuid4())
                await db.commit()
            async with sm() as db:
                delivered = await platform.drain_outbox(db, handler, limit=10)
            assert delivered == 1 and seen == ["test.keep"]

        return go()

    run(body)


def test_worker_loop_stops_and_survives_a_failed_poll(monkeypatch):
    def body(sm):
        async def go():
            stop = asyncio.Event()
            calls = {"n": 0}

            async def flaky(_sessionmaker, **_kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("db restart")
                stop.set()
                return {"delivered": 0, "retrying": 0, "dead": 0}

            monkeypatch.setattr("app.platform.outbox.drain_until_empty", flaky)
            await run_outbox_worker(sm, stop, interval_seconds=0.01)
            assert calls["n"] >= 2

        return go()

    run(body)
