"""Bounded SSE lifecycle shared by the saved Knowledge conversation route."""
import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable


async def conversation_events(
    produce: Callable[[], Awaitable[None]],
    queue: asyncio.Queue,
    *,
    heartbeat_seconds: float = 15,
) -> AsyncIterator[str]:
    task = asyncio.create_task(produce())
    try:
        yield ": connected\n\n"
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if item is None:
                return
            event, data = item
            yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
            # The producer may be unable to enqueue its sentinel when its bounded queue is full.
            # Terminal events are sufficient: never wait forever after a saved answer or error.
            if event in {"done", "error"}:
                return
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
