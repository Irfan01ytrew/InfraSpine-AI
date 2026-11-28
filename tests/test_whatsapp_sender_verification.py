"""Step F slice 2: sender verification flow + per-sender intake rate limits.

Builds on the slice 1 harness (`tests/test_whatsapp_unified_intake.py`): the same
throwaway `*_test` database, mocked Meta transport, stubbed agent execution. Nothing
here calls the real Meta API.

Proved here:

* the verification flow: request creates a pending row that grants nothing; only the
  requesting user can confirm it; a confirmed sender runs as themselves; a number
  linked to someone else can never be taken over;
* a revoked identity is refused even if the number is still on the connection
  allow-list, and re-request + confirm restores it;
* the legacy allow-list mirror no longer mints identities for new numbers, while a
  legacy row minted before slice 2 keeps working until it is revoked;
* per-sender intake is bounded at 10 messages / minute per connection: excess
  messages are never stored as tasks, are audited, and a webhook with nothing
  accepted answers 429;
* the rate limit never blocks the verification flow itself.

Known boundary (platform rule, not slice 2): agents are owner-scoped
(`AgentStudioService._require_agent`), so a verified sender who is NOT the
connection owner cannot drive the owner's agent. Slice 2 guarantees such a sender
never silently runs as the owner — their message is refused with no run at all.
Whether verified non-owners may use the connection's agent is an owner decision
for a later slice (agent sharing).
"""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agent_studio.unified import StudioHost, configure_host
from app.core.config import get_settings
from app.core.errors import PermissionDeniedError
from app.db.agent_studio_models import AgentStudioRun
from app.db.agent_studio_whatsapp_models import (
    AgentStudioWhatsAppContact,
    AgentStudioWhatsAppInbound,
)
from app.db.models import AuditLog, User
from app.db.session import test_engine_options as _test_engine_options
from app.integrations import whatsapp as cloud_integration
from app.integrations.transport import Response as TransportResponse
from app.main import app
from app.platform.channels import (
    confirm_sender_verification,
    request_sender_verification,
    resolve_whatsapp_sender,
    revoke_sender_identity,
    sync_allowed_senders,
)
from app.platform.models import PlatformChannelIdentity
from app.services.agent_studio_whatsapp import WHATSAPP_INTAKE_RATE_LIMIT
from tests.conftest import ADMIN_EMAIL, CSRF, TEST_DATABASE_URL, USER2_EMAIL, run_db
from tests.test_agent_studio_unified import _stub_execute
from tests.test_whatsapp_unified_intake import (
    SENDER,
    _drive_intake,
    _inbound_count,
    _payload,
    _post,
    _seed_connection,
    _studio_runs,
)

OWNER_NUMBER = "31000005"
OTHER_NUMBER = "31000006"
UNVERIFIED_NUMBER = "31000009"


async def _noop_worker(*_args, **_kwargs):
    return None


async def _fake_meta_request(*_args, **_kwargs):
    """Mocked Meta Graph API: every send is accepted with a synthetic id. No network."""
    return TransportResponse(status=200, body={"messages": [{"id": "wamid.synthetic-reply"}]})


@pytest.fixture
def quiet_client(monkeypatch):
    """A TestClient whose lifespan starts no background workers, with Meta + LLM stubbed."""
    monkeypatch.setattr(cloud_integration, "run_whatsapp_agent_worker", _noop_worker)
    monkeypatch.setattr(cloud_integration, "provider_request", _fake_meta_request)
    _stub_execute(monkeypatch)
    with TestClient(app, headers=CSRF) as test_client:
        configure_host(StudioHost(app.state.sessionmaker, get_settings(), app.state.cipher,
                                  app.state.llm_runtime, app.state.rag))
        yield test_client
        configure_host(None)


def run_session(fn):
    """Run `await fn(sessionmaker)` against the throwaway test database."""

    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


async def _user_by_email(db, email):
    return await db.scalar(select(User).where(User.email == email))


def _user_id(email):
    def body(sm):
        async def go():
            async with sm() as db:
                return (await _user_by_email(db, email)).id

        return go()

    return run_session(body)


