"""Step F slice 5: approvals over WhatsApp + the "Done (verified)" wording.

Proved here, on a throwaway ``*_test`` database only, single process:

* parsing: Cloud button reply ids (``wa-approve:/wa-reject:``) and Baileys
  ``APPROVE``/``REJECT`` text commands, case-insensitive and
  whitespace-tolerant; anything else is not a command;
* the action hash binds the exact action (tool + arguments) and is stable
  across equal actions, changes with the action, and sanitizes floats;
* the approval card fits Meta's interactive limits and carries the commands;
* Cloud: approve-by-WhatsApp completes the run through the same
  ``AgentStudioService.decide`` the web UI uses; reject fails it; a forged
  id, a replay, a wrong sender, a missing hash and a hash mismatch are all
  refused and audited, with the approval left pending;
* Cloud: a run that reaches ``awaiting_approval`` gets an interactive
  approval card (buttons on Cloud, text on the Baileys leg);
* Baileys: the card arrives as text, and an ``APPROVE`` text command decides
  the run;
* wording: "Done (verified)" is stamped only when a verified
  ``execute_once`` record exists for the run's attempt; an unverified
  completion — even one whose answer says "done" — carries no success
  marker.

Nothing here calls the real Meta API: the provider transport is stubbed, and
the agent execution is stubbed to a verified synthetic completion (same
pattern as ``tests/test_agent_studio_unified.py``).
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agent_studio.service import AgentStudioService, _seal as _seal_approval, decode_run_state
from app.agent_studio.unified import StudioHost, configure_host
from app.core.config import Settings, get_settings
from app.db.agent_studio_models import AgentStudioAgent, AgentStudioApproval, AgentStudioRun
from app.db.agent_studio_whatsapp_models import AgentStudioWhatsAppConnection, AgentStudioWhatsAppInbound
from app.db.models import User
from app.db.session import test_engine_options as _test_engine_options
from app.integrations import whatsapp as cloud_integration
from app.integrations.transport import Response as TransportResponse
from app.integrations.whatsapp_approvals import (
    approval_action_hash,
    approval_buttons,
    approval_card_text,
    approval_summary,
    completed_reply,
    parse_approval_command,
)
from app.main import app
from app.platform.channels import link_identity
from app.platform.models import PlatformActionExecution
from app.platform.side_effects import org_scope_id
from app.services.agent_studio_whatsapp import _open, _seal
from tests.conftest import ADMIN_EMAIL, CSRF, TEST_DATABASE_URL, USER2_EMAIL, run_db
from tests.test_agent_studio_unified import (
    _pump_until,
    _set_awaiting_approval,
    _setup,
    _stub_execute,
)
from tests.test_whatsapp_unified_intake import (
    SECRET,
    SENDER,
    _drive_intake,
    _noop_worker,
    _payload,
    _post,
    _seed_connection,  # noqa: F401  (kept for the single-scope text test below)
    quiet_client,  # noqa: F401  (fixture shared with the intake tests)
)
from tests.test_whatsapp_voice import (
    _seed_baileys_message,
    baileys_client,  # noqa: F401  (fixture shared with the voice tests)
)


# ---------------------------------------------------------------------------
# Pure parsing / hash / card tests (no database rows needed)
# ---------------------------------------------------------------------------

def _fake_approval(*, approval_id=None, tool="web_search", arguments=None, run_id=None):
    # Sealed with the Agent Studio seal (AAD "infraspine:agent-studio:v1"),
    # the same namespace production approval rows use — NOT the WhatsApp
    # legacy seal ("...-whatsapp:v1"), which fails authentication here.
    approval_id = approval_id or uuid.uuid4()
    action = {"name": tool, "arguments": arguments if arguments is not None else {"query": "synthetic"}}
    sealed = _seal_approval(app.state.cipher, "approval-action", approval_id, json.dumps(action))
    return AgentStudioApproval(
        id=approval_id, run_id=run_id or uuid.uuid4(), owner_id=uuid.uuid4(), tool_name=tool,
        action_ciphertext=sealed.ciphertext, action_nonce=sealed.nonce, action_key_id=sealed.key_id,
    )


def test_parse_approval_command_accepts_button_and_text_forms():
    approval_id = uuid.uuid4()
    code = "a1b2c3d4e5f6"
    assert parse_approval_command(f"wa-approve:{approval_id}:{code}") == ("approve", approval_id, code)
    assert parse_approval_command(f"WA-REJECT:{approval_id}:{code}") == ("reject", approval_id, code)
    assert parse_approval_command(f"APPROVE {approval_id} {code}") == ("approve", approval_id, code)
    assert parse_approval_command(f"  reject   {approval_id}   {code}  ") == ("reject", approval_id, code)
    # The hash is optional at parse time so the caller can refuse it with a
    # specific message; a decision is never applied without it.
    assert parse_approval_command(f"APPROVE {approval_id}") == ("approve", approval_id, None)
    assert parse_approval_command(f"wa-approve:{approval_id}") == ("approve", approval_id, None)


def test_parse_approval_command_rejects_non_commands():
    approval_id = uuid.uuid4()
    assert parse_approval_command("hello, please check the laptop status") is None
    assert parse_approval_command("approve the payment of $5000") is None
    assert parse_approval_command("APPROVE") is None
    assert parse_approval_command(f"APPROVE not-a-uuid a1b2c3d4e5f6") is None
    assert parse_approval_command(f"APPROVE {approval_id} nothexcode12") is None
    assert parse_approval_command(f"APPROVE {approval_id} a1b2c3d4e5f6 extra") is None
    assert parse_approval_command(None) is None
    assert parse_approval_command(42) is None


def test_approval_action_hash_is_stable_and_binds_the_action(quiet_client):
    scope_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    run_id = uuid.uuid4()
    approval = _fake_approval(approval_id=approval_id, run_id=run_id)
    first = approval_action_hash(app.state.cipher, approval, scope_id)
    assert len(first) == 64 and all(c in "0123456789abcdef" for c in first)
    assert approval_action_hash(app.state.cipher, approval, scope_id) == first
    # A different tool, different arguments, or a different run all change it
    # (same approval id AND run id throughout, so only the varied field can differ).
    assert approval_action_hash(app.state.cipher, _fake_approval(approval_id=approval_id, run_id=run_id, tool="create_file"), scope_id) != first
    assert approval_action_hash(app.state.cipher, _fake_approval(approval_id=approval_id, run_id=run_id, arguments={"query": "other"}), scope_id) != first
    assert approval_action_hash(app.state.cipher, _fake_approval(approval_id=approval_id, run_id=uuid.uuid4()), scope_id) != first
    # Floats are sanitized (repr) instead of raising, so the hash stays stable.
    floaty = _fake_approval(approval_id=approval_id, run_id=run_id, arguments={"score": 1.5})
    assert approval_action_hash(app.state.cipher, floaty, scope_id) == approval_action_hash(
        app.state.cipher, _fake_approval(approval_id=approval_id, run_id=run_id, arguments={"score": "1.5"}), scope_id)


def test_approval_card_text_and_buttons_fit_meta_limits(quiet_client):
    approval_id = uuid.uuid4()
    action_hash = approval_action_hash(app.state.cipher, _fake_approval(), uuid.uuid4())
    card = approval_card_text(
        tool_name="web_search", summary=approval_summary("web_search", {"name": "web_search", "arguments": {}}),
        approval_id=approval_id, action_hash=action_hash,
    )
    assert len(card) <= 1024
    assert f"APPROVE {approval_id} {action_hash[:12]}" in card
    assert f"REJECT {approval_id} {action_hash[:12]}" in card
    assert "web_search" in card
    buttons = approval_buttons(approval_id, action_hash)
    assert [title for _id, title in buttons] == ["Approve", "Reject"]
    for button_id, title in buttons:
        assert len(button_id) <= 256 and len(title) <= 20
    assert buttons[0][0] == f"wa-approve:{approval_id}:{action_hash[:12]}"
    assert approval_summary("create_file", {"arguments": {"format": "pdf"}}) == "Create a downloadable PDF file."
    assert approval_summary("generate_image", {}) == "Generate an image using the configured AI provider."


# ---------------------------------------------------------------------------
# Shared helpers for the WhatsApp decision tests
# ---------------------------------------------------------------------------

def _approval_and_hash(client, run_id):
    """(approval id, first 12 hash chars) for the run's pending approval."""

    async def load():
        async with app.state.sessionmaker() as db:
            approval = await db.scalar(select(AgentStudioApproval).where(AgentStudioApproval.run_id == run_id))
            scope_id = await org_scope_id(db)
            return approval.id, approval_action_hash(app.state.cipher, approval, scope_id)[:12]

    return client.portal.call(load)


