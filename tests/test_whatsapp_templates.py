"""Step F slice 6: 24-hour window templates + hardening.

Proved here, on a throwaway ``*_test`` database only, single process:

* templates: the catalogue validates (unknown template, bad name or
  language, wrong language for a template, wrong parameter count, empty
  or overlong parameters) and renders; the summary parameter truncates;
* the Cloud worker re-opens an expired 24-hour window with the
  ``task_ready`` template instead of failing the inbound — before the
  run (invitation parameter, no agent run) and after the run (the helper
  sends the reply summary; the live second check calls the same helper,
  which time travel cannot reach in a test, so it is covered directly);
* template sends go through ``dispatch_once``: a replay reuses the
  stored provider id and calls Meta once;
* a template send that fails over to Baileys carries the rendered text
  on the same idempotency key (adapter level);
* replay window: a message timestamped more than 7 days ago is failed
  at intake (``stale_message``, no dispatch row, no run); a future-dated
  message is treated as arrived now and completes normally;
* secret rotation: rotating the app secret makes the old secret fail the
  webhook HMAC and the new one verify; rotating the verify token makes
  the old token fail the subscription challenge and the new one answer
  it; bad names and empty values are refused;
* audit completeness: a completed run is complete, a template re-open
  is complete, a queued inbound is pending, and a terminal inbound with
  no terminal audit event is incomplete with the missing event named;
* spoken approvals: a voice note saying ``APPROVE …`` decides the run
  on Cloud and on Baileys — no agent run is started for the decision.

Nothing here calls the real Meta API: the provider transport is stubbed,
and the agent execution is stubbed to a verified synthetic completion
(same pattern as ``tests/test_agent_studio_unified.py``).
"""

import asyncio
import base64
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from app.core.errors import NotFoundError
from app.db.agent_studio_models import AgentStudioRun
from app.db.agent_studio_whatsapp_models import AgentStudioWhatsAppConnection, AgentStudioWhatsAppInbound
from app.db.models import AuditLog, User
from app.integrations import whatsapp as cloud_integration
from app.integrations import whatsapp_templates, whatsapp_voice
from app.integrations.models import WhatsAppDispatch
from app.integrations.whatsapp_audit import check_audit_completeness, expected_event_groups
from app.integrations.whatsapp_failover import PathUnavailable, send_with_failover
from app.main import app
from app.services.agent_studio_whatsapp import _seal
from tests.conftest import ADMIN_EMAIL, login, run_db
from tests.test_whatsapp_approvals import (
    _awaiting_run,
    _run_and_approval_status,
    _seed_whatsapp_connection,
    recording_client,  # noqa: F401  (fixture)
)
from tests.test_whatsapp_unified_intake import (
    SECRET,
    SENDER,
    _drive_intake,
    _post,
    _seed_connection,
    quiet_client,  # noqa: F401  (fixture)
)
from tests.test_whatsapp_voice import (
    _audio_payload,
    _record_start_run,
    _seed_baileys_message,
    baileys_client,  # noqa: F401  (fixture)
    voice_client,  # noqa: F401  (fixture)
)


# ---------------------------------------------------------------------------
# Pure: template catalogue, summary, prefix strip, audit expectations
# ---------------------------------------------------------------------------

def test_template_catalogue_validates_and_renders():
    assert whatsapp_templates.validate_template("task_ready", "en_US", ["hello"]) == "task_ready"
    assert whatsapp_templates.render_template_text("task_ready", ["Reply here."]) == (
        "Your InfraSpine AI task is ready. Reply here."
    )


def test_template_validation_rejects_bad_inputs():
    with pytest.raises(ValueError):
        whatsapp_templates.validate_template("no_such_template", "en_US", ["x"])
    with pytest.raises(ValueError):
        whatsapp_templates.validate_template("Task_Ready", "en_US", ["x"])
    with pytest.raises(ValueError):
        whatsapp_templates.validate_template("task_ready", "xx", ["x"])
    with pytest.raises(ValueError):
        whatsapp_templates.validate_template("task_ready", "en_GB", ["x"])
    with pytest.raises(ValueError):
        whatsapp_templates.validate_template("task_ready", "en_US", [])
    with pytest.raises(ValueError):
        whatsapp_templates.validate_template("task_ready", "en_US", ["a", "b"])
    with pytest.raises(ValueError):
        whatsapp_templates.validate_template("task_ready", "en_US", [""])
    with pytest.raises(ValueError):
        whatsapp_templates.validate_template("task_ready", "en_US", ["x" * 1025])
    with pytest.raises(ValueError):
        whatsapp_templates.validate_template("task_ready", "en_US", [42])


