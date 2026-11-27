"""Pausing an agent stops it on every entry point: inline (WhatsApp) runs, approval resumes and delegation."""

import json
import uuid
from datetime import UTC, datetime

import pytest

from app.agent_studio.service import AgentStudioService, _encode_state, _seal, decode_run_state
from app.agent_studio.worker import process_next_agent_studio_run
from app.core.config import get_settings
from app.db.agent_studio_models import AgentStudioAgent, AgentStudioApproval, AgentStudioRun, AgentStudioThread
from app.db.models import User
from app.main import app
from tests.test_agent_studio_queue import _create_agent_and_thread, _enqueue


def _process_args():
    return (app.state.sessionmaker, get_settings(), app.state.cipher, app.state.llm_runtime, app.state.rag)


def _stub_execute(monkeypatch):
    executed: list[uuid.UUID] = []

    async def verified_execute(self, run, _agent, _thread, state, *, allow_retry=True, expected_attempt, expected_claimed_at):
        executed.append(run.id)
        state["answer"] = "Verified synthetic result."
        run.status, run.failure_code, run.finished_at, run.claimed_at = "completed", None, datetime.now(UTC), None
        sealed = _encode_state(self.cipher, run.id, state)
        run.state_ciphertext, run.state_nonce, run.state_key_id = sealed.ciphertext, sealed.nonce, sealed.key_id
        await self.db.commit()
        return run, state, None

    monkeypatch.setattr(AgentStudioService, "_execute", verified_execute)
    return executed


def _service(db, user):
    return AgentStudioService(db, user, get_settings(), app.state.cipher, app.state.llm_runtime, app.state.rag)


def _start_inline(client, agent_id, thread_id, prompt, key):
    """The call every WhatsApp adapter makes (Cloud, legacy Cloud and Baileys)."""
    async def go():
        async with app.state.sessionmaker() as db:
            agent = await db.get(AgentStudioAgent, uuid.UUID(agent_id))
            user = await db.get(User, agent.owner_id)
            run, state, _ = await _service(db, user).start_run(
                agent.id, uuid.UUID(thread_id), prompt, idempotency_key=key, execute_inline=True
            )
            return run.id, run.status, state.get("answer")

    return client.portal.call(go)


def test_inline_whatsapp_run_waits_while_paused_and_runs_after_resume(admin_client, clean_tables, monkeypatch):
    executed = _stub_execute(monkeypatch)
    agent_id, thread_id = _create_agent_and_thread(admin_client)

    run_id, status, answer = _start_inline(admin_client, agent_id, thread_id, "Before pause", "whatsapp-cloud:a")
    assert (status, answer) == ("completed", "Verified synthetic result.") and executed == [run_id]

    assert admin_client.post(f"/api/agent-studio/agents/{agent_id}/pause").json()["paused"] is True
    paused_run, status, answer = _start_inline(admin_client, agent_id, thread_id, "While paused", "whatsapp-cloud:b")
    assert (status, answer) == ("queued", None)
    assert executed == [run_id]  # nothing executed for the paused agent
    assert admin_client.portal.call(process_next_agent_studio_run, *_process_args()) is False

    assert admin_client.post(f"/api/agent-studio/agents/{agent_id}/resume").json()["paused"] is False
    assert admin_client.portal.call(process_next_agent_studio_run, *_process_args()) is True
    assert executed == [run_id, paused_run]
    assert admin_client.get(f"/api/agent-studio/runs/{paused_run}").json()["status"] == "completed"


def test_approved_action_does_not_resume_while_the_agent_is_paused(admin_client, clean_tables, monkeypatch):
    executed = _stub_execute(monkeypatch)
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    queued = _enqueue(admin_client, agent_id, thread_id, "Needs approval", "approval-pause")
    assert queued.status_code == 202, queued.text
    run_id = uuid.UUID(queued.json()["id"])
    pending = {"name": "web_search", "arguments": {"query": "synthetic"}}

    async def wait_for_approval():
        async with app.state.sessionmaker() as db:
            run = await db.get(AgentStudioRun, run_id)
            state = decode_run_state(app.state.cipher, run)
            state.update(pending_tool=pending, awaiting_approval=True)
            sealed = _encode_state(app.state.cipher, run.id, state)
            run.state_ciphertext, run.state_nonce, run.state_key_id = sealed.ciphertext, sealed.nonce, sealed.key_id
            run.status = "awaiting_approval"
            approval_id = uuid.uuid4()
            action = _seal(app.state.cipher, "approval-action", approval_id, json.dumps(pending))
            db.add(AgentStudioApproval(
                id=approval_id, run_id=run.id, owner_id=run.owner_id, tool_name="web_search",
                action_ciphertext=action.ciphertext, action_nonce=action.nonce, action_key_id=action.key_id,
            ))
            await db.commit()

    admin_client.portal.call(wait_for_approval)
    assert admin_client.post(f"/api/agent-studio/agents/{agent_id}/pause").status_code == 200
    decided = admin_client.post(f"/api/agent-studio/runs/{run_id}/decision", json={"decision": "approve", "note": ""})
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "queued"
    assert admin_client.portal.call(process_next_agent_studio_run, *_process_args()) is False
    assert executed == []

    assert admin_client.post(f"/api/agent-studio/agents/{agent_id}/resume").status_code == 200
    assert admin_client.portal.call(process_next_agent_studio_run, *_process_args()) is True
    assert executed == [run_id]


def test_a_run_claimed_just_before_a_pause_hands_its_claim_back(admin_client, clean_tables, monkeypatch):
    executed = _stub_execute(monkeypatch)
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    run_id = uuid.UUID(_enqueue(admin_client, agent_id, thread_id, "Race the pause", "claim-pause").json()["id"])

    async def claim_then_pause_then_execute():
        async with app.state.sessionmaker() as db:
            run = await db.get(AgentStudioRun, run_id)
            claimed_at = datetime.now(UTC)
            run.status, run.attempt_count, run.claimed_at, run.started_at = "running", 1, claimed_at, claimed_at
            agent = await db.get(AgentStudioAgent, run.agent_id)
            agent.paused = True
            await db.commit()
            user = await db.get(User, run.owner_id)
            result, _, _ = await _service(db, user).execute_claimed(
                run_id, expected_attempt=1, expected_claimed_at=claimed_at
            )
            return result.status, result.attempt_count, result.claimed_at

    assert admin_client.portal.call(claim_then_pause_then_execute) == ("queued", 0, None)
    assert executed == []


def test_a_paused_subagent_refuses_delegated_work(admin_client, clean_tables):
    child_id, _ = _create_agent_and_thread(admin_client)
    parent_id, _ = _create_agent_and_thread(admin_client)
    assert admin_client.post(f"/api/agent-studio/agents/{child_id}/pause").status_code == 200

    async def delegate():
        async with app.state.sessionmaker() as db:
            parent = await db.get(AgentStudioAgent, uuid.UUID(parent_id))
            parent.delegate_agent_ids = [child_id]
            await db.commit()
            user = await db.get(User, parent.owner_id)
            with pytest.raises(ValueError, match="paused"):
                await _service(db, user)._delegate(
                    parent, None, "delegate_agent", {"agent_id": child_id, "task": "Summarise"},
                    {"visited_agents": [parent_id]}, usage_tracker={}, on_usage=None,
                )

    admin_client.portal.call(delegate)
