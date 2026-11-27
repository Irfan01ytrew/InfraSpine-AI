"""Owner-scoped Agent Studio persistence, LangGraph execution, and approval resumption."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_studio.graph import AgentState, build_agent_graph
from app.agent_studio.tools import (
    APPROVAL_TOOLS,
    TOOL_DEFINITIONS,
    run_tool,
    _decrypt as decrypt_memory,
)
from app.core.config import Settings
from app.core.crypto import CredentialCipher, EncryptedValue
from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.db.agent_studio_models import (
    AgentStudioAgent,
    AgentStudioApproval,
    AgentStudioMemory,
    AgentStudioRun,
    AgentStudioThread,
)
from app.db.models import User
from app.services.llm import LLMClient
from app.services.llm_runtime import LLMRuntime

if TYPE_CHECKING:
    from app.rag.components import RagComponents

logger = logging.getLogger(__name__)
_NOT_FOUND = "Agent Studio resource not found."


class _AgentRunClaimLost(Exception):
    """Raised when a worker no longer owns the durable run attempt."""
_MAX_HISTORY_MESSAGES = 24
_MAX_MEMORY_ITEMS = 20
_MAX_DELEGATION_DEPTH = 2


def _aad(kind: str, row_id: uuid.UUID) -> bytes:
    return f"infraspine:agent-studio:v1:{kind}:{row_id}".encode("ascii")


def _seal(cipher: CredentialCipher, kind: str, row_id: uuid.UUID, value: str) -> EncryptedValue:
    return cipher.encrypt(value, associated_data=_aad(kind, row_id))


def _open(cipher: CredentialCipher, kind: str, row_id: uuid.UUID, ciphertext: bytes, nonce: bytes, key_id: str) -> str:
    return cipher.decrypt(EncryptedValue(ciphertext, nonce, key_id), associated_data=_aad(kind, row_id))


def _encode_state(cipher: CredentialCipher, run_id: uuid.UUID, state: dict[str, Any]) -> EncryptedValue:
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":"), default=str)
    return _seal(cipher, "run-state", run_id, raw)


def decode_run_state(cipher: CredentialCipher, run: AgentStudioRun) -> dict[str, Any]:
    return json.loads(_open(cipher, "run-state", run.id, run.state_ciphertext, run.state_nonce, run.state_key_id))


def _encode_thread(cipher: CredentialCipher, thread_id: uuid.UUID, history: list[dict[str, str]]) -> EncryptedValue:
    return _seal(cipher, "thread-history", thread_id, json.dumps(history, ensure_ascii=False, separators=(",", ":")))


def decode_thread_history(cipher: CredentialCipher, thread: AgentStudioThread) -> list[dict[str, str]]:
    return json.loads(_open(cipher, "thread-history", thread.id, thread.history_ciphertext, thread.history_nonce, thread.history_key_id))


def _validate_definition(data: dict[str, Any]) -> dict[str, Any]:
    name = " ".join(str(data.get("name") or "").split())
    description = " ".join(str(data.get("description") or "").split())
    instructions = str(data.get("instructions") or "").strip()
    tools = list(data.get("tool_permissions") or [])
    approvals = list(data.get("approval_tools") or [])
    delegates = list(dict.fromkeys(data.get("delegate_agent_ids") or []))
    if not name or len(name) > 100:
        raise InvalidInputError("Agent name must contain between 1 and 100 characters.")
    if len(description) > 500 or not instructions or len(instructions) > 6000:
        raise InvalidInputError("Description or agent instructions exceed the allowed length.")
    known = {definition[0] for definition in TOOL_DEFINITIONS}
    if not tools or len(tools) > 12 or len(set(tools)) != len(tools) or not set(tools) <= known:
        raise InvalidInputError("Choose one or more distinct registered tools.")
    if len(approvals) > 8 or len(set(approvals)) != len(approvals) or not set(approvals) <= set(tools):
        raise InvalidInputError("Approval tools must be a distinct subset of the agent's tools.")
    if "rag_search" in tools and not data.get("rag_enabled"):
        raise InvalidInputError("Enable optional Knowledge access before granting the RAG search tool.")
    if delegates and "delegate_agent" not in tools:
        raise InvalidInputError("Add the delegate_agent tool before choosing subagents.")
    if data.get("max_steps", 6) not in range(1, 9):
        raise InvalidInputError("Maximum steps must be between 1 and 8.")
    provider_id, model = data.get("provider_id"), data.get("model")
    if (provider_id is None) != (model is None):
        raise InvalidInputError("Choose both a provider and a model, or use the active default.")
    return {
        "name": name,
        "description": description,
        "instructions": instructions,
        "tool_permissions": sorted(tools),
        "approval_tools": sorted(approvals),
        "delegate_agent_ids": [str(item) for item in delegates],
        "rag_enabled": bool(data.get("rag_enabled")),
        "max_steps": int(data.get("max_steps", 6)),
        "provider_id": provider_id,
        "model": model,
    }


async def _require_studio_access(db: AsyncSession, user: User) -> None:
    from app.platform.modules import org_gate, raise_unless
    gate = await org_gate(db, user, "agent.run", legacy_allowed=bool(getattr(user, "is_active", True)))
    raise_unless(gate, not_found=_NOT_FOUND)


async def _require_agent(db: AsyncSession, user: User, agent_id: uuid.UUID, *, enabled: bool | None = None) -> AgentStudioAgent:
    await _require_studio_access(db, user)
    query = select(AgentStudioAgent).where(AgentStudioAgent.id == agent_id, AgentStudioAgent.owner_id == user.id)
    if enabled is not None:
        query = query.where(AgentStudioAgent.enabled.is_(enabled))
    agent = await db.scalar(query)
    if agent is None:
        raise NotFoundError(_NOT_FOUND)
    return agent


async def _paused_now(db: AsyncSession, agent_id: uuid.UUID) -> bool:
    """Read the agent's current pause flag under a share lock, so a concurrent pause/resume waits for the
    caller's next commit and a run cannot start executing on a stale "not paused" read."""
    paused = await db.scalar(
        select(AgentStudioAgent.paused).where(AgentStudioAgent.id == agent_id).with_for_update(read=True)
    )
    return bool(paused)