def test_template_summary_truncates_and_defaults():
    assert whatsapp_templates.template_summary("  The task\nis   done.  ") == "The task is done."
    assert whatsapp_templates.template_summary("x" * 500) == "x" * 200
    assert whatsapp_templates.template_summary("") == whatsapp_templates.TEMPLATE_INVITATION
    assert whatsapp_templates.template_summary(None) == whatsapp_templates.TEMPLATE_INVITATION


def test_strip_machine_text_prefix():
    marked = whatsapp_voice.VOICE_TRANSCRIPT_PREFIX + "APPROVE 123"
    assert whatsapp_voice.strip_machine_text_prefix(marked) == "APPROVE 123"
    assert whatsapp_voice.strip_machine_text_prefix("plain text") == "plain text"
    assert whatsapp_voice.strip_machine_text_prefix("") == ""


def test_audit_expected_event_groups():
    assert expected_event_groups("completed") == [
        ["integration.whatsapp_accepted", "integration.whatsapp_template_reopen"]
    ]
    assert expected_event_groups("failed") == [
        ["integration.whatsapp_failed", "integration.whatsapp_stale_message"]
    ]
    assert expected_event_groups("queued") == []
    assert expected_event_groups("processing") == []


# ---------------------------------------------------------------------------
# Shared helpers for the slice-6 worker tests
# ---------------------------------------------------------------------------

def _payload_at(*, timestamp, message_id="wamid.t6", sender=SENDER, body="hello, please check the laptop status"):
    data = json.dumps({"entry": [{"id": "100000", "changes": [{"value": {
        "metadata": {"phone_number_id": "200000"},
        "messages": [{"id": message_id, "from": sender, "type": "text",
                      "text": {"body": body}, "timestamp": str(timestamp)}],
    }}]}]}).encode()
    return data


def _inbound_state(connection):
    async def load(db):
        return (await db.execute(select(
            AgentStudioWhatsAppInbound.id, AgentStudioWhatsAppInbound.status,
            AgentStudioWhatsAppInbound.last_error,
        ).where(AgentStudioWhatsAppInbound.connection_id == connection))).one()

    return run_db(load)


def _dispatch_state(inbound_id):
    async def load(db):
        # run_db hands us a Core connection: select explicit columns —
        # scalars(select(Entity)) on a Core connection yields only the
        # first column (the id), not the row.
        row = (await db.execute(select(
            WhatsAppDispatch.phase, WhatsAppDispatch.provider_message_id,
            WhatsAppDispatch.incoming_at,
        ).where(WhatsAppDispatch.inbound_id == inbound_id))).one_or_none()
        return None if row is None else (row[0], row[1], row[2])

    return run_db(load)


def _audit_actions(target):
    async def load(db):
        rows = await db.scalars(select(AuditLog.action).where(AuditLog.target == str(target)))
        return sorted(rows)

    return run_db(load)


def _seed_inbound_with_dispatch(connection, *, incoming_at=None, phase="waiting", status="queued"):
    """An inbound + dispatch row, bypassing the webhook (for direct helper tests)."""
    inbound_id = uuid.uuid4()

    async def seed(db):
        owner = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        sealed = _seal(app.state.cipher, "inbound", inbound_id, "hello")
        await db.execute(insert(AgentStudioWhatsAppInbound).values(
            id=inbound_id, connection_id=connection, owner_id=owner,
            external_message_id="wamid.seed-" + inbound_id.hex[:8], from_wa_id=SENDER,
            message_type="text", text_ciphertext=sealed.ciphertext, text_nonce=sealed.nonce,
            text_key_id=sealed.key_id, status=status, attempts=0,
            next_attempt_at=datetime.now(UTC),
        ))
        await db.execute(insert(WhatsAppDispatch).values(
            inbound_id=inbound_id, phase=phase,
            incoming_at=incoming_at or datetime.now(UTC),
        ))
        return owner

    owner = run_db(seed)
    return inbound_id, owner


# ---------------------------------------------------------------------------
# Templates through the Cloud worker
# ---------------------------------------------------------------------------