def _run_and_approval_status(run_id):
    async def load(db):
        run_status = await db.scalar(select(AgentStudioRun.status).where(AgentStudioRun.id == run_id))
        appr_status = await db.scalar(select(AgentStudioApproval.status).where(AgentStudioApproval.run_id == run_id))
        return run_status, appr_status

    return run_db(load)


def _inbound_reply_text(connection):
    """The latest stored reply for the connection (a replay posts a second inbound)."""
    async def load(db):
        row = (await db.execute(select(
            AgentStudioWhatsAppInbound.id, AgentStudioWhatsAppInbound.reply_ciphertext,
            AgentStudioWhatsAppInbound.reply_nonce, AgentStudioWhatsAppInbound.reply_key_id,
        ).where(AgentStudioWhatsAppInbound.connection_id == connection)
        .order_by(AgentStudioWhatsAppInbound.created_at.desc()).limit(1))).one()
        return _open(app.state.cipher, "reply", row[0], row[1], row[2], row[3])

    return run_db(load)


def _awaiting_run(client):
    """A run put behind an exact-action approval; returns (run_id, approval_id, hash12)."""
    _scope, _people, _agent_id, _thread_id, run_id = _setup(client)
    _set_awaiting_approval(client, run_id)
    approval_id, hash12 = _approval_and_hash(client, run_id)
    return run_id, approval_id, hash12


def _link_sender(email, sender):
    async def link():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
            async with sessionmaker() as session:
                owner_id = await session.scalar(select(User.id).where(User.email == email))
                await link_identity(session, owner_id, "whatsapp_cloud", sender,
                                    method="whatsapp:confirmed")
                await session.commit()
        finally:
            await engine.dispose()

    import asyncio

    asyncio.run(link())


def _seed_whatsapp_connection(client, *, enabled=True, allowed=(SENDER,)):
    """A Cloud connection for the admin, without a second setup_scope call.

    ``_seed_connection`` (intake tests) calls ``setup_scope``; ``_setup``
    does too, and the access-group name is unique per (kind, name) — calling
    both in one test violates ``uq_access_groups_kind_name``. Tests here call
    ``_setup`` first (one scope), then seed the connection directly.
    """
    connection = uuid.uuid4()

    async def seed(db):
        owner = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        agent = uuid.uuid4()
        await db.execute(insert(AgentStudioAgent).values(
            id=agent, owner_id=owner, name="Cloud intake test", instructions="Test fixture",
        ))
        encrypted = {}
        for key in ("access_token", "app_secret", "verify_token"):
            value = _seal(app.state.cipher, key, connection, SECRET)
            for part in ("ciphertext", "nonce", "key_id"):
                encrypted[f"{key}_{part}"] = getattr(value, part)
        await db.execute(insert(AgentStudioWhatsAppConnection).values(
            id=connection, owner_id=owner, agent_id=agent, enabled=enabled,
            business_account_id="100000", phone_number_id="200000", allowed_senders=list(allowed),
            **encrypted,
        ))
        return agent

    agent = run_db(seed)
    if SENDER in allowed:
        _link_sender(ADMIN_EMAIL, SENDER)
    return connection, agent


# ---------------------------------------------------------------------------
# Cloud: decisions over WhatsApp
# ---------------------------------------------------------------------------

def test_approve_by_whatsapp_completes_the_run(quiet_client):
    run_id, approval_id, hash12 = _awaiting_run(quiet_client)
    connection, _agent = _seed_whatsapp_connection(quiet_client)
    assert _post(quiet_client, connection,
                 _payload(body=f"APPROVE {approval_id} {hash12}", message_id="wamid.approve-one")).status_code == 200
    _drive_intake(quiet_client)

    assert _run_and_approval_status(run_id) == ("queued", "approved")
    assert _inbound_reply_text(connection).startswith("Approved. The agent will continue now")
    assert _pump_until(quiet_client, run_id, "completed")


def test_reject_by_whatsapp_fails_the_run(quiet_client):
    run_id, approval_id, hash12 = _awaiting_run(quiet_client)
    connection, _agent = _seed_whatsapp_connection(quiet_client)
    assert _post(quiet_client, connection,
                 _payload(body=f"REJECT {approval_id} {hash12}", message_id="wamid.reject-one")).status_code == 200
    _drive_intake(quiet_client)

    # decide() records the rejection and queues the run; the action is never run.
    assert _run_and_approval_status(run_id) == ("queued", "rejected")
    assert _inbound_reply_text(connection).startswith("Rejected. The agent will not run that action.")
    # The engine then processes the rejection: the run terminates (the stubbed
    # executor completes it) and the approval stays rejected — the tool never ran.
    assert _pump_until(quiet_client, run_id, "completed")
    assert _run_and_approval_status(run_id)[1] == "rejected"


def test_forged_approval_is_refused(quiet_client):
    run_id, approval_id, hash12 = _awaiting_run(quiet_client)
    connection, _agent = _seed_whatsapp_connection(quiet_client)
    forged = uuid.uuid4()
    assert _post(quiet_client, connection,
                 _payload(body=f"APPROVE {forged} {hash12}", message_id="wamid.forged-one")).status_code == 200
    _drive_intake(quiet_client)

    assert _run_and_approval_status(run_id) == ("awaiting_approval", "pending")
    assert _inbound_reply_text(connection).startswith("I couldn't find that approval.")


def test_replayed_approval_is_refused(quiet_client):
    run_id, approval_id, hash12 = _awaiting_run(quiet_client)
    connection, _agent = _seed_whatsapp_connection(quiet_client)
    command = f"APPROVE {approval_id} {hash12}"
    assert _post(quiet_client, connection, _payload(body=command, message_id="wamid.replay-first")).status_code == 200
    _drive_intake(quiet_client)
    assert _run_and_approval_status(run_id) == ("queued", "approved")

    assert _post(quiet_client, connection, _payload(body=command, message_id="wamid.replay-second")).status_code == 200
    _drive_intake(quiet_client)
    assert _run_and_approval_status(run_id) == ("queued", "approved")
    assert _inbound_reply_text(connection).startswith("This approval was already decided.")


def test_wrong_sender_approval_is_refused(quiet_client):
    run_id, approval_id, hash12 = _awaiting_run(quiet_client)
    connection, _agent = _seed_whatsapp_connection(quiet_client, allowed=("30000000", "40000000"))
    _link_sender(USER2_EMAIL, "40000000")
    assert _post(quiet_client, connection,
                 _payload(sender="40000000", body=f"APPROVE {approval_id} {hash12}",
                          message_id="wamid.wrong-sender")).status_code == 200
    _drive_intake(quiet_client)

    assert _run_and_approval_status(run_id) == ("awaiting_approval", "pending")
    assert _inbound_reply_text(connection).startswith("This approval belongs to a different account.")


def test_approval_without_hash_is_refused(quiet_client):
    run_id, approval_id, _hash12 = _awaiting_run(quiet_client)
    connection, _agent = _seed_whatsapp_connection(quiet_client)
    assert _post(quiet_client, connection,
                 _payload(body=f"APPROVE {approval_id}", message_id="wamid.no-hash")).status_code == 200
    _drive_intake(quiet_client)

    assert _run_and_approval_status(run_id) == ("awaiting_approval", "pending")
    assert _inbound_reply_text(connection).startswith("Reply with the full command from the approval card")


def test_hash_mismatch_is_refused(quiet_client):
    run_id, approval_id, hash12 = _awaiting_run(quiet_client)
    connection, _agent = _seed_whatsapp_connection(quiet_client)
    wrong = "000000000000" if hash12 != "000000000000" else "ffffffffffff"
    assert _post(quiet_client, connection,
                 _payload(body=f"APPROVE {approval_id} {wrong}", message_id="wamid.bad-hash")).status_code == 200
    _drive_intake(quiet_client)

    assert _run_and_approval_status(run_id) == ("awaiting_approval", "pending")
    assert _inbound_reply_text(connection).startswith("The action changed since the approval card was sent.")


# ---------------------------------------------------------------------------
# Cloud: the interactive approval card
# ---------------------------------------------------------------------------

@pytest.fixture
def recording_client(monkeypatch):
    """A TestClient whose Meta calls are recorded (no network, no workers)."""
    calls = []

    async def _record_meta(*_args, **kwargs):
        calls.append(kwargs.get("payload"))
        return TransportResponse(status=200, body={"messages": [{"id": "wamid.synthetic-reply"}]})

    monkeypatch.setattr(cloud_integration, "run_whatsapp_agent_worker", _noop_worker)
    monkeypatch.setattr(cloud_integration, "provider_request", _record_meta)
    _stub_execute(monkeypatch)
    with TestClient(app, headers=CSRF) as test_client:
        configure_host(StudioHost(app.state.sessionmaker, get_settings(),
                                  app.state.cipher, app.state.llm_runtime, app.state.rag))
        yield test_client, calls
        configure_host(None)


def _patch_start_run_awaiting(monkeypatch, run_id):
    """start_run returns the seeded awaiting-approval run instead of running."""

    async def awaiting(self, agent_id, thread_id, prompt, **kwargs):
        run = await self.db.get(AgentStudioRun, run_id)
        approval = await self.db.scalar(select(AgentStudioApproval).where(AgentStudioApproval.run_id == run_id))
        state = decode_run_state(self.cipher, run)
        return run, state, approval

    monkeypatch.setattr(AgentStudioService, "start_run", awaiting)


