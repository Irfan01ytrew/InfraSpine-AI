import asyncio

from app.services.chat_stream import conversation_events


def test_terminal_event_finishes_even_when_full_queue_dropped_sentinel():
    async def check():
        queue = asyncio.Queue(maxsize=1)

        async def produce():
            await queue.put(("done", {"answer": "saved"}))
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

        async def collect():
            return [event async for event in conversation_events(produce, queue)]

        events = await asyncio.wait_for(collect(), timeout=1)
        assert len(events) == 2
        assert 'event: done\ndata: {"answer":"saved"}' in events[-1]

    asyncio.run(check())


def test_slow_retrieval_sends_heartbeat_then_provider_error_terminates():
    async def check():
        queue = asyncio.Queue(maxsize=1)
        release = asyncio.Event()

        async def produce():
            await release.wait()
            await queue.put(("error", {"code": "unavailable"}))

        stream = conversation_events(produce, queue, heartbeat_seconds=0.01)
        assert await anext(stream) == ": connected\n\n"
        assert await asyncio.wait_for(anext(stream), 1) == ": keep-alive\n\n"
        release.set()
        async def remaining():
            return [event async for event in stream]
        events = await asyncio.wait_for(remaining(), 1)
        assert "event: error" in events[-1]
        assert all(event == ": keep-alive\n\n" for event in events[:-1])

    asyncio.run(check())


def test_disconnected_consumer_cancels_blocked_producer():
    async def check():
        queue = asyncio.Queue(maxsize=1)
        started, stopped = asyncio.Event(), asyncio.Event()

        async def produce():
            started.set()
            try:
                while True:
                    await queue.put(("delta", {"delta": "partial"}))
            finally:
                stopped.set()

        stream = conversation_events(produce, queue)
        await anext(stream)
        await started.wait()
        await stream.aclose()
        assert stopped.is_set()

    asyncio.run(check())