async def list_agents(db: AsyncSession, user: User) -> list[AgentStudioAgent]:
    await _require_studio_access(db, user)
    # Each account starts with an independent, editable Master Agent. It is scoped to its owner
    # and uses the same explicit allowlist and approval controls as custom agents.
    master_id = uuid.uuid5(uuid.NAMESPACE_URL, f"infraspine-agent-studio-master:{user.id}")
    await db.execute(
        pg_insert(AgentStudioAgent)
        .values(
            id=master_id,
            owner_id=user.id,
            name="Master Agent",
            description="General multi-step assistant that can use your enabled tools and delegate to your configured subagents.",
            instructions=(
                "Coordinate the user's task, plan a small sequence of steps, and use only the enabled tools. "
                "Ask a subagent to handle a distinct research subtask when delegation is configured. "
                "Never claim an action succeeded without its tool result. Keep the user updated with concise progress."
            ),
            tool_permissions=[
                "web_search", "browse_url", "browser_capture", "file_list", "file_read",
                "create_file", "generate_image", "remember", "recall",
            ],
            approval_tools=["create_file", "generate_image"],
            delegate_agent_ids=[],
            rag_enabled=False,
            max_steps=8,
            enabled=True,
            version=1,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await db.commit()
    return list((await db.execute(
        select(AgentStudioAgent).where(AgentStudioAgent.owner_id == user.id)
        .order_by(AgentStudioAgent.updated_at.desc(), AgentStudioAgent.name)
    )).scalars())


async def create_agent(db: AsyncSession, user: User, data: dict[str, Any]) -> AgentStudioAgent:
    await _require_studio_access(db, user)
    clean = _validate_definition(data)
    if clean["delegate_agent_ids"]:
        existing_ids = set((await db.execute(
            select(AgentStudioAgent.id).where(
                AgentStudioAgent.owner_id == user.id,
                AgentStudioAgent.enabled.is_(True),
                AgentStudioAgent.id.in_([uuid.UUID(item) for item in clean["delegate_agent_ids"]]),
            )
        )).scalars())
        if existing_ids != {uuid.UUID(item) for item in clean["delegate_agent_ids"]}:
            raise InvalidInputError("Subagents must be enabled agents owned by your account.")
    row = AgentStudioAgent(owner_id=user.id, **clean)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_agent(
    db: AsyncSession, user: User, agent_id: uuid.UUID, expected_version: int, data: dict[str, Any]
) -> AgentStudioAgent:
    row = await _require_agent(db, user, agent_id)
    if row.version != expected_version:
        raise ConflictError("This agent changed in another session. Reload it and try again.")
    clean = _validate_definition(data)
    if clean["delegate_agent_ids"]:
        allowed_ids = {uuid.UUID(item) for item in clean["delegate_agent_ids"]}
        found = set((await db.execute(
            select(AgentStudioAgent.id).where(
                AgentStudioAgent.owner_id == user.id,
                AgentStudioAgent.enabled.is_(True),
                AgentStudioAgent.id.in_(allowed_ids),
                AgentStudioAgent.id != row.id,
            )
        )).scalars())
        if found != allowed_ids:
            raise InvalidInputError("Subagents must be different enabled agents owned by your account.")
    for key, value in clean.items():
        setattr(row, key, value)
    row.version += 1
    row.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(row)
    return row


async def set_agent_enabled(db: AsyncSession, user: User, agent_id: uuid.UUID, enabled: bool) -> AgentStudioAgent:
    row = await _require_agent(db, user, agent_id)
    row.enabled = enabled
    row.version += 1
    row.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(row)
    return row


async def set_agent_paused(db: AsyncSession, user: User, agent_id: uuid.UUID, paused: bool) -> AgentStudioAgent:
    row = await _require_agent(db, user, agent_id)
    if row.paused != paused:
        row.paused = paused
        row.version += 1
        row.updated_at = datetime.now(UTC)
        await db.flush()
    return row


async def delete_agent(db: AsyncSession, user: User, agent_id: uuid.UUID) -> None:
    row = await _require_agent(db, user, agent_id)
    master_id = uuid.uuid5(uuid.NAMESPACE_URL, f"infraspine-agent-studio-master:{user.id}")
    if row.id == master_id:
        raise InvalidInputError("The Master Agent is permanent. You can pause it instead.")
    parents = list((await db.execute(
        select(AgentStudioAgent).where(
            AgentStudioAgent.owner_id == user.id,
            AgentStudioAgent.id != row.id,
            AgentStudioAgent.delegate_agent_ids.contains([str(row.id)]),
        )
    )).scalars())
    now = datetime.now(UTC)
    for parent in parents:
        parent.delegate_agent_ids = [value for value in (parent.delegate_agent_ids or []) if value != str(row.id)]
        parent.version += 1
        parent.updated_at = now
    await db.delete(row)
    await db.commit()


async def create_thread(
    db: AsyncSession, user: User, cipher: CredentialCipher, agent_id: uuid.UUID, title: str
) -> AgentStudioThread:
    await _require_agent(db, user, agent_id, enabled=True)
    clean_title = " ".join(title.split())[:180] or "New agent run"
    thread_id = uuid.uuid4()
    sealed = _encode_thread(cipher, thread_id, [])
    row = AgentStudioThread(
        id=thread_id,
        agent_id=agent_id,
        owner_id=user.id,
        title=clean_title,
        history_ciphertext=sealed.ciphertext,
        history_nonce=sealed.nonce,
        history_key_id=sealed.key_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_threads(db: AsyncSession, user: User, agent_id: uuid.UUID) -> list[tuple[AgentStudioThread, str | None, datetime | None]]:
    await _require_agent(db, user, agent_id)
    threads = list((await db.execute(
        select(AgentStudioThread)
        .where(AgentStudioThread.owner_id == user.id, AgentStudioThread.agent_id == agent_id)
        .order_by(AgentStudioThread.updated_at.desc())
        .limit(100)
    )).scalars())
    if not threads:
        return []
    latest: dict[uuid.UUID, tuple[str | None, datetime | None]] = {}
    run_rows = (await db.execute(
        select(AgentStudioRun.thread_id, AgentStudioRun.status, AgentStudioRun.created_at)
        .where(AgentStudioRun.thread_id.in_([thread.id for thread in threads]), AgentStudioRun.owner_id == user.id)
        .order_by(AgentStudioRun.created_at.desc())
    )).all()
    for thread_id, run_status, created_at in run_rows:
        latest.setdefault(thread_id, (run_status, created_at))
    return [
        (thread, *(latest.get(thread.id, (None, None))))
        for thread in threads
    ]


async def list_runs(db: AsyncSession, user: User, agent_id: uuid.UUID, limit: int = 50) -> list[AgentStudioRun]:
    await _require_agent(db, user, agent_id)
    return list((await db.execute(
        select(AgentStudioRun).where(
            AgentStudioRun.owner_id == user.id, AgentStudioRun.agent_id == agent_id
        ).order_by(AgentStudioRun.created_at.desc()).limit(min(max(limit, 1), 100))
    )).scalars())


async def get_run(db: AsyncSession, user: User, run_id: uuid.UUID) -> AgentStudioRun:
    row = await db.scalar(select(AgentStudioRun).where(AgentStudioRun.id == run_id, AgentStudioRun.owner_id == user.id))
    if row is None:
        raise NotFoundError(_NOT_FOUND)
    return row


async def _load_memories(db: AsyncSession, user: User, agent: AgentStudioAgent, cipher: CredentialCipher) -> list[dict[str, str]]:
    rows = (await db.execute(
        select(AgentStudioMemory)
        .where(AgentStudioMemory.agent_id == agent.id, AgentStudioMemory.owner_id == user.id)
        .order_by(AgentStudioMemory.updated_at.desc())
        .limit(_MAX_MEMORY_ITEMS)
    )).scalars()
    values = []
    for row in rows:
        try:
            values.append({"memory_key": row.memory_key, "value": decrypt_memory(cipher, row)[:1200]})
        except Exception as exc:
            logger.error("Agent Studio memory could not be decrypted error_type=%s", type(exc).__name__)
    return list(reversed(values))


class AgentStudioService:
    def __init__(
        self,
        db: AsyncSession,
        user: User,
        settings: Settings,
        cipher: CredentialCipher,
        runtime: LLMRuntime,
        rag: RagComponents | None,
    ) -> None:
        self.db, self.user, self.settings, self.cipher, self.runtime, self.rag = db, user, settings, cipher, runtime, rag

    async def _existing_idempotent_run(
        self,
        key: str,
        agent_id: uuid.UUID,
        thread_id: uuid.UUID,
        prompt: str,
        scheduled_key: str | None,
    ) -> tuple[AgentStudioRun, dict[str, Any], AgentStudioApproval | None] | None:
        existing = await self.db.scalar(select(AgentStudioRun).where(
            AgentStudioRun.owner_id == self.user.id,
            AgentStudioRun.idempotency_key == key,
        ))
        if existing is None:
            return None
        state = decode_run_state(self.cipher, existing)
        if (
            existing.agent_id != agent_id
            or existing.thread_id != thread_id
            or state.get("user_input") != prompt
            or state.get("scheduled_at") != scheduled_key
        ):
            raise ConflictError("This idempotency key was already used for a different task.")
        approval = await self.db.scalar(select(AgentStudioApproval).where(
            AgentStudioApproval.run_id == existing.id,
            AgentStudioApproval.owner_id == self.user.id,
            AgentStudioApproval.status == "pending",
        ))
        return existing, state, approval

    async def start_run(
        self,
        agent_id: uuid.UUID,
        thread_id: uuid.UUID,
        prompt: str,
        *,
        idempotency_key: str | None = None,
        execute_inline: bool = False,
        scheduled_at: datetime | None = None,
    ) -> tuple[AgentStudioRun, dict[str, Any], AgentStudioApproval | None]:
        agent = await _require_agent(self.db, self.user, agent_id, enabled=True)
        thread = await self.db.scalar(select(AgentStudioThread).where(
            AgentStudioThread.id == thread_id,
            AgentStudioThread.agent_id == agent_id,
            AgentStudioThread.owner_id == self.user.id,
        ))
        if thread is None:
            raise NotFoundError(_NOT_FOUND)
        if not prompt.strip() or len(prompt) > 6000:
            raise InvalidInputError("Enter a task of up to 6,000 characters.")
        scheduled_key = scheduled_at.astimezone(UTC).isoformat() if scheduled_at else None
        clean_prompt = prompt.strip()
        digest = None
        if idempotency_key is not None:
            if not idempotency_key.strip() or len(idempotency_key) > 256:
                raise InvalidInputError("Idempotency keys must contain 1 to 256 characters.")
            digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
            existing = await self._existing_idempotent_run(digest, agent_id, thread_id, clean_prompt, scheduled_key)
            if existing is not None:
                return existing
        history = decode_thread_history(self.cipher, thread)
        memory = await _load_memories(self.db, self.user, agent, self.cipher)
        state: dict[str, Any] = {
            "user_input": clean_prompt,
            "scheduled_at": scheduled_key,
            "conversation": history[-_MAX_HISTORY_MESSAGES:],
            "memory": memory,
            "steps": [],
            "step_count": 0,
            "max_steps": agent.max_steps,
            "pending_tool": None,
            "approval_granted": False,
            "approval_rejected": False,
            "awaiting_approval": False,
            "agent_id": str(agent.id),
            "visited_agents": [str(agent.id)],
            "usage": {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0},
        }
        run_id = uuid.uuid4()
        sealed = _encode_state(self.cipher, run_id, state)
        run = AgentStudioRun(
            id=run_id,
            agent_id=agent.id,
            thread_id=thread.id,
            owner_id=self.user.id,
            status="queued",
            state_ciphertext=sealed.ciphertext,
            state_nonce=sealed.nonce,
            state_key_id=sealed.key_id,
            idempotency_key=digest,
            next_attempt_at=scheduled_at or datetime.now(UTC),
            provider=agent.provider_id,
            model=agent.model,
        )
        self.db.add(run)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            if digest is None:
                raise
            existing = await self._existing_idempotent_run(digest, agent_id, thread_id, clean_prompt, scheduled_key)
            if existing is None:
                raise
            return existing
        await self.db.refresh(run)
        if not execute_inline:
            return run, state, None
        if await _paused_now(self.db, agent.id):
            # Pause applies to every entry point (WhatsApp Cloud, Baileys, inline API): the task stays queued and
            # the worker runs it only after the agent is resumed. Callers report it as queued, not completed.
            await self.db.commit()
            return run, state, None
        run.status = "running"
        run.attempt_count = 1
        run.started_at = datetime.now(UTC)
        run.claimed_at = run.started_at
        claim_attempt = run.attempt_count
        claim_time = run.claimed_at
        await self.db.commit()
        return await self._execute(
            run,
            agent,
            thread,
            state,
            allow_retry=not execute_inline,
            expected_attempt=claim_attempt,
            expected_claimed_at=claim_time,
        )

    async def decide(
        self, run_id: uuid.UUID, decision: str, note: str, user: User
    ) -> tuple[AgentStudioRun, dict[str, Any], AgentStudioApproval | None]:
        if decision not in {"approve", "reject"}:
            raise InvalidInputError("Decision must be approve or reject.")
        run = await self.db.scalar(
            select(AgentStudioRun).where(
                AgentStudioRun.id == run_id, AgentStudioRun.owner_id == user.id
            ).with_for_update()
        )
        if run is None:
            raise NotFoundError(_NOT_FOUND)
        if run.status != "awaiting_approval":
            raise ConflictError("This agent run is no longer waiting for approval.")
        approval = await self.db.scalar(
            select(AgentStudioApproval).where(
                AgentStudioApproval.run_id == run.id,
                AgentStudioApproval.owner_id == user.id,
                AgentStudioApproval.status == "pending",
            ).with_for_update()
        )
        if approval is None:
            raise ConflictError("The pending approval record is missing.")
        state = decode_run_state(self.cipher, run)
        approved_action = json.loads(_open(self.cipher, "approval-action", approval.id, approval.action_ciphertext, approval.action_nonce, approval.action_key_id))
        if not isinstance(approved_action, dict) or state.get("pending_tool") != approved_action or approved_action.get("name") != approval.tool_name:
            raise ConflictError("The saved agent action does not match its approval record.")
        if decision == "approve" and datetime.now(UTC) - approval.created_at > timedelta(hours=1):
            raise ConflictError("This approval expired. Reject it or start a fresh run.")
        approval.status = "approved" if decision == "approve" else "rejected"
        approval.decision_note = note.strip()[:500] or None
        approval.decided_by_user_id = user.id
        approval.decided_at = datetime.now(UTC)
        run.status = "queued"
        run.next_attempt_at = datetime.now(UTC)
        run.claimed_at = None
        run.finished_at = None
        state["approval_granted"] = decision == "approve"
        state["approval_rejected"] = decision == "reject"
        thread = await self.db.scalar(select(AgentStudioThread).where(
            AgentStudioThread.id == run.thread_id, AgentStudioThread.owner_id == user.id
        ))
        agent = await _require_agent(self.db, user, run.agent_id)
        sealed = _encode_state(self.cipher, run.id, state)
        run.state_ciphertext, run.state_nonce, run.state_key_id = sealed.ciphertext, sealed.nonce, sealed.key_id
        await self.db.commit()
        return run, state, None

    async def execute_claimed(
        self,
        run_id: uuid.UUID,
        *,
        expected_attempt: int,
        expected_claimed_at: datetime,
    ) -> tuple[AgentStudioRun, dict[str, Any], AgentStudioApproval | None]:
        run = await self.db.scalar(select(AgentStudioRun).where(
            AgentStudioRun.id == run_id,
            AgentStudioRun.owner_id == self.user.id,
            AgentStudioRun.status == "running",
            AgentStudioRun.attempt_count == expected_attempt,
            AgentStudioRun.claimed_at == expected_claimed_at,
        ))
        if run is None:
            raise _AgentRunClaimLost
        agent = await _require_agent(self.db, self.user, run.agent_id)
        if await _paused_now(self.db, agent.id):
            # Paused between the worker's claim and execution (including a run resumed by an approval): hand the
            # claim back so nothing runs until the agent is resumed. The attempt is not counted.
            run.status = "queued"
            run.attempt_count = max(0, run.attempt_count - 1)
            run.claimed_at = None
            run.next_attempt_at = datetime.now(UTC)
            await self.db.commit()
            await self.db.refresh(run)
            return run, decode_run_state(self.cipher, run), None
        thread = await self.db.scalar(select(AgentStudioThread).where(
            AgentStudioThread.id == run.thread_id,
            AgentStudioThread.owner_id == self.user.id,
        ))
        if thread is None:
            raise NotFoundError(_NOT_FOUND)
        state = decode_run_state(self.cipher, run)
        if run.usage_by_model:
            state["usage"] = {
                "llm_calls": run.llm_calls,
                "prompt_tokens": run.prompt_tokens,
                "completion_tokens": run.completion_tokens,
                "models": dict(run.usage_by_model),
            }
        return await self._execute(
            run,
            agent,
            thread,
            state,
            expected_attempt=expected_attempt,
            expected_claimed_at=expected_claimed_at,
        )

    async def _execute(
        self,
        run: AgentStudioRun,
        agent: AgentStudioAgent,
        thread: AgentStudioThread,
        state: dict[str, Any],
        *,
        allow_retry: bool = True,
        expected_attempt: int,
        expected_claimed_at: datetime,
    ) -> tuple[AgentStudioRun, dict[str, Any], AgentStudioApproval | None]:
        claimed_run_id = run.id
        claimed_owner_id = self.user.id
        pending_approval = None
        usage_tracker = dict(state.get("usage") or {})
        usage_tracker.setdefault("llm_calls", 0)
        usage_tracker.setdefault("prompt_tokens", 0)
        usage_tracker.setdefault("completion_tokens", 0)
        usage_tracker.setdefault("models", {})

        def seal_run_state(run_state: dict[str, Any]) -> None:
            sealed = _encode_state(self.cipher, run.id, run_state)
            run.state_ciphertext, run.state_nonce, run.state_key_id = sealed.ciphertext, sealed.nonce, sealed.key_id

        def store_usage(snapshot: dict[str, Any]) -> None:
            usage_tracker.update(snapshot)
            model_rows = dict(run.usage_by_model or {})
            for model_key, usage in (snapshot.get("models") or {}).items():
                existing = dict(model_rows.get(model_key) or {})
                rate = self.settings.llm_model_pricing_json.get(model_key) or {}
                existing.update({
                    "provider": usage.get("provider"),
                    "model": usage.get("model"),
                    "prompt_tokens": max(0, int(usage.get("prompt_tokens", 0))),
                    "completion_tokens": max(0, int(usage.get("completion_tokens", 0))),
                    "llm_calls": max(0, int(usage.get("llm_calls", 0))),
                })
                if "input_per_1m_tokens_usd" not in existing:
                    existing["input_per_1m_tokens_usd"] = rate.get("input_per_1m_tokens_usd")
                    existing["output_per_1m_tokens_usd"] = rate.get("output_per_1m_tokens_usd")
                model_rows[model_key] = existing
            run.usage_by_model = model_rows
            run.prompt_tokens = max(0, int(snapshot.get("prompt_tokens", 0)))
            run.completion_tokens = max(0, int(snapshot.get("completion_tokens", 0)))
            run.llm_calls = max(0, int(snapshot.get("llm_calls", 0)))
            run.provider = str(snapshot.get("provider") or run.provider or "")[:80] or None
            run.model = str(snapshot.get("model") or run.model or "")[:160] or None
            total_cost = Decimal("0")
            all_priced = bool(model_rows)
            for usage in model_rows.values():
                input_rate = usage.get("input_per_1m_tokens_usd")
                output_rate = usage.get("output_per_1m_tokens_usd")
                if input_rate is None or output_rate is None:
                    all_priced = False
                    break
                total_cost += (
                    Decimal(int(usage.get("prompt_tokens", 0))) * Decimal(str(input_rate))
                    + Decimal(int(usage.get("completion_tokens", 0))) * Decimal(str(output_rate))
                ) / Decimal(1_000_000)
            run.estimated_cost_usd = total_cost.quantize(Decimal("0.00000001")) if all_priced else None

        async def assert_current_claim() -> None:
            # This is an identity-only query so a stale ORM object cannot flush its
            # status before the ownership predicate is checked. The row lock is held
            # through the following commit, fencing recovery/cancellation updates.
            with self.db.no_autoflush:
                owned_id = await self.db.scalar(
                    select(AgentStudioRun.id)
                    .where(
                        AgentStudioRun.id == claimed_run_id,
                        AgentStudioRun.owner_id == claimed_owner_id,
                        AgentStudioRun.status == "running",
                        AgentStudioRun.attempt_count == expected_attempt,
                        AgentStudioRun.claimed_at == expected_claimed_at,
                    )
                    .with_for_update()
                )
            if owned_id is None:
                raise _AgentRunClaimLost

        async def return_current_after_claim_loss():
            await self.db.rollback()
            current = await self.db.scalar(
                select(AgentStudioRun)
                .where(AgentStudioRun.id == claimed_run_id, AgentStudioRun.owner_id == claimed_owner_id)
                .execution_options(populate_existing=True)
            )
            if current is None:
                raise NotFoundError(_NOT_FOUND)
            return current, decode_run_state(self.cipher, current), None

        async def persist_usage(snapshot: dict[str, Any]) -> None:
            await assert_current_claim()
            store_usage(snapshot)
            await self.db.commit()

        async def persist_progress(progress_state: AgentState) -> None:
            await assert_current_claim()
            if run.status != "running":
                return
            saved = dict(progress_state)
            saved["usage"] = dict(usage_tracker)
            saved["action_inflight"] = False
            store_usage(usage_tracker)
            seal_run_state(saved)
            await self.db.commit()

        try:
            await assert_current_claim()
            await self.db.commit()
            choice = (agent.provider_id, agent.model) if agent.provider_id and agent.model else None
            llm = await self.runtime.get_client(self.db, choice)

            async def dispatch(name: str, arguments: dict[str, Any], graph_state: AgentState) -> dict[str, Any]:
                await assert_current_claim()
                inflight = dict(graph_state)
                inflight["usage"] = dict(usage_tracker)
                inflight["action_inflight"] = True
                seal_run_state(inflight)
                await self.db.commit()
                if name == "delegate_agent":
                    return await self._delegate(
                        agent, llm, name, arguments, graph_state,
                        usage_tracker=usage_tracker,
                        on_usage=persist_usage,
                    )
                return await run_tool(
                    name,
                    arguments,
                    db=self.db,
                    user=self.user,
                    agent=agent,
                    settings=self.settings,
                    cipher=self.cipher,
                    llm=llm,
                    rag=self.rag if agent.rag_enabled else None,
                )

            graph = build_agent_graph(
                agent,
                llm,
                dispatch,
                user_email=self.user.email,
                usage_tracker=usage_tracker,
                on_usage=persist_usage,
                on_progress=persist_progress,
            )
            result = await asyncio.wait_for(
                graph.ainvoke(state, config={"recursion_limit": max(30, agent.max_steps * 5)}),
                timeout=self.settings.agent_studio_run_timeout_seconds,
            )
            result = dict(result)
            result["usage"] = dict(usage_tracker)
            result["action_inflight"] = False
            store_usage(usage_tracker)
            if not result.get("awaiting_approval") and not str(result.get("answer") or "").strip():
                result["failure_code"] = result.get("failure_code") or "missing_result"
            if any(step.get("status") == "failed" for step in (result.get("steps") or []) if isinstance(step, dict)):
                result["failure_code"] = result.get("failure_code") or "tool_execution_failed"
            run.failure_code = str(result.get("failure_code") or "")[:64] or None
            run.status = "failed" if run.failure_code else (
                "awaiting_approval" if result.get("awaiting_approval") else "completed"
            )
            if result.get("awaiting_approval"):
                run.claimed_at = None
                pending = result.get("pending_tool") or {}
                action_id = uuid.uuid4()
                action = _seal(self.cipher, "approval-action", action_id, json.dumps(pending, ensure_ascii=False))
                pending_approval = AgentStudioApproval(
                    id=action_id,
                    run_id=run.id,
                    owner_id=self.user.id,
                    tool_name=str(pending.get("name") or ""),
                    action_ciphertext=action.ciphertext,
                    action_nonce=action.nonce,
                    action_key_id=action.key_id,
                )
                self.db.add(pending_approval)
            else:
                await self._append_history(thread, result)
                run.finished_at = datetime.now(UTC)
                run.claimed_at = None
            seal_run_state(result)
            await assert_current_claim()
            await self.db.commit()
            await self.db.refresh(run)
            if pending_approval is not None:
                await self.db.refresh(pending_approval)
            return run, result, pending_approval
        except _AgentRunClaimLost:
            return await return_current_after_claim_loss()
        except Exception as exc:
            logger.error("Agent Studio run failed run_id=%s error_type=%s", run.id, type(exc).__name__)
            failed_run_id = run.id
            await self.db.rollback()
            try:
                await assert_current_claim()
            except _AgentRunClaimLost:
                return await return_current_after_claim_loss()
            run = await self.db.scalar(
                select(AgentStudioRun)
                .where(AgentStudioRun.id == failed_run_id, AgentStudioRun.owner_id == claimed_owner_id)
                .execution_options(populate_existing=True)
            )
            if run is None:
                raise
            current_state = decode_run_state(self.cipher, run)
            current_usage = dict(usage_tracker)
            result = {
                **current_state,
                "usage": current_usage,
                "action_inflight": bool(current_state.get("action_inflight")),
                "answer": "I couldn't complete this agent run. Check the configured AI provider and retry.",
                "awaiting_approval": False,
            }
            store_usage(current_usage)
            run.claimed_at = None
            retryable = (
                allow_retry
                and run.attempt_count < 3
                and self._is_transient_error(exc)
                and not current_state.get("action_inflight")
                and not current_state.get("approval_granted")
                and not current_state.get("steps")
            )
            if retryable:
                run.status = "queued"
                run.failure_code = None
                run.finished_at = None
                delay = min(60, 2 ** max(1, run.attempt_count))
                run.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
                result["answer"] = f"A temporary provider or network error occurred. The task is queued for retry in {delay} seconds."
                result["retry_error_code"] = type(exc).__name__[:64]
            else:
                run.status = "failed"
                run.failure_code = type(exc).__name__[:64]
                run.finished_at = datetime.now(UTC)
                result["answer"] = (
                    "The worker stopped while an action may have been in progress. Review the run before retrying."
                    if current_state.get("action_inflight")
                    else "I couldn't complete this agent run. Check the configured AI provider and retry."
                )
            seal_run_state(result)
            await self.db.commit()
            await self.db.refresh(run)
            return run, result, None

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code == 429 or (isinstance(status_code, int) and status_code >= 500):
            return True
        name = type(exc).__name__.casefold()
        return any(part in name for part in ("timeout", "connection", "ratelimit", "serviceunavailable", "internalserver"))

    async def _append_history(self, thread: AgentStudioThread, state: dict[str, Any]) -> None:
        history = decode_thread_history(self.cipher, thread)
        history.extend([
            {"role": "user", "content": str(state.get("user_input") or "")[:6000]},
            {"role": "assistant", "content": str(state.get("answer") or "")[:12000]},
        ])
        history = history[-_MAX_HISTORY_MESSAGES:]
        sealed = _encode_thread(self.cipher, thread.id, history)
        thread.history_ciphertext, thread.history_nonce, thread.history_key_id = sealed.ciphertext, sealed.nonce, sealed.key_id
        thread.updated_at = datetime.now(UTC)
        if thread.title == "New agent run":
            thread.title = re.sub(r"\s+", " ", str(state.get("user_input") or ""))[:180] or thread.title

    async def _delegate(
        self,
        parent: AgentStudioAgent,
        llm: LLMClient,
        _name: str,
        arguments: dict[str, Any],
        state: AgentState,
        *,
        usage_tracker: dict[str, Any],
        on_usage,
    ) -> dict[str, Any]:
        target_text = str(arguments.get("agent_id") or "")
        try:
            target_id = uuid.UUID(target_text)
        except ValueError:
            raise ValueError("Provide the ID of an allowed subagent.") from None
        allowed = {uuid.UUID(item) for item in (parent.delegate_agent_ids or [])}
        if target_id not in allowed:
            raise ValueError("This agent cannot delegate to that agent.")
        visited = [str(value) for value in (state.get("visited_agents") or [])]
        if str(target_id) in visited or len(visited) >= _MAX_DELEGATION_DEPTH:
            raise ValueError("Delegation depth or cycle limit reached.")
        child = await _require_agent(self.db, self.user, target_id, enabled=True)
        if child.paused:
            raise ValueError("That subagent is paused, so it cannot take delegated work until it is resumed.")
        if set(child.approval_tools or []) & APPROVAL_TOOLS:
            raise ValueError("A delegated subagent can only use read-only or private-memory tools.")
        task = str(arguments.get("task") or "").strip()
        if not task or len(task) > 3000:
            raise ValueError("Provide a subtask of up to 3,000 characters.")
        choice = (child.provider_id, child.model) if child.provider_id and child.model else None
        child_llm = await self.runtime.get_client(self.db, choice)

        async def child_dispatch(tool: str, args: dict[str, Any], child_state: AgentState) -> dict[str, Any]:
            if tool == "delegate_agent":
                return await self._delegate(
                    child, child_llm, tool, args, child_state,
                    usage_tracker=usage_tracker,
                    on_usage=on_usage,
                )
            return await run_tool(
                tool,
                args,
                db=self.db,
                user=self.user,
                agent=child,
                settings=self.settings,
                cipher=self.cipher,
                llm=child_llm,
                rag=self.rag if child.rag_enabled else None,
            )

        graph = build_agent_graph(
            child, child_llm, child_dispatch, user_email=self.user.email,
            usage_tracker=usage_tracker, on_usage=on_usage,
        )
        child_state: AgentState = {
            "user_input": task,
            "conversation": [],
            "memory": await _load_memories(self.db, self.user, child, self.cipher),
            "steps": [],
            "step_count": 0,
            "max_steps": min(4, child.max_steps),
            "pending_tool": None,
            "awaiting_approval": False,
            "agent_id": str(child.id),
            "visited_agents": [*visited, str(child.id)],
        }
        outcome = dict(await graph.ainvoke(child_state, config={"recursion_limit": 24}))
        if outcome.get("awaiting_approval"):
            raise ValueError("Delegated agents cannot pause for approval; run side-effecting work directly.")
        return {
            "agent": child.name,
            "answer": str(outcome.get("answer") or "")[:12000],
            "steps": outcome.get("steps") or [],
            "usage": usage_tracker,
        }


def public_run(
    run: AgentStudioRun,
    state: dict[str, Any],
    approval: AgentStudioApproval | None = None,
    cipher: CredentialCipher | None = None,
) -> dict[str, Any]:
    pending = None
    if approval is not None:
        action = {}
        try:
            if cipher is None:
                raise ValueError("Missing approval decryption key.")
            action = json.loads(_open(
                cipher, "approval-action", approval.id,
                approval.action_ciphertext, approval.action_nonce, approval.action_key_id,
            ))
        except Exception:
            action = {}
        tool_name = approval.tool_name
        if tool_name == "create_file":
            summary = f"Create a downloadable {str((action.get('arguments') or {}).get('format') or 'document').upper()} file."
        elif tool_name == "generate_image":
            summary = "Generate an image using the configured AI provider."
        else:
            summary = "Run an action that requires your approval."
        pending = {"id": approval.id, "tool_name": tool_name, "summary": summary}
    return {
        "id": run.id,
        "agent_id": run.agent_id,
        "thread_id": run.thread_id,
        "status": run.status,
        "prompt": str(state.get("user_input") or ""),
        "answer": state.get("answer"),
        "steps": state.get("steps") or [],
        "pending_approval": pending,
        "failure_code": run.failure_code,
        "attempt_count": run.attempt_count,
        "next_attempt_at": run.next_attempt_at,
        "provider": run.provider,
        "model": run.model,
        "prompt_tokens": run.prompt_tokens,
        "completion_tokens": run.completion_tokens,
        "llm_calls": run.llm_calls,
        "estimated_cost_usd": float(run.estimated_cost_usd) if run.estimated_cost_usd is not None else None,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }
