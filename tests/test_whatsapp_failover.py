"""Step F slice 3: WhatsApp runtime path setting + automatic failover (Cloud <-> Baileys).

The panel toggle used to be UI-only. These tests lock in the runtime
contract (plan ``docs/step-f-whatsapp-plan.md`` §5), on a throwaway
``*_test`` database only, with the Meta transport stubbed — no network:

* adapter: the primary is resolved from the owner's setting (explicit wins,
  ``auto`` prefers the arrival channel, an unavailable preferred path is
  skipped, never retried); a primary that fails with an error that
  confirmed nothing is retried exactly once on the other path, under the
  same idempotency key, and the attempt is audited; when both fail the
  *primary's* error is raised; a send that may have been accepted is never
  retried on the other path (``provider_rejected`` is not wrapped);
* setting: per-owner ``cloud`` / ``baileys`` / ``auto`` (default ``auto``),
  persisted, audited only on change, unknown values rejected, and the
  ``PUT /whatsapp/path`` endpoint is owner-scoped;
* Cloud worker: a Meta 5xx fails over to a live Baileys session once per
  phase (progress + final), with one failover audit row each and exactly
  two ``whatsapp:`` ledger rows — no duplicate dispatch; the path setting
  is respected end to end; when both paths are down the dispatch is
  ``uncertain`` and nothing is sent twice;
* Baileys worker: a dead bridge fails over to Cloud once, and the message
  still completes.
"""

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agent_studio.unified import StudioHost, configure_host
from app.core.config import Settings, get_settings
from app.db.agent_studio_baileys_models import BaileysConnection, BaileysMessage
from app.db.agent_studio_whatsapp_models import AgentStudioWhatsAppInbound
from app.db.models import AuditLog, User
from app.db.session import test_engine_options as _test_engine_options
from app.integrations import whatsapp as cloud_integration
from app.integrations.models import WhatsAppDispatch
from app.integrations.transport import Response as TransportResponse, TransportError
from app.integrations.whatsapp_failover import (
    BaileysTransport,
    CloudTransport,
    PathUnavailable,
    get_path_setting,
    jid_to_phone,
    phone_to_jid,
    resolve_primary,
    send_with_failover,
    set_path_setting,
)
from app.main import app
from app.platform.models import PlatformActionExecution
from app.services import agent_studio_baileys
from app.services.agent_studio_baileys import BridgeSession, seal
from tests.conftest import ADMIN_EMAIL, CSRF, TEST_DATABASE_URL, USER_EMAIL, login, run_db
from tests.test_agent_studio_unified import _stub_execute
from tests.test_whatsapp_unified_intake import _payload, _post, _seed_connection

SENDER = "30000000"


# ---------------------------------------------------------------------------
# Adapter unit tests (no database)
# ---------------------------------------------------------------------------

class _StubTransport:
    """A transport that returns a ref or raises, recording every call."""

    def __init__(self, name, behavior):
        self.name = name
        self.behavior = behavior
        self.calls = []

    async def send(self, *, to, text, key, interactive=None, template=None):
        # Mirror the real transport interface: slice 5 threads
        # ``interactive`` through send_with_failover and slice 6 threads
        # ``template``; the stub must accept both or every failover test
        # TypeErrors on the call.
        self.calls.append((to, text, key, interactive, template))
        if isinstance(self.behavior, Exception):
            raise self.behavior
        return self.behavior


def test_resolve_primary_matrix():
    assert resolve_primary("auto", own_channel="cloud", cloud_available=True, baileys_available=True) == "cloud"
    assert resolve_primary("auto", own_channel="baileys", cloud_available=True, baileys_available=True) == "baileys"
    assert resolve_primary("cloud", own_channel="baileys", cloud_available=True, baileys_available=True) == "cloud"
    assert resolve_primary("baileys", own_channel="cloud", cloud_available=True, baileys_available=True) == "baileys"
    # An unavailable preferred path is skipped, never retried.
    assert resolve_primary("cloud", own_channel="cloud", cloud_available=False, baileys_available=True) == "baileys"
    assert resolve_primary("baileys", own_channel="baileys", cloud_available=True, baileys_available=False) == "cloud"
    assert resolve_primary("auto", own_channel="cloud", cloud_available=False, baileys_available=False) is None
    with pytest.raises(ValueError):
        resolve_primary("smoke-signal", own_channel="cloud", cloud_available=True, baileys_available=True)