def _verify_number(number, email):
    """Run the Step F verification flow for `number` as the user with `email`."""

    def body(sm):
        async def go():
            async with sm() as db:
                user = await _user_by_email(db, email)
                await request_sender_verification(db, user_id=user.id, channel="whatsapp_cloud",
                                                  external_id=number)
                confirmed = await confirm_sender_verification(db, user_id=user.id,
                                                              channel="whatsapp_cloud",
                                                              external_id=number)
                await db.commit()
                return confirmed

        return go()

    return run_session(body)


def _revoke_number(number, email):
    def body(sm):
        async def go():
            async with sm() as db:
                user = await _user_by_email(db, email)
                await revoke_sender_identity(db, user_id=user.id, channel="whatsapp_cloud",
                                             external_id=number)
                await db.commit()

        return go()

    return run_session(body)


def _inbound_state(connection):
    async def state(db):
        row = (await db.execute(select(
            AgentStudioWhatsAppInbound.status, AgentStudioWhatsAppInbound.last_error).where(
            AgentStudioWhatsAppInbound.connection_id == connection))).one()
        return row.status, row.last_error

    return run_db(state)


def _rate_limited_audit_count():
    return run_db(lambda db: db.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == "integration.whatsapp_rate_limited")))


def _burst_payload(*, sender, count):
    data = json.dumps({"entry": [{"id": "100000", "changes": [{"value": {
        "metadata": {"phone_number_id": "200000"},
        "messages": [{"id": f"wamid.burst-{i}", "from": sender, "type": "text",
                      "text": {"body": f"burst {i}"}, "timestamp": "1795800000"}
                     for i in range(count)],
    }}]}]}).encode()
    return data


# --- Verification flow: pure function tests -------------------------------------


def test_verification_flow_request_confirm_revoke():
    def body(sm):
        async def go():
            async with sm() as db:
                owner = await _user_by_email(db, ADMIN_EMAIL)
                other = await _user_by_email(db, USER2_EMAIL)
                number = "31000001"
                # Request creates a pending row that grants nothing.
                pending = await request_sender_verification(
                    db, user_id=owner.id, channel="whatsapp_cloud", external_id=number)
                assert pending.verified_at is None
                assert pending.verification_method == "whatsapp:pending"
                with pytest.raises(PermissionDeniedError):
                    await resolve_whatsapp_sender(db, channel="whatsapp_cloud", sender=number)
                # Only the requesting user can confirm it.
                with pytest.raises(PermissionDeniedError):
                    await confirm_sender_verification(
                        db, user_id=other.id, channel="whatsapp_cloud", external_id=number)
                confirmed = await confirm_sender_verification(
                    db, user_id=owner.id, channel="whatsapp_cloud", external_id=number)
                assert confirmed.verified_at is not None
                assert confirmed.verification_method == "whatsapp:confirmed"
                principal = await resolve_whatsapp_sender(db, channel="whatsapp_cloud", sender=number)
                assert principal.user_id == owner.id
                # Requesting again is idempotent; a different user can never take the number over.
                again = await request_sender_verification(
                    db, user_id=owner.id, channel="whatsapp_cloud", external_id=number)
                assert again.id == confirmed.id
                with pytest.raises(PermissionDeniedError):
                    await request_sender_verification(
                        db, user_id=other.id, channel="whatsapp_cloud", external_id=number)
                # Revoke cuts the channel; re-request starts a fresh pending link.
                await revoke_sender_identity(
                    db, user_id=owner.id, channel="whatsapp_cloud", external_id=number)
                with pytest.raises(PermissionDeniedError):
                    await resolve_whatsapp_sender(db, channel="whatsapp_cloud", sender=number)
                fresh = await request_sender_verification(
                    db, user_id=owner.id, channel="whatsapp_cloud", external_id=number)
                assert fresh.id != confirmed.id
                assert fresh.verified_at is None
                await db.commit()

        return go()

    run_session(body)