def test_window_expired_before_run_sends_template(recording_client, monkeypatch):
    client, calls = recording_client
    recorded = _record_start_run(monkeypatch)
    connection, _agent = _seed_connection(client)
    expired_at = int(time.time()) - 25 * 3600
    assert _post(client, connection, _payload_at(timestamp=expired_at, message_id="wamid.expired-one")).status_code == 200
    _drive_intake(client)

    assert recorded == []  # the task is not run; the template invites the user back
    inbound_id, status, error = _inbound_state(connection)
    assert status == "completed" and error is None
    assert len(calls) == 1  # no progress message, just the template
    template = calls[0]
    assert template["type"] == "template"
    assert template["template"]["name"] == "task_ready"
    assert template["template"]["language"] == {"code": "en_US"}
    assert template["template"]["components"][0]["parameters"][0] == {
        "type": "text", "text": whatsapp_templates.TEMPLATE_INVITATION,
    }
    assert "integration.whatsapp_template_reopen" in _audit_actions(inbound_id)


def test_template_reopen_helper_sends_summary_after_run(recording_client):
    # The live second window check (after the agent ran) calls the same
    # helper; the clock cannot be advanced between the two checks in a
    # test, so the post-run path is covered here directly.
    client, calls = recording_client
    connection, _agent = _seed_connection(client)
    inbound_id, owner = _seed_inbound_with_dispatch(
        connection, incoming_at=datetime.now(UTC) - timedelta(hours=25),
        phase="final_prepared", status="processing",
    )

    async def reopen():
        async with app.state.sessionmaker() as db:
            inbound = await db.get(AgentStudioWhatsAppInbound, inbound_id)
            dispatch = await db.get(WhatsAppDispatch, inbound_id)
            conn = await db.get(AgentStudioWhatsAppConnection, connection)
            user = await db.get(User, owner)
            await cloud_integration._reopen_expired_window(
                db, user, conn, inbound, dispatch, app.state.cipher, "The task is done.")

    client.portal.call(reopen)

    assert len(calls) == 1
    assert calls[0]["template"]["components"][0]["parameters"][0]["text"] == "The task is done."
    phase, provider_id, _incoming = _dispatch_state(inbound_id)
    assert phase == "accepted" and provider_id == "wamid.synthetic-reply"
    assert _inbound_state(connection)[1] == "completed"
    assert "integration.whatsapp_template_reopen" in _audit_actions(inbound_id)


def test_template_send_is_idempotent_under_dispatch_once(recording_client):
    client, calls = recording_client
    connection, _agent = _seed_connection(client)
    inbound_id, owner = _seed_inbound_with_dispatch(connection)

    async def send_twice():
        async with app.state.sessionmaker() as db:
            inbound = await db.get(AgentStudioWhatsAppInbound, inbound_id)
            conn = await db.get(AgentStudioWhatsAppConnection, connection)
            user = await db.get(User, owner)
            key = f"whatsapp:{inbound_id}:template:task_ready"
            first = await cloud_integration.send_template_once(
                db, user, conn, inbound, app.state.cipher,
                "task_ready", "en_US", ["hello"], key=key)
            second = await cloud_integration.send_template_once(
                db, user, conn, inbound, app.state.cipher,
                "task_ready", "en_US", ["hello"], key=key)
            return first, second

    first, second = client.portal.call(send_twice)
    assert first == second == "wamid.synthetic-reply"
    assert len(calls) == 1  # the replay reused the stored provider id
    assert calls[0]["type"] == "template"
    assert calls[0]["template"]["name"] == "task_ready"


class _StubTransport:
    """A transport that returns a ref or raises, recording every call (incl. template)."""

    def __init__(self, name, behavior):
        self.name = name
        self.behavior = behavior
        self.calls = []

    async def send(self, *, to, text, key, interactive=None, template=None):
        self.calls.append((to, text, key, template))
        if isinstance(self.behavior, Exception):
            raise self.behavior
        return self.behavior


def test_template_failover_to_baileys_sends_rendered_text():
    cloud = _StubTransport("cloud", PathUnavailable("cloud", "provider_unavailable", retryable=True))
    baileys = _StubTransport("baileys", "bridge-1")
    template = ("task_ready", "en_US", ["Reply here to continue."])

    ref, used = asyncio.run(send_with_failover(
        setting="auto", own_channel="cloud",
        to={"cloud": "30000000", "baileys": "30000000@s.whatsapp.net"},
        text=whatsapp_templates.render_template_text("task_ready", ["Reply here to continue."]),
        key="whatsapp:1:template:task_ready", cloud=cloud, baileys=baileys, template=template,
    ))
    assert (ref, used) == ("bridge-1", "baileys")
    assert len(cloud.calls) == 1 and len(baileys.calls) == 1
    # Same idempotency key on both legs; the Baileys leg got the rendered text.
    assert cloud.calls[0][2] == baileys.calls[0][2] == "whatsapp:1:template:task_ready"
    assert baileys.calls[0][1] == "Your InfraSpine AI task is ready. Reply here to continue."
    assert baileys.calls[0][3] == template


# ---------------------------------------------------------------------------
# Replay window: stale and future-dated messages
# ---------------------------------------------------------------------------

def test_stale_message_is_rejected_at_intake(quiet_client):
    connection, _agent = _seed_connection(quiet_client)
    stale_at = int(time.time()) - 8 * 86400
    assert _post(quiet_client, connection,
                 _payload_at(timestamp=stale_at, message_id="wamid.stale-one")).status_code == 200

    inbound_id, status, error = _inbound_state(connection)
    assert status == "failed" and error == "stale_message"
    assert _dispatch_state(inbound_id) is None  # never queued for the worker
    assert "integration.whatsapp_stale_message" in _audit_actions(inbound_id)
    assert run_db(lambda db: db.scalar(select(AgentStudioRun.id))) is None


def test_future_timestamp_uses_arrival_time(quiet_client):
    connection, _agent = _seed_connection(quiet_client)
    future_at = int(time.time()) + 3600
    assert _post(quiet_client, connection,
                 _payload_at(timestamp=future_at, message_id="wamid.future-one")).status_code == 200
    _drive_intake(quiet_client)

    inbound_id, status, error = _inbound_state(connection)
    assert status == "completed" and error is None
    # The window ran from arrival, so the agent ran and the reply went out.
    _phase, provider_id, incoming_at = _dispatch_state(inbound_id)
    assert provider_id == "wamid.synthetic-reply"
    assert incoming_at is not None and incoming_at <= datetime.now(UTC)


# ---------------------------------------------------------------------------
# Secret rotation
# ---------------------------------------------------------------------------

def test_rotate_app_secret_old_stops_new_works(quiet_client):
    connection, _agent = _seed_connection(quiet_client)
    login(quiet_client, ADMIN_EMAIL)
    response = quiet_client.post(
        "/api/agent-studio/whatsapp/rotate-secret",
        json={"secret": "app_secret", "new_value": "rotated-secret-1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["connection"]["app_secret_configured"] is True

    assert _post(quiet_client, connection, _payload_at(timestamp=int(time.time()),
                 message_id="wamid.old-secret"), secret=SECRET).status_code == 422
    assert _post(quiet_client, connection, _payload_at(timestamp=int(time.time()),
                 message_id="wamid.new-secret"), secret="rotated-secret-1").status_code == 200


def test_rotate_verify_token_old_stops_new_works(quiet_client):
    connection, _agent = _seed_connection(quiet_client)
    login(quiet_client, ADMIN_EMAIL)
    url = f"/api/agent-studio/whatsapp/webhook/{connection}"
    good = {"hub.mode": "subscribe", "hub.challenge": "challenge-123"}
    assert quiet_client.get(url, params={**good, "hub.verify_token": SECRET}).text == "challenge-123"

    response = quiet_client.post(
        "/api/agent-studio/whatsapp/rotate-secret",
        json={"secret": "verify_token", "new_value": "rotated-verify-1"},
    )
    assert response.status_code == 200, response.text

    assert quiet_client.get(url, params={**good, "hub.verify_token": SECRET}).status_code == 404
    assert quiet_client.get(url, params={**good, "hub.verify_token": "rotated-verify-1"}).text == "challenge-123"


def test_rotate_secret_validates_input(quiet_client):
    _seed_connection(quiet_client)
    login(quiet_client, ADMIN_EMAIL)
    url = "/api/agent-studio/whatsapp/rotate-secret"
    assert quiet_client.post(url, json={"secret": "wrong_name", "new_value": "x"}).status_code == 422
    assert quiet_client.post(url, json={"secret": "app_secret", "new_value": ""}).status_code == 422
    assert quiet_client.post(url, json={"secret": "app_secret", "new_value": "   "}).status_code == 422


# ---------------------------------------------------------------------------
# Audit completeness
# ---------------------------------------------------------------------------

def test_audit_complete_for_accepted_run(quiet_client):
    connection, _agent = _seed_connection(quiet_client)
    assert _post(quiet_client, connection, _payload_at(timestamp=int(time.time()),
                 message_id="wamid.audit-one")).status_code == 200
    _drive_intake(quiet_client)

    inbound_id, status, _error = _inbound_state(connection)
    assert status == "completed"

    async def check():
        async with app.state.sessionmaker() as db:
            return await check_audit_completeness(db, inbound_id)

    verdict = quiet_client.portal.call(check)
    assert verdict["status"] == "complete"
    assert "integration.whatsapp_accepted" in verdict["present"]
    assert verdict["missing"] == []


def test_audit_complete_for_template_reopen(recording_client):
    client, _calls = recording_client
    connection, _agent = _seed_connection(client)
    expired_at = int(time.time()) - 25 * 3600
    assert _post(client, connection, _payload_at(timestamp=expired_at, message_id="wamid.audit-two")).status_code == 200
    _drive_intake(client)

    inbound_id, status, _error = _inbound_state(connection)
    assert status == "completed"

    async def check():
        async with app.state.sessionmaker() as db:
            return await check_audit_completeness(db, inbound_id)

    verdict = client.portal.call(check)
    assert verdict["status"] == "complete"
    assert "integration.whatsapp_template_reopen" in verdict["present"]
    assert verdict["missing"] == []


def test_audit_incomplete_when_terminal_event_missing(quiet_client):
    connection, _agent = _seed_connection(quiet_client)
    inbound_id, _owner = _seed_inbound_with_dispatch(connection, status="completed")

    async def check():
        async with app.state.sessionmaker() as db:
            return await check_audit_completeness(db, inbound_id)

    verdict = quiet_client.portal.call(check)
    assert verdict["status"] == "incomplete"
    assert verdict["missing"] == ["integration.whatsapp_accepted"]
    assert verdict["present"] == []


def test_audit_pending_while_queued_and_failed_without_event(quiet_client):
    connection, _agent = _seed_connection(quiet_client)
    queued_id, _owner = _seed_inbound_with_dispatch(connection, status="queued")
    failed_id, _owner = _seed_inbound_with_dispatch(connection, status="failed")

    async def check(inbound_id):
        async with app.state.sessionmaker() as db:
            return await check_audit_completeness(db, inbound_id)

    assert quiet_client.portal.call(check, queued_id)["status"] == "pending"
    failed = quiet_client.portal.call(check, failed_id)
    assert failed["status"] == "incomplete"
    assert failed["missing"] == ["integration.whatsapp_failed"]


def test_audit_unknown_inbound_raises(quiet_client):
    _seed_connection(quiet_client)

    async def check():
        async with app.state.sessionmaker() as db:
            return await check_audit_completeness(db, uuid.uuid4())

    with pytest.raises(NotFoundError):
        quiet_client.portal.call(check)


# ---------------------------------------------------------------------------
# Spoken approvals: a voice note saying APPROVE decides the run
# ---------------------------------------------------------------------------

def test_cloud_spoken_approval_decides_the_run(voice_client, monkeypatch):
    run_id, approval_id, hash12 = _awaiting_run(voice_client)
    connection, _agent = _seed_whatsapp_connection(voice_client)
    recorded = _record_start_run(monkeypatch)
    monkeypatch.setattr(
        whatsapp_voice, "transcribe_spoken_question",
        lambda _path: f"APPROVE {approval_id} {hash12}",
    )
    assert _post(voice_client, connection, _audio_payload(message_id="wamid.voice-approve")).status_code == 200
    _drive_intake(voice_client)

    assert recorded == []  # a decision is not a task: no agent run
    assert _run_and_approval_status(run_id) == ("queued", "approved")
    inbound_id, status, _error = _inbound_state(connection)
    assert status == "completed"


def test_baileys_spoken_approval_decides_the_run(baileys_client, monkeypatch):
    run_id, approval_id, hash12 = _awaiting_run(baileys_client)
    _cloud, agent = _seed_whatsapp_connection(baileys_client)
    monkeypatch.setattr(
        whatsapp_voice, "transcribe_spoken_question",
        lambda _path: f"APPROVE {approval_id} {hash12}",
    )
    encoded = base64.b64encode(b"fake ogg bytes").decode()
    _conn, message_id, bridge, sent = _seed_baileys_message(monkeypatch, baileys_client, agent, {
        "text": "", "jid": "10000000@s.whatsapp.net",
        "audio": encoded, "mime": "audio/ogg; codecs=opus", "audio_too_large": False,
    })

    async def run():
        await bridge.execute(message_id)

    baileys_client.portal.call(run)

    # A decision is not a task: no ack, just the decision reply.
    assert [phase for phase, *_rest in sent] == ["final"]
    assert "Approved. The agent will continue now" in sent[-1][3]
    assert _run_and_approval_status(run_id) == ("queued", "approved")