def test_jid_phone_helpers():
    assert phone_to_jid("30000000") == "30000000@s.whatsapp.net"
    assert jid_to_phone("30000000@s.whatsapp.net") == "30000000"
    # @lid identities have no phone number: Cloud cannot reach them.
    assert jid_to_phone("123456789@lid") is None
    assert jid_to_phone("30000000@s.whatsapp.net") is not None


def test_send_with_failover_primary_ok_never_touches_fallback():
    cloud = _StubTransport("cloud", "wamid.1")
    baileys = _StubTransport("baileys", "bridge-1")

    ref, used = asyncio.run(send_with_failover(
        setting="auto", own_channel="cloud",
        to={"cloud": "30000000", "baileys": "30000000@s.whatsapp.net"},
        text="hello", key="whatsapp:1:final", cloud=cloud, baileys=baileys,
    ))
    assert (ref, used) == ("wamid.1", "cloud")
    assert baileys.calls == []


def test_send_with_failover_cloud_500_falls_back_to_baileys_once():
    cloud = _StubTransport("cloud", PathUnavailable("cloud", "provider_unavailable", retryable=True))
    baileys = _StubTransport("baileys", "bridge-1")
    events = []

    async def on_failover(from_path, to_path, reason, key, *, outcome, fallback_code=None):
        events.append((from_path, to_path, reason, key, outcome, fallback_code))

    ref, used = asyncio.run(send_with_failover(
        setting="auto", own_channel="cloud",
        to={"cloud": "30000000", "baileys": "30000000@s.whatsapp.net"},
        text="hello", key="whatsapp:1:final", cloud=cloud, baileys=baileys, on_failover=on_failover,
    ))
    assert (ref, used) == ("bridge-1", "baileys")
    assert len(cloud.calls) == 1 and len(baileys.calls) == 1
    # Same idempotency key on both legs: the send can never double-deliver.
    assert cloud.calls[0][2] == baileys.calls[0][2] == "whatsapp:1:final"
    assert events == [("cloud", "baileys", "provider_unavailable", "whatsapp:1:final", "success", None)]


def test_send_with_failover_both_fail_raises_primary_error():
    cloud = _StubTransport("cloud", PathUnavailable("cloud", "provider_unavailable", retryable=True))
    baileys = _StubTransport("baileys", PathUnavailable("baileys", "delivery_failed", ambiguous=True))
    events = []

    async def on_failover(from_path, to_path, reason, key, *, outcome, fallback_code=None):
        events.append((from_path, to_path, reason, key, outcome, fallback_code))

    with pytest.raises(PathUnavailable) as excinfo:
        asyncio.run(send_with_failover(
            setting="auto", own_channel="cloud",
            to={"cloud": "30000000", "baileys": "30000000@s.whatsapp.net"},
            text="hello", key="whatsapp:1:final", cloud=cloud, baileys=baileys, on_failover=on_failover,
        ))
    # The caller's existing handling sees the primary's error, not the fallback's.
    assert excinfo.value.code == "provider_unavailable"
    assert events == [("cloud", "baileys", "provider_unavailable", "whatsapp:1:final", "failure", "delivery_failed")]


def test_send_with_failover_no_paths_raises_path_unavailable():
    with pytest.raises(PathUnavailable) as excinfo:
        asyncio.run(send_with_failover(
            setting="auto", own_channel="cloud",
            to={"cloud": "30000000", "baileys": "30000000@s.whatsapp.net"},
            text="hello", key="whatsapp:1:final", cloud=None, baileys=None,
        ))
    assert excinfo.value.code == "path_unavailable"


def test_cloud_transport_only_wraps_errors_that_confirmed_nothing(monkeypatch):
    async def _rejected(*_args, **_kwargs):
        raise TransportError("provider_rejected")

    async def _unavailable(*_args, **_kwargs):
        raise TransportError("provider_unavailable", retryable=True, ambiguous=False)

    transport = CloudTransport(SimpleNamespace(), SimpleNamespace(from_wa_id="30000000"), SimpleNamespace())
    monkeypatch.setattr(cloud_integration, "send_text", _rejected)
    with pytest.raises(TransportError) as excinfo:
        asyncio.run(transport.send(to="30000000", text="hello", key="k"))
    # A rejected request is not failover-eligible: the raw error propagates,
    # so the adapter never retries a send that may have been accepted.
    assert type(excinfo.value) is TransportError
    assert excinfo.value.code == "provider_rejected"
    monkeypatch.setattr(cloud_integration, "send_text", _unavailable)
    with pytest.raises(PathUnavailable) as excinfo:
        asyncio.run(transport.send(to="30000000", text="hello", key="k"))
    assert excinfo.value.path == "cloud"
    assert excinfo.value.code == "provider_unavailable"


def test_baileys_transport_bridge_states():
    class _Session:
        def __init__(self, behavior):
            self.behavior = behavior
            self.calls = []

        async def send_reply(self, delivery_id, jid, reply, *, timeout=30):
            self.calls.append((delivery_id, jid, reply))
            if isinstance(self.behavior, Exception):
                raise self.behavior
            return self.behavior

    ok = _Session(True)
    assert asyncio.run(BaileysTransport(ok, delivery_id="ABC").send(
        to="30000000@s.whatsapp.net", text="hi", key="k")) == "ABC"
    down = _Session(RuntimeError("bridge down"))
    with pytest.raises(PathUnavailable) as excinfo:
        asyncio.run(BaileysTransport(down).send(to="30000000@s.whatsapp.net", text="hi", key="k"))
    assert excinfo.value.code == "bridge_unavailable"
    assert excinfo.value.ambiguous is False
    timeout = _Session(False)
    with pytest.raises(PathUnavailable) as excinfo:
        asyncio.run(BaileysTransport(timeout).send(to="30000000@s.whatsapp.net", text="hi", key="k"))
    # A bridge timeout may still deliver late: ambiguous, never retried blindly.
    assert excinfo.value.code == "delivery_failed"
    assert excinfo.value.ambiguous is True
    # The derived delivery id is deterministic per key: a replay reuses it.
    first = asyncio.run(BaileysTransport(_Session(True)).send(
        to="30000000@s.whatsapp.net", text="hi", key="whatsapp:1:final"))
    second = asyncio.run(BaileysTransport(_Session(True)).send(
        to="30000000@s.whatsapp.net", text="hi", key="whatsapp:1:final"))
    assert first == second


# ---------------------------------------------------------------------------
# Path setting: storage + audit
# ---------------------------------------------------------------------------

def _audit_count(action):
    async def count(db):
        return await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == action))

    return run_db(count)


def _owner_id(email):
    return run_db(lambda db: db.scalar(select(User.id).where(User.email == email)))


def _run_session(fn):
    """Run ``await fn(session)`` with a real AsyncSession, then commit.

    ``run_db`` hands the function a bare Connection, which has no ``get``
    or ``add`` — the path-setting helpers need a Session, exactly as the
    routes and workers call them with in production.
    """

    async def _run():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                result = await fn(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_path_setting_defaults_to_auto():
    owner = _owner_id(ADMIN_EMAIL)
    assert _run_session(lambda db: get_path_setting(db, owner)) == "auto"


def test_path_setting_persists_and_audits_only_on_change():
    owner = _owner_id(ADMIN_EMAIL)

    async def set_path(db, path):
        return await set_path_setting(db, owner, path)

    assert _run_session(lambda db: set_path(db, "cloud")) == "cloud"
    assert _run_session(lambda db: get_path_setting(db, owner)) == "cloud"
    assert _audit_count("integration.whatsapp_path_changed") == 1
    # Setting the same value again is a no-op: no second audit row.
    _run_session(lambda db: set_path(db, "cloud"))
    assert _audit_count("integration.whatsapp_path_changed") == 1
    _run_session(lambda db: set_path(db, "baileys"))
    assert _run_session(lambda db: get_path_setting(db, owner)) == "baileys"
    assert _audit_count("integration.whatsapp_path_changed") == 2


def test_path_setting_rejects_unknown_value():
    owner = _owner_id(ADMIN_EMAIL)
    with pytest.raises(ValueError):
        _run_session(lambda db: set_path_setting(db, owner, "smoke-signal"))
    assert _run_session(lambda db: get_path_setting(db, owner)) == "auto"


# ---------------------------------------------------------------------------
# API: GET /whatsapp path fields + PUT /whatsapp/path
# ---------------------------------------------------------------------------

def test_get_whatsapp_reports_path_fields(client):
    connection, _agent = _seed_connection(client)
    login(client, ADMIN_EMAIL)
    response = client.get("/api/agent-studio/whatsapp")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["connection"] is not None
    assert body["path_setting"] == "auto"
    # Enabled Cloud connection, no Baileys session: Cloud is the active path.
    assert body["active_path"] == "cloud"
    assert body["cloud_available"] is True
    assert body["baileys_available"] is False


def test_put_whatsapp_path_persists_and_skips_unavailable_primary(client):
    _seed_connection(client)
    login(client, ADMIN_EMAIL)
    response = client.put("/api/agent-studio/whatsapp/path", json={"path": "baileys"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["path_setting"] == "baileys"
    # Baileys was requested but nothing is connected there: Cloud stays active.
    assert body["active_path"] == "cloud"
    assert client.get("/api/agent-studio/whatsapp").json()["path_setting"] == "baileys"
    admin = _owner_id(ADMIN_EMAIL)
    assert _run_session(lambda db: get_path_setting(db, admin)) == "baileys"


def test_put_whatsapp_path_rejects_unknown_value(client):
    _seed_connection(client)
    login(client, ADMIN_EMAIL)
    assert client.put("/api/agent-studio/whatsapp/path", json={"path": "smoke-signal"}).status_code == 422
    admin = _owner_id(ADMIN_EMAIL)
    assert _run_session(lambda db: get_path_setting(db, admin)) == "auto"


def test_put_whatsapp_path_is_owner_scoped(client):
    _seed_connection(client)
    login(client, USER_EMAIL)
    assert client.put("/api/agent-studio/whatsapp/path", json={"path": "cloud"}).status_code == 200
    # The other user set their own setting; the admin's is untouched.
    user, admin = _owner_id(USER_EMAIL), _owner_id(ADMIN_EMAIL)
    assert _run_session(lambda db: get_path_setting(db, user)) == "cloud"
    assert _run_session(lambda db: get_path_setting(db, admin)) == "auto"


# ---------------------------------------------------------------------------
# Cloud worker failover (drives the real process() with a stubbed Meta)
# ---------------------------------------------------------------------------

async def _noop_worker(*_args, **_kwargs):
    return None


_META = {"calls": [], "fail": True}


async def _meta_request(*_args, **_kwargs):
    """Mocked Meta Graph API: fails with a 5xx-style error, or answers."""
    _META["calls"].append(True)
    if _META["fail"]:
        raise TransportError("provider_unavailable", retryable=True, ambiguous=False)
    return TransportResponse(status=200, body={"messages": [{"id": "wamid.synthetic-reply"}]})


@pytest.fixture
def failover_client(monkeypatch):
    """A TestClient with Meta stubbed and the agent execution stubbed."""
    monkeypatch.setattr(cloud_integration, "run_whatsapp_agent_worker", _noop_worker)
    monkeypatch.setattr(cloud_integration, "provider_request", _meta_request)
    _stub_execute(monkeypatch)
    _META["calls"].clear()
    _META["fail"] = True
    with TestClient(app, headers=CSRF) as test_client:
        configure_host(StudioHost(app.state.sessionmaker, get_settings(), app.state.cipher,
                                  app.state.llm_runtime, app.state.rag))
        yield test_client
        configure_host(None)


class _FakeBridge:
    """A live Baileys session stand-in: online, process alive, records sends."""

    def __init__(self, *, ok=True):
        self.online = asyncio.Event()
        self.online.set()
        self.process = SimpleNamespace(poll=lambda: None)
        self.sent = []
        self.ok = ok

    async def send_reply(self, delivery_id, jid, reply, *, timeout=30):
        self.sent.append((delivery_id, jid, reply))
        if isinstance(self.ok, Exception):
            raise self.ok
        return self.ok


def _seed_live_baileys(client, agent, *, ok=True):
    """A Baileys connection row for the admin + a live fake session in the registry.

    The registry entry is the ``(bridge, task)`` tuple production stores:
    the Baileys supervisor (running in the TestClient lifespan) unpacks
    every entry, so a bare session would raise there every 2 seconds.
    The task is a real never-ending task on the app's loop; the supervisor
    cancels it at teardown, exactly as it does a real bridge task.
    """
    async def seed(db):
        owner = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        connection = uuid.uuid4()
        await db.execute(insert(BaileysConnection).values(id=connection, owner_id=owner, agent_id=agent))
        return connection

    connection = run_db(seed)
    sessions = getattr(app.state, "baileys_sessions", None)
    if sessions is None:
        sessions = app.state.baileys_sessions = {}
    fake = _FakeBridge(ok=ok)

    async def _spawn():
        return asyncio.create_task(asyncio.sleep(3600))

    sessions[connection] = (fake, client.portal.call(_spawn))
    return connection, fake, sessions


def _drive_intake(client, sessions):
    """Claim the queued inbound and run the Cloud worker intake once."""
    settings = Settings(workflow_engine_worker_enabled=True)

    async def drive():
        identity = await cloud_integration.claim(app.state.sessionmaker)
        assert identity is not None, "no queued WhatsApp inbound to claim"
        await cloud_integration.process(app.state.sessionmaker, identity, settings,
                                        app.state.cipher, app.state.llm_runtime, app.state.rag,
                                        baileys_sessions=sessions)

    client.portal.call(drive)


def _inbound_state(connection):
    async def state(db):
        row = (await db.execute(select(
            AgentStudioWhatsAppInbound.id, AgentStudioWhatsAppInbound.status,
            AgentStudioWhatsAppInbound.reply_ciphertext).where(
            AgentStudioWhatsAppInbound.connection_id == connection))).one()
        phase = await db.scalar(select(WhatsAppDispatch.phase).where(
            WhatsAppDispatch.inbound_id == row.id))
        return row.id, row.status, row.reply_ciphertext is not None, phase

    return run_db(state)


def _failover_audit_rows():
    async def rows(db):
        result = await db.execute(select(
            AuditLog.outcome, AuditLog.target, AuditLog.details).where(
            AuditLog.action == "integration.whatsapp_failover"))
        return result.all()

    return run_db(rows)


def _whatsapp_ledger_keys():
    async def keys(db):
        return (await db.scalars(select(PlatformActionExecution.idempotency_key))).all()

    return [k for k in run_db(keys) if k.startswith("whatsapp:")]


def test_cloud_500_fails_over_to_live_baileys_once(failover_client):
    connection, _agent = _seed_connection(failover_client)
    _baileys, fake, sessions = _seed_live_baileys(failover_client, _agent)
    assert _post(failover_client, connection, _payload()).status_code == 200
    _drive_intake(failover_client, sessions)

    status, has_reply, phase = _inbound_state(connection)[1:]
    assert status == "completed"
    assert has_reply
    # Cloud was attempted once per phase and failed; Baileys sent exactly
    # once per phase (progress + final), to the inbound sender's JID.
    assert len(_META["calls"]) == 2
    assert len(fake.sent) == 2
    assert {jid for _delivery, jid, _text in fake.sent} == {"30000000@s.whatsapp.net"}
    delivery_ids = [delivery for delivery, _jid, _text in fake.sent]
    assert len(set(delivery_ids)) == 2  # progress and final are distinct sends
    assert all(text for _delivery, _jid, text in fake.sent)
    # One failover audit row per phase, and exactly two whatsapp ledger rows.
    rows = _failover_audit_rows()
    assert len(rows) == 2
    for outcome, target, details in rows:
        assert outcome == "success"
        assert target.startswith("whatsapp:")
        assert details["from_path"] == "cloud" and details["to_path"] == "baileys"
        assert details["reason"] == "provider_unavailable"
    assert sorted(k.rsplit(":", 1)[-1] for k in _whatsapp_ledger_keys()) == ["final", "progress"]


def test_path_setting_baileys_sends_on_baileys_when_cloud_healthy(failover_client):
    connection, _agent = _seed_connection(failover_client)
    _baileys, fake, sessions = _seed_live_baileys(failover_client, _agent)
    _META["fail"] = False
    owner = _owner_id(ADMIN_EMAIL)
    _run_session(lambda db: set_path_setting(db, owner, "baileys"))
    assert _post(failover_client, connection, _payload()).status_code == 200
    _drive_intake(failover_client, sessions)

    status, _has_reply, _phase = _inbound_state(connection)[1:]
    assert status == "completed"
    # The setting is respected: Baileys sent both phases, Cloud was never called.
    assert len(fake.sent) == 2
    assert _META["calls"] == []
    assert _failover_audit_rows() == []


def test_path_setting_cloud_sends_on_cloud_when_baileys_live(failover_client):
    connection, _agent = _seed_connection(failover_client)
    _baileys, fake, sessions = _seed_live_baileys(failover_client, _agent)
    _META["fail"] = False
    owner = _owner_id(ADMIN_EMAIL)
    _run_session(lambda db: set_path_setting(db, owner, "cloud"))
    assert _post(failover_client, connection, _payload()).status_code == 200
    _drive_intake(failover_client, sessions)

    status, _has_reply, _phase = _inbound_state(connection)[1:]
    assert status == "completed"
    assert len(_META["calls"]) == 2
    assert fake.sent == []
    assert _failover_audit_rows() == []


def test_both_paths_down_dispatch_uncertain_no_duplicate(failover_client):
    connection, _agent = _seed_connection(failover_client)
    _baileys, fake, sessions = _seed_live_baileys(failover_client, _agent, ok=False)
    assert _post(failover_client, connection, _payload()).status_code == 200
    _drive_intake(failover_client, sessions)

    inbound_id, status, _has_reply, phase = _inbound_state(connection)
    # Progress failed on both paths: the dispatch is uncertain, not failed
    # outright, and the final send was never attempted.
    assert status == "failed"
    assert phase == "uncertain"
    assert len(_META["calls"]) == 1  # Cloud attempted once (progress)
    assert len(fake.sent) == 1  # Baileys attempted once (progress), timed out
    rows = _failover_audit_rows()
    assert len(rows) == 1
    outcome, target, details = rows[0]
    assert outcome == "failure"
    assert target == f"whatsapp:{inbound_id}:progress"
    assert details["from_path"] == "cloud" and details["to_path"] == "baileys"
    assert details["fallback_code"] == "delivery_failed"
    # Exactly one whatsapp ledger row: the failed progress dispatch, never replayed.
    assert [k.rsplit(":", 1)[-1] for k in _whatsapp_ledger_keys()] == ["progress"]


# ---------------------------------------------------------------------------
# Baileys worker failover (dead bridge -> Cloud once)
# ---------------------------------------------------------------------------

def test_baileys_bridge_down_fails_over_to_cloud_once(client, monkeypatch):
    monkeypatch.setattr(agent_studio_baileys, "bridge_available", lambda: False)
    monkeypatch.setattr(agent_studio_baileys, "windows_host_status", lambda: "Measured diagnostics")
    monkeypatch.setattr(cloud_integration, "provider_request", _meta_request)
    _META["calls"].clear()
    _META["fail"] = False
    cloud_connection, agent = _seed_connection(client)

    message_id = uuid.uuid4()
    baileys_connection = uuid.uuid4()

    async def seed(db):
        owner = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        await db.execute(insert(BaileysConnection).values(
            id=baileys_connection, owner_id=owner, agent_id=agent))
        await db.execute(insert(BaileysMessage).values(
            id=message_id, connection_id=baileys_connection, external_id=message_id.hex,
            status="processing",
            payload_sealed=seal(app.state.cipher, f"message:{message_id}", json.dumps({
                "text": "hello", "jid": "10000000@s.whatsapp.net",
            })),
            reply_sealed=seal(app.state.cipher, f"reply:{message_id}", "Test reply from the agent."),
        ))

    run_db(seed)
    bridge = BridgeSession(app, get_settings(), baileys_connection)
    bridge.online.set()
    calls = []

    async def _dead_bridge(delivery_id, jid, reply, *, timeout):
        calls.append((delivery_id, jid, reply))
        raise RuntimeError("bridge down")

    bridge.send_reply = _dead_bridge

    async def run():
        await bridge.execute(message_id)

    client.portal.call(run)

    async def final_status(db):
        return await db.scalar(select(BaileysMessage.status).where(BaileysMessage.id == message_id))

    assert run_db(final_status) == "completed"
    # The bridge was tried once and raised; Cloud sent exactly once.
    assert len(calls) == 1
    assert calls[0][1] == "10000000@s.whatsapp.net"
    assert len(_META["calls"]) == 1
    rows = _failover_audit_rows()
    assert len(rows) == 1
    outcome, target, details = rows[0]
    assert outcome == "success"
    assert target == f"baileys:{message_id}:final"
    assert details["from_path"] == "baileys" and details["to_path"] == "cloud"
    assert details["reason"] == "bridge_unavailable"