def test_legacy_allow_list_no_longer_mints_identities():
    def body(sm):
        async def go():
            async with sm() as db:
                owner = await _user_by_email(db, ADMIN_EMAIL)
                # A fresh allow-listed number gets NO identity from the sync.
                await sync_allowed_senders(db, owner_id=owner.id, channel="whatsapp_cloud",
                                           allowed=["31000002"])
                await db.commit()
                rows = list(await db.scalars(select(PlatformChannelIdentity).where(
                    PlatformChannelIdentity.channel == "whatsapp_cloud",
                    PlatformChannelIdentity.external_id == "31000002",
                    PlatformChannelIdentity.revoked_at.is_(None))))
                assert rows == []
                with pytest.raises(PermissionDeniedError):
                    await resolve_whatsapp_sender(
                        db, channel="whatsapp_cloud", sender="31000002",
                        allowed=["31000002"], owner_id=owner.id)
                # A legacy row minted before slice 2 keeps working until it is revoked...
                legacy = PlatformChannelIdentity(
                    user_id=owner.id, channel="whatsapp_cloud", external_id="31000003",
                    verified_at=datetime.now(UTC), verification_method="legacy:allowed_senders")
                db.add(legacy)
                await db.commit()
                principal = await resolve_whatsapp_sender(
                    db, channel="whatsapp_cloud", sender="31000003",
                    allowed=["31000003"], owner_id=owner.id)
                assert principal.user_id == owner.id
                # ... and leaving the allow-list revokes it.
                await sync_allowed_senders(db, owner_id=owner.id, channel="whatsapp_cloud",
                                           allowed=[])
                await db.commit()
                with pytest.raises(PermissionDeniedError):
                    await resolve_whatsapp_sender(
                        db, channel="whatsapp_cloud", sender="31000003",
                        allowed=["31000003"], owner_id=owner.id)

        return go()

    run_session(body)


# --- Verification flow: end-to-end intake tests ---------------------------------


def test_unverified_sender_gets_no_run(quiet_client):
    connection, _agent = _seed_connection(quiet_client, allowed=(UNVERIFIED_NUMBER,))
    assert _post(quiet_client, connection, _payload(sender=UNVERIFIED_NUMBER)).status_code == 200
    # The message is stored (allow-list gate passes) but the worker refuses it: identity
    # is bound at process time, and an unverified sender binds to nobody.
    assert _inbound_count(connection) == 1
    _drive_intake(quiet_client)
    assert _studio_runs() == []
    status, last_error = _inbound_state(connection)
    assert status == "failed"
    assert last_error == "identity_revoked"


def test_verified_owner_sender_runs_as_themselves(quiet_client):
    connection, _agent = _seed_connection(quiet_client, allowed=(OWNER_NUMBER,))
    # No identity exists yet: the number acts only after the explicit flow.
    owner_id = _user_id(ADMIN_EMAIL)
    confirmed = _verify_number(OWNER_NUMBER, ADMIN_EMAIL)
    assert confirmed.verified_at is not None
    assert _post(quiet_client, connection, _payload(sender=OWNER_NUMBER)).status_code == 200
    assert _inbound_count(connection) == 1
    _drive_intake(quiet_client)

    runs = _studio_runs()
    assert len(runs) == 1
    assert runs[0].status == "completed"

    async def owner_of_run(db):
        return await db.scalar(select(AgentStudioRun.owner_id))

    assert run_db(owner_of_run) == owner_id

    async def owner_of_contact(db):
        return await db.scalar(select(AgentStudioWhatsAppContact.owner_id).where(
            AgentStudioWhatsAppContact.wa_id == OWNER_NUMBER))

    assert run_db(owner_of_contact) == owner_id


def test_verified_non_owner_sender_never_runs_as_the_connection_owner(quiet_client):
    connection, _agent = _seed_connection(quiet_client, allowed=(OTHER_NUMBER,))
    _verify_number(OTHER_NUMBER, USER2_EMAIL)
    assert _post(quiet_client, connection, _payload(sender=OTHER_NUMBER)).status_code == 200
    assert _inbound_count(connection) == 1
    _drive_intake(quiet_client)
    # Agents are owner-scoped, so the verified non-owner cannot drive the owner's agent —
    # and slice 2 guarantees they never silently run as the owner instead: no run at all.
    assert _studio_runs() == []
    status, _last_error = _inbound_state(connection)
    assert status == "failed"


