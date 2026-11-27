"""Leased database worker for safe, read-only agent runs."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crypto import CredentialCipher
from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.core.config import Settings
from app.db.models import AgentProfile, AgentProfileRun, User
from app.rag import tracing
from app.rag.components import RagComponents
from app.rag.respond import rag_respond
from app.services import agents, audit
from app.services.audit import AuditContext
from app.services.llm_runtime import LLMRuntime

logger = logging.getLogger(__name__)

AGENT_QUEUE_POLL_SECONDS = 2.0
AGENT_QUEUE_LEASE = timedelta(minutes=60)
AGENT_QUEUE_MAX_ATTEMPTS = 2
AGENT_QUEUE_RETRY_DELAY_SECONDS = 5
AGENT_QUEUE_BATCH_SIZE = 25
AGENT_QUEUE_CAPABILITIES = frozenset({"knowledge_search", "company_data_analysis"})


def _worker_context(run_id: uuid.UUID) -> AuditContext:
    return AuditContext(None, "agent-queue-worker", None, str(run_id))


async def recover_expired_agent_queue_runs(
    sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime | None = None
) -> int:
    """Requeue an expired read-only run lease once, then leave it failed for human review."""
    current = now or datetime.now(UTC)
    recovered = 0
    while True:
        async with sessionmaker() as db:
            rows = list(
                (
                    await db.execute(
                        select(AgentProfileRun)
                        .where(
                            AgentProfileRun.execution_mode == "queue",
                            AgentProfileRun.status == "running",
                            AgentProfileRun.locked_until.is_not(None),
                            AgentProfileRun.locked_until <= current,
                        )
                        .order_by(AgentProfileRun.locked_until, AgentProfileRun.id)
                        .limit(AGENT_QUEUE_BATCH_SIZE)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            if not rows:
                break
            for run in rows:
                run.failure_code = "interrupted"
                run.locked_until = None
                if run.attempts < AGENT_QUEUE_MAX_ATTEMPTS:
                    run.status = "queued"
                    run.available_at = current
                    run.finished_at = None
                    action = "agent.profile_run_requeued"
                    outcome = "success"
                else:
                    run.status = "failed"
                    run.finished_at = current
                    action = "agent.profile_run_interrupted"
                    outcome = "failure"
                audit.record(
                    db,
                    _worker_context(run.id),
                    action,
                    outcome=outcome,
                    target=str(run.id),
                    details={"profile_id": str(run.profile_id), "attempt": run.attempts},
                )
            await db.commit()
            recovered += len(rows)
            if len(rows) < AGENT_QUEUE_BATCH_SIZE:
                break
    return recovered


async def _claim_next_agent_run(
    sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime | None = None
) -> tuple[uuid.UUID, int] | None:
    current = now or datetime.now(UTC)
    async with sessionmaker() as db:
        run = (
            await db.execute(
                select(AgentProfileRun)
                .where(
                    AgentProfileRun.execution_mode == "queue",
                    AgentProfileRun.status == "queued",
                    AgentProfileRun.available_at <= current,
                )
                .order_by(AgentProfileRun.available_at, AgentProfileRun.requested_at, AgentProfileRun.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if run is None:
            return None
        run.status = "running"
        run.attempts += 1
        run.started_at = current
        run.locked_until = current + AGENT_QUEUE_LEASE
        run.finished_at = None
        run.failure_code = None
        audit.record(
            db,
            _worker_context(run.id),
            "agent.profile_run_started",
            target=str(run.id),
            details={"profile_id": str(run.profile_id), "capability": run.capability, "attempt": run.attempts},
        )
        await db.commit()
        return run.id, run.attempts


async def _fail_agent_attempt(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    attempt: int,
    error_code: str,
    *,
    now: datetime | None = None,
    retry_delay_seconds: int = AGENT_QUEUE_RETRY_DELAY_SECONDS,
    retryable: bool = True,
) -> None:
    current = now or datetime.now(UTC)
    async with sessionmaker() as db:
        run = (
            await db.execute(
                select(AgentProfileRun)
                .where(
                    AgentProfileRun.id == run_id,
                    AgentProfileRun.execution_mode == "queue",
                    AgentProfileRun.status == "running",
                    AgentProfileRun.attempts == attempt,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            return  # a newer fenced attempt owns the row
        run.failure_code = error_code[:60]
        run.locked_until = None
        if retryable and attempt < AGENT_QUEUE_MAX_ATTEMPTS:
            run.status = "queued"
            run.available_at = current + timedelta(seconds=max(0, retry_delay_seconds))
            run.finished_at = None
        else:
            run.status = "failed"
            run.finished_at = current
        audit.record(
            db,
            _worker_context(run.id),
            "agent.profile_run_attempt_failed",
            outcome="failure",
            target=str(run.id),
            details={"profile_id": str(run.profile_id), "attempt": attempt, "error_type": error_code[:60]},
        )
        await db.commit()


async def _execute_agent_attempt(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    attempt: int,
    settings: Settings,
    rag: RagComponents,
    cipher: CredentialCipher,
    llm_runtime: LLMRuntime,
) -> None:
    async with sessionmaker() as db:
        run = await db.get(AgentProfileRun, run_id)
        if run is None or run.status != "running" or run.attempts != attempt or run.execution_mode != "queue":
            return
        if run.capability not in AGENT_QUEUE_CAPABILITIES:
            raise ConflictError("This agent capability is not eligible for background execution.")
        profile = await db.get(AgentProfile, run.profile_id)
        actor = await db.get(User, run.requester_user_id) if run.requester_user_id else None
        if profile is None or actor is None or profile.version != run.profile_version:
            raise ConflictError("The agent profile or requester is no longer available.")
        await agents.profile_for_run(db, actor, profile.id, run.capability)
        try:
            stored_input = json.loads(agents.open_run_value(cipher, run, "task"))
            task = stored_input["task"]
            document_ids = [uuid.UUID(value) for value in stored_input["document_ids"]]
        except (ValueError, KeyError, TypeError):
            raise ConflictError("The stored agent request failed its integrity check.") from None
        if not isinstance(task, str) or not isinstance(document_ids, list):
            raise ConflictError("The stored agent request failed its integrity check.")
        department_scope = profile.department_group_id if profile.knowledge_scope == "department_only" else None
        async with tracing.traced(
            sessionmaker,
            settings,
            route="agent_profile_queue",
            question=task,
            user_id=actor.id,
            request_id=str(run.id),
        ):
            result = await rag_respond(
                db,
                actor,
                rag,
                settings,
                lambda: llm_runtime.get_client(db),
                task,
                history=[],
                document_ids=document_ids or None,
                focus_document_ids=document_ids or None,
                memory=None,
                department_group_id=department_scope,
            )
        serialized = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > 2_000_000:
            raise ConflictError("The agent result exceeds the encrypted history limit.")
        encrypted = agents.seal_run_value(cipher, run.id, "result", serialized)
        saved = await db.execute(
            update(AgentProfileRun)
            .where(
                AgentProfileRun.id == run.id,
                AgentProfileRun.status == "running",
                AgentProfileRun.execution_mode == "queue",
                AgentProfileRun.attempts == attempt,
            )
            .values(
                result_ciphertext=encrypted.ciphertext,
                result_nonce=encrypted.nonce,
                result_key_id=encrypted.key_id,
                status="completed",
                failure_code=None,
                locked_until=None,
                finished_at=datetime.now(UTC),
            )
        )
        if saved.rowcount != 1:
            await db.rollback()
            return  # a later attempt fenced this worker out
        audit.record(
            db,
            _worker_context(run.id),
            "agent.profile_run_completed",
            target=str(run.id),
            details={"profile_id": str(profile.id), "profile_version": profile.version, "capability": run.capability},
        )
        await db.commit()


async def process_next_agent_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    rag: RagComponents,
    cipher: CredentialCipher,
    llm_runtime: LLMRuntime,
    *,
    now: datetime | None = None,
    retry_delay_seconds: int = AGENT_QUEUE_RETRY_DELAY_SECONDS,
) -> bool:
    """Claim and execute one queued run; failed read-only work receives at most one retry."""
    claim_time = now or datetime.now(UTC)
    claimed = await _claim_next_agent_run(sessionmaker, now=claim_time)
    if claimed is None:
        return False
    run_id, attempt = claimed
    try:
        await _execute_agent_attempt(sessionmaker, run_id, attempt, settings, rag, cipher, llm_runtime)
    except Exception as exc:
        code = type(exc).__name__[:60]
        logger.warning("Queued agent run %s failed on attempt %s (%s)", run_id, attempt, code)
        await _fail_agent_attempt(
            sessionmaker,
            run_id,
            attempt,
            code,
            retry_delay_seconds=retry_delay_seconds,
            retryable=not isinstance(exc, (ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError)),
        )
    return True


async def run_agent_worker(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    rag: RagComponents,
    cipher: CredentialCipher,
    llm_runtime: LLMRuntime,
    stop: asyncio.Event,
    *,
    interval_seconds: float = AGENT_QUEUE_POLL_SECONDS,
) -> None:
    """Poll durable read-only agent runs until the service asks the worker to stop."""
    await recover_expired_agent_queue_runs(sessionmaker)
    while not stop.is_set():
        try:
            worked = await process_next_agent_run(sessionmaker, settings, rag, cipher, llm_runtime)
        except Exception as exc:
            logger.warning("Agent queue worker iteration failed (%s)", type(exc).__name__)
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
