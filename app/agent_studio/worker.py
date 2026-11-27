"""Owner-scoped durable Agent Studio queue worker."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent_studio.service import AgentStudioService, decode_run_state, _encode_state, _AgentRunClaimLost
from app.db.agent_studio_models import AgentStudioAgent, AgentStudioRun
from app.db.models import User
from app.core.crypto import CredentialCipher
from app.core.config import Settings
from app.services.llm_runtime import LLMRuntime

logger = logging.getLogger(__name__)
_MAX_ATTEMPTS = 3


async def recover_abandoned_agent_studio_runs(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    cipher: CredentialCipher,
    *,
    now: datetime | None = None,
) -> int:
    """Requeue safe interrupted work; quarantine a run stopped during an action."""
    current = now or datetime.now(UTC)
    lease = current - timedelta(seconds=settings.agent_studio_run_timeout_seconds + 30)
    recovered = 0
    async with sessionmaker() as db:
        rows = list((await db.scalars(
            select(AgentStudioRun)
            .where(AgentStudioRun.status == "running", AgentStudioRun.claimed_at < lease)
            .order_by(AgentStudioRun.claimed_at)
            .with_for_update(skip_locked=True)
            .limit(100)
        )).all())
        for run in rows:
            state = decode_run_state(cipher, run)
            run.claimed_at = None
            if state.get("action_inflight") or state.get("approval_granted"):
                run.status = "failed"
                run.failure_code = "worker_interrupted_action_uncertain"
                run.finished_at = current
                state["answer"] = "The worker stopped while an action may have been in progress. Review this run before retrying."
                state["action_inflight"] = False
            elif run.attempt_count < _MAX_ATTEMPTS:
                run.status = "queued"
                run.failure_code = None
                run.finished_at = None
                delay = min(60, 2 ** max(1, run.attempt_count))
                run.next_attempt_at = current + timedelta(seconds=delay)
                state["answer"] = f"The worker restarted before completing this task. It is queued for retry in {delay} seconds."
            else:
                run.status = "failed"
                run.failure_code = "worker_interrupted"
                run.finished_at = current
                state["answer"] = "The worker stopped repeatedly before completing this task. Review it before retrying."
            sealed = _encode_state(cipher, run.id, state)
            run.state_ciphertext, run.state_nonce, run.state_key_id = sealed.ciphertext, sealed.nonce, sealed.key_id
            recovered += 1
        if recovered:
            await db.commit()
    return recovered


async def process_next_agent_studio_run(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    cipher: CredentialCipher,
    runtime: LLMRuntime,
    rag: Any | None,
    *,
    run_id: uuid.UUID | None = None,
    exclude_unified: bool = False,
) -> bool:
    """Claim and execute one eligible run (or the given run). Returns False when nothing was claimed.

    `exclude_unified=True` leaves runs that the unified workflow runtime carries (`wf_runs.origin='studio'`) to the
    engine, so a paused or stopped workflow run cannot be executed behind its back by this legacy loop."""
    claim = await claim_agent_studio_run(sessionmaker, run_id=run_id, exclude_unified=exclude_unified)
    if claim is None:
        return False
    await execute_agent_studio_claim(sessionmaker, settings, cipher, runtime, rag, claim)
    return True


async def claim_agent_studio_run(
    sessionmaker: async_sessionmaker, *, run_id: uuid.UUID | None = None, exclude_unified: bool = False,
) -> tuple[uuid.UUID, uuid.UUID, int, datetime] | None:
    now = datetime.now(UTC)
    async with sessionmaker() as db:
        query = (
            select(AgentStudioRun)
            .join(AgentStudioAgent, AgentStudioAgent.id == AgentStudioRun.agent_id)
            .join(User, User.id == AgentStudioRun.owner_id)
            .where(
                AgentStudioRun.status == "queued",
                AgentStudioRun.next_attempt_at <= now,
                AgentStudioAgent.enabled.is_(True),
                AgentStudioAgent.paused.is_(False),
                User.is_active.is_(True),
            )
        )
        if run_id is not None:
            query = query.where(AgentStudioRun.id == run_id)
        if exclude_unified:
            from sqlalchemy import String, exists
            from app.db.workflow_engine_models import WorkflowRun
            query = query.where(~exists().where(WorkflowRun.origin == "studio",
                                                WorkflowRun.origin_ref == AgentStudioRun.id.cast(String)))
        run = await db.scalar(
            query.order_by(AgentStudioRun.next_attempt_at, AgentStudioRun.created_at)
            .with_for_update(of=AgentStudioRun, skip_locked=True)
            .limit(1)
        )
        if run is None:
            return None
        run.status = "running"
        run.attempt_count += 1
        run.started_at = run.started_at or now
        run.claimed_at = now
        run.failure_code = None
        await db.commit()
        return run.id, run.owner_id, run.attempt_count, run.claimed_at


async def execute_agent_studio_claim(sessionmaker, settings, cipher, runtime, rag, claim) -> None:
    run_id, owner_id, expected_attempt, expected_claimed_at = claim
    async with sessionmaker() as db:
        user = await db.get(User, owner_id)
        if user is None or not user.is_active:
            async with sessionmaker() as failed_db:
                stale = await failed_db.scalar(
                    select(AgentStudioRun)
                    .where(
                        AgentStudioRun.id == run_id,
                        AgentStudioRun.owner_id == owner_id,
                        AgentStudioRun.status == "running",
                        AgentStudioRun.attempt_count == expected_attempt,
                        AgentStudioRun.claimed_at == expected_claimed_at,
                    )
                    .with_for_update()
                )
                if stale is not None:
                    stale.status = "failed"
                    stale.failure_code = "owner_unavailable"
                    stale.finished_at = datetime.now(UTC)
                    stale.claimed_at = None
                    await failed_db.commit()
            return
        service = AgentStudioService(db, user, settings, cipher, runtime, rag)
        try:
            await service.execute_claimed(
                run_id,
                expected_attempt=expected_attempt,
                expected_claimed_at=expected_claimed_at,
            )
        except _AgentRunClaimLost:
            # Recovery, cancellation, or a newer attempt already owns this row.
            return


async def run_agent_studio_worker(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    cipher: CredentialCipher,
    runtime: LLMRuntime,
    rag: Any | None,
    stop: asyncio.Event,
    *,
    exclude_unified: bool = False,
) -> None:
    """Poll the durable queue until the owning application begins shutdown. With the unified workflow runtime on
    (`exclude_unified`), this loop only runs work the runtime does not carry (e.g. an owner without a department)."""
    try:
        count = await recover_abandoned_agent_studio_runs(sessionmaker, settings, cipher)
        if count:
            logger.warning("Recovered interrupted Agent Studio runs count=%s", count)
        last_recovery = datetime.now(UTC)
        while not stop.is_set():
            try:
                if (datetime.now(UTC) - last_recovery).total_seconds() >= 30:
                    count = await recover_abandoned_agent_studio_runs(sessionmaker, settings, cipher)
                    if count:
                        logger.warning("Recovered interrupted Agent Studio runs count=%s", count)
                    last_recovery = datetime.now(UTC)
                did_work = await process_next_agent_studio_run(sessionmaker, settings, cipher, runtime, rag,
                                                                exclude_unified=exclude_unified)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Agent Studio worker iteration failed error_type=%s", type(exc).__name__)
                did_work = False
            if not did_work:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.agent_studio_worker_poll_seconds)
                except TimeoutError:
                    pass
    except asyncio.CancelledError:
        raise