def test_revoked_identity_is_refused_even_on_allow_list(quiet_client):
    connection, _agent = _seed_connection(quiet_client)
    _revoke_number(SENDER, ADMIN_EMAIL)
    assert _post(quiet_client, connection, _payload()).status_code == 200
    assert _inbound_count(connection) == 1
    _drive_intake(quiet_client)
    assert _studio_runs() == []
    status, last_error = _inbound_state(connection)
    assert status == "failed"
    assert last_error == "identity_revoked"
    # Re-request + confirm restores the sender.
    _verify_number(SENDER, ADMIN_EMAIL)
    assert _post(quiet_client, connection,
                 _payload(message_id="wamid.synthetic-2")).status_code == 200
    assert _inbound_count(connection) == 2
    _drive_intake(quiet_client)
    assert len(_studio_runs()) == 1
    assert _studio_runs()[0].status == "completed"


# --- Per-sender intake rate limits ----------------------------------------------


def test_rate_limit_breach_is_rejected_with_429_and_audit(quiet_client):
    connection, _agent = _seed_connection(quiet_client)
    for i in range(WHATSAPP_INTAKE_RATE_LIMIT):
        assert _post(quiet_client, connection,
                     _payload(message_id=f"wamid.rate-{i}")).status_code == 200
    assert _inbound_count(connection) == WHATSAPP_INTAKE_RATE_LIMIT
    response = _post(quiet_client, connection, _payload(message_id="wamid.rate-over"))
    assert response.status_code == 429
    # The excess message was never stored as a task, and the breach was audited.
    assert _inbound_count(connection) == WHATSAPP_INTAKE_RATE_LIMIT
    assert _rate_limited_audit_count() >= 1
    # The stored messages still run.
    _drive_intake(quiet_client)
    assert len(_studio_runs()) == 1


def test_rate_limit_counts_messages_inside_one_payload(quiet_client):
    connection, _agent = _seed_connection(quiet_client)
    response = _post(quiet_client, connection,
                     _burst_payload(sender=SENDER, count=WHATSAPP_INTAKE_RATE_LIMIT + 2))
    # Ten accepted, two skipped — the webhook still answers 200 for the accepted ones.
    assert response.status_code == 200
    assert _inbound_count(connection) == WHATSAPP_INTAKE_RATE_LIMIT
    assert _rate_limited_audit_count() >= 2


def test_rate_limit_never_blocks_verification(quiet_client):
    connection, _agent = _seed_connection(quiet_client)
    owner_id = _user_id(ADMIN_EMAIL)
    _revoke_number(SENDER, ADMIN_EMAIL)
    for i in range(WHATSAPP_INTAKE_RATE_LIMIT):
        assert _post(quiet_client, connection,
                     _payload(message_id=f"wamid.fill-{i}")).status_code == 200
    assert _inbound_count(connection) == WHATSAPP_INTAKE_RATE_LIMIT
    assert _post(quiet_client, connection, _payload(message_id="wamid.fill-over")).status_code == 429
    # The sender is at the intake limit — but the verification flow is in-app, not webhook,
    # so re-verifying the same number still works.
    pending = run_session(lambda sm: _request_only(sm, SENDER, owner_id))
    assert pending.verified_at is None
    confirmed = run_session(lambda sm: _confirm_only(sm, SENDER, owner_id))
    assert confirmed.verified_at is not None
    assert confirmed.verification_method == "whatsapp:confirmed"


async def _request_only(sm, number, user_id):
    async with sm() as db:
        row = await request_sender_verification(db, user_id=user_id, channel="whatsapp_cloud",
                                                external_id=number)
        await db.commit()
        return row


async def _confirm_only(sm, number, user_id):
    async with sm() as db:
        row = await confirm_sender_verification(db, user_id=user_id, channel="whatsapp_cloud",
                                                external_id=number)
        await db.commit()
        return row
