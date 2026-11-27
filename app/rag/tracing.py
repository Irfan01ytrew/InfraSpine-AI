"""One trace per knowledge-chat question: how it was answered, how long each stage took, and why it failed.

A trace holds metadata only, the same policy as the logs: the strategy, the retrieved chunk IDs and scores,
the evidence decision (for example `refused_verifier`), per-stage latency, LLM calls and tokens, and for a
failure the exception type and stack frames. It never holds the question, the answer, a prompt or document
text, and not the exception message either, which can quote input. The full traceback with its message goes
to the log file instead.

The route opens a trace with `traced(...)`; code below it adds to the current trace through `stage`,
`decide` and `TracedLLM` without the trace being passed around. Writing a trace never fails a request.
"""

from __future__ import annotations

import logging
import time
import traceback
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import RagTrace

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.rag.chat import ChatResult
    from app.services.llm import LLMClient

logger = logging.getLogger(__name__)

_current: ContextVar[Trace | None] = ContextVar("rag_trace", default=None)
_PURGE_EVERY = timedelta(hours=6)
_last_purge: datetime | None = None
_MAX_STACK_FRAMES = 30


@dataclass
class Trace:
    route: str
    question_chars: int
    history_turns: int = 0
    request_id: str | None = None
    user_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    message_id: int | None = None
    strategy: str | None = None
    decision: str | None = None
    provider: str | None = None
    model: str | None = None
    insufficient_evidence: bool | None = None
    general_knowledge: bool | None = None
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    llm_calls: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error_type: str | None = None
    error_stack: str | None = None
    started: float = field(default_factory=time.perf_counter)

    def add_time(self, name: str, seconds: float) -> None:
        self.timings[name] = round(self.timings.get(name, 0.0) + seconds * 1000, 1)

    def record_result(self, result: ChatResult) -> None:
        answer = result.answer
        cited = {chunk.chunk_id for _, chunk in answer.cited}
        self.strategy = result.strategy
        # Keep the provider recorded at call time when the answer itself names none.
        if answer.provider is not None:
            self.provider, self.model = answer.provider, answer.model
        self.insufficient_evidence, self.general_knowledge = answer.insufficient_evidence, answer.general
        self.retrieved = [
            {
                "rank": rank,
                "chunk_id": chunk.chunk_id,
                "document_id": str(chunk.document_id),
                "score": round(chunk.score, 4),
                "cited": chunk.chunk_id in cited,
            }
            for rank, chunk in enumerate(result.passages, start=1)
        ]

    def fail(self, exc: BaseException) -> None:
        self.error_type = f"{type(exc).__module__}.{type(exc).__qualname__}"[:120]
        frames = traceback.extract_tb(exc.__traceback__)[-_MAX_STACK_FRAMES:]
        self.error_stack = "".join(traceback.format_list(frames))  # frames only: no exception message


def current() -> Trace | None:
    return _current.get()


def decide(decision: str) -> None:
    """Why the answer came out the way it did; the last call wins."""
    trace = _current.get()
    if trace is not None:
        trace.decision = decision


@contextmanager
def stage(name: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        trace = _current.get()
        if trace is not None:
            trace.add_time(name, time.perf_counter() - started)


class TracedLLM:
    """Counts calls, tokens and time of an LLM client into the current trace."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def chat(self, message: str, *, system_prompt: str | None = None):
        trace = _current.get()
        started = time.perf_counter()
        try:
            response = await self._inner.chat(message, system_prompt=system_prompt)
        finally:
            if trace is not None:
                trace.llm_calls += 1
                trace.add_time("llm", time.perf_counter() - started)
                # Attribute the call even when it fails, so usage reports count failed calls per provider.
                provider = getattr(self._inner, "provider", None)
                if trace.provider is None and isinstance(provider, str):
                    model = getattr(self._inner, "_model", None)
                    trace.provider, trace.model = provider, model if isinstance(model, str) else None
        if trace is not None and response.usage is not None:
            trace.prompt_tokens = (trace.prompt_tokens or 0) + response.usage.prompt_tokens
            trace.completion_tokens = (trace.completion_tokens or 0) + response.usage.completion_tokens
        return response

    async def chat_stream(self, message: str, *, system_prompt: str | None = None, on_token):
        trace = _current.get()
        started = time.perf_counter()
        try:
            stream = getattr(self._inner, "chat_stream", None)
            if callable(stream):
                response = await stream(message, system_prompt=system_prompt, on_token=on_token)
            else:
                response = await self._inner.chat(message, system_prompt=system_prompt)
                result = on_token(response.reply)
                if hasattr(result, "__await__"):
                    await result
        finally:
            if trace is not None:
                trace.llm_calls += 1
                trace.add_time("llm", time.perf_counter() - started)
                provider = getattr(self._inner, "provider", None)
                if trace.provider is None and isinstance(provider, str):
                    model = getattr(self._inner, "_model", None)
                    trace.provider, trace.model = provider, model if isinstance(model, str) else None
        if trace is not None and response.usage is not None:
            trace.prompt_tokens = (trace.prompt_tokens or 0) + response.usage.prompt_tokens
            trace.completion_tokens = (trace.completion_tokens or 0) + response.usage.completion_tokens
        return response


def traced_llm(get_llm: Callable[[], Awaitable[LLMClient]]) -> Callable[[], Awaitable[LLMClient]]:
    async def build() -> LLMClient:
        client = await get_llm()
        return TracedLLM(client) if _current.get() is not None else client  # type: ignore[return-value]

    return build


@asynccontextmanager
async def traced(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    route: str,
    question: str,
    user_id: uuid.UUID | None,
    request_id: str | None,
    conversation_id: uuid.UUID | None = None,
) -> AsyncIterator[Trace | None]:
    if not settings.rag_trace_enabled:
        yield None
        return
    trace = Trace(
        route=route,
        question_chars=len(question),
        request_id=request_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    token = _current.set(trace)
    try:
        yield trace
    except BaseException as exc:
        trace.fail(exc)
        raise
    finally:
        _current.reset(token)
        await _save(sessionmaker, settings, trace)


async def _save(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, trace: Trace) -> None:
    global _last_purge
    latency_ms = int((time.perf_counter() - trace.started) * 1000)
    try:
        async with sessionmaker() as db:
            db.add(
                RagTrace(
                    request_id=trace.request_id,
                    user_id=trace.user_id,
                    conversation_id=trace.conversation_id,
                    message_id=trace.message_id,
                    route=trace.route,
                    status="error" if trace.error_type else "ok",
                    strategy=trace.strategy,
                    decision=trace.decision,
                    provider=trace.provider,
                    model=trace.model,
                    insufficient_evidence=trace.insufficient_evidence,
                    general_knowledge=trace.general_knowledge,
                    question_chars=trace.question_chars,
                    history_turns=trace.history_turns,
                    retrieved=trace.retrieved,
                    timings=trace.timings,
                    llm_calls=trace.llm_calls,
                    prompt_tokens=trace.prompt_tokens,
                    completion_tokens=trace.completion_tokens,
                    latency_ms=latency_ms,
                    error_type=trace.error_type,
                    error_stack=trace.error_stack,
                )
            )
            now = datetime.now(UTC)
            if _last_purge is None or now - _last_purge > _PURGE_EVERY:
                _last_purge = now
                cutoff = now - timedelta(days=settings.rag_trace_retention_days)
                await db.execute(delete(RagTrace).where(RagTrace.created_at < cutoff))
            await db.commit()
    except Exception as exc:
        logger.warning(
            "Could not save the RAG trace request_id=%s error_type=%s",
            trace.request_id, type(exc).__name__,
        )