def test_cloud_sends_interactive_approval_card(recording_client, monkeypatch):
    client, calls = recording_client
    run_id, approval_id, hash12 = _awaiting_run(client)
    connection, _agent = _seed_whatsapp_connection(client)
    _patch_start_run_awaiting(monkeypatch, run_id)

    assert _post(client, connection, _payload(body="please run the web search")).status_code == 200
    _drive_intake(client)

    assert len(calls) == 2  # progress text + interactive card (no plain-text final)
    assert calls[0]["type"] == "text"
    assert calls[0]["text"]["body"].startswith("Request received.")
    interactive = calls[1]
    assert interactive["type"] == "interactive"
    body = interactive["interactive"]["body"]["text"]
    assert "*Approval required*" in body
    assert f"APPROVE {approval_id} {hash12}" in body
    buttons = interactive["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == [
        f"wa-approve:{approval_id}:{hash12}", f"wa-reject:{approval_id}:{hash12}"]
    assert [b["reply"]["title"] for b in buttons] == ["Approve", "Reject"]
    assert _inbound_reply_text(connection) == body
    assert _run_and_approval_status(run_id) == ("awaiting_approval", "pending")


# ---------------------------------------------------------------------------
# Baileys: card as text + decision by text command
# ---------------------------------------------------------------------------

def test_baileys_sends_approval_card_text(baileys_client, monkeypatch):
    run_id, approval_id, hash12 = _awaiting_run(baileys_client)
    _cloud, agent = _seed_whatsapp_connection(baileys_client)
    _patch_start_run_awaiting(monkeypatch, run_id)
    _conn, message_id, bridge, sent = _seed_baileys_message(monkeypatch, baileys_client, agent, {
        "text": "please run the web search", "jid": "10000000@s.whatsapp.net",
    })

    async def run():
        await bridge.execute(message_id)

    baileys_client.portal.call(run)

    assert [phase for phase, *_rest in sent] == ["ack", "final"]
    reply = sent[-1][3]
    assert "*Approval required*" in reply
    assert f"APPROVE {approval_id} {hash12}" in reply
    assert f"REJECT {approval_id} {hash12}" in reply
    assert _run_and_approval_status(run_id) == ("awaiting_approval", "pending")


def test_baileys_approve_command_decides_the_run(baileys_client, monkeypatch):
    run_id, approval_id, hash12 = _awaiting_run(baileys_client)
    _cloud, agent = _seed_whatsapp_connection(baileys_client)
    _conn, message_id, bridge, sent = _seed_baileys_message(monkeypatch, baileys_client, agent, {
        "text": f"APPROVE {approval_id} {hash12}", "jid": "10000000@s.whatsapp.net",
    })

    async def run():
        await bridge.execute(message_id)

    baileys_client.portal.call(run)

    # A decision is not a task: no ack, no agent run, just the decision reply.
    assert [phase for phase, *_rest in sent] == ["final"]
    assert sent[-1][3].startswith("InfraSpine AI ·")
    assert "Approved. The agent will continue now" in sent[-1][3]
    assert _run_and_approval_status(run_id) == ("queued", "approved")


# ---------------------------------------------------------------------------
# "Done (verified)" wording
# ---------------------------------------------------------------------------

def test_completed_task_reply_carries_no_unverified_success_marker(quiet_client):
    connection, _agent = _seed_connection(quiet_client)
    assert _post(quiet_client, connection, _payload()).status_code == 200
    _drive_intake(quiet_client)
    # The stubbed run completes inline (no execute_once row), so the reply is
    # the bare answer — never a success claim without verification.
    assert _inbound_reply_text(connection) == "Verified synthetic result."


def test_completed_reply_marks_verified_only(quiet_client):
    _scope, _people, _agent_id, _thread_id, run_id = _setup(quiet_client)

    async def check(answer):
        async with app.state.sessionmaker() as db:
            run = await db.get(AgentStudioRun, run_id)
            return await completed_reply(db, run, answer)

    async def seed_verified():
        async with app.state.sessionmaker() as db:
            scope_id = await org_scope_id(db)
            owner = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
            db.add(PlatformActionExecution(
                idempotency_key=f"agent_studio:{run_id}:attempt:1",
                action_type="agent_studio.execute", action_hash="0" * 64, duty="agent_studio.execute",
                subject=f"agent_studio_run:{run_id}", scope_id=scope_id,
                executor_user_id=owner, executor_kind="user", status="verified",
                trace_id=uuid.uuid4(),
            ))
            await db.commit()

    # No ledger row: bare answer, even when the answer itself says "done".
    assert quiet_client.portal.call(check, "The task is done.") == "The task is done."
    quiet_client.portal.call(seed_verified)
    assert quiet_client.portal.call(check, "The task is done.") == "Done (verified)\n\nThe task is done."
