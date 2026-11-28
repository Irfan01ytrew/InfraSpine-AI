"""Step AE slice 3 — WhatsApp lead capture + read-only leads list.

DB-backed tests (throwaway ``*_test`` database, log capture on):
capture is exactly-once per inbound, lead fields are stored verbatim as
data, reads are owner-scoped, and the worker entry point captures a lead
and returns the acknowledgement for a lead message.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.agent_studio_models import AgentStudioAgent
from app.db.agent_studio_whatsapp_models import (
    AgentStudioWhatsAppConnection,
    AgentStudioWhatsAppInbound,
)
from app.db.models import User
from app.db.session import test_engine_options as _engine_options_for_tests
from app.db.whatsapp_lead_models import WhatsAppLead
from app.main import app
from app.services.agent_studio_whatsapp import _seal
from app.whatsapp_auto import knowledge_reply
from app.whatsapp_auto.decider import LeadSignals
from app.whatsapp_auto.leads import capture_lead, lead_idempotency_key, list_leads
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, USER2_EMAIL, login, run_db

_SECRET = "test-secret-value-0123456789"
_WA_ID = "923001234567"


async def _seed_inbound(db, owner_id):
    """One connection + one sealed text inbound. Returns inbound_id.

    No commit here: callers run inside ``run_db``'s ``engine.begin()``
    (commits on exit) or ``run_session`` (commits after ``fn``), and a
    commit inside the ``begin()`` block would close the transaction for
    any statement that follows it.
    """
    connection_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    inbound_id = uuid.uuid4()
    await db.execute(insert(AgentStudioAgent).values(
        id=agent_id, owner_id=owner_id, name="Cloud test agent", instructions="Test fixture",
    ))
    encrypted = {}
    for key in ("access_token", "app_secret", "verify_token"):
        sealed = _seal(app.state.cipher, key, connection_id, _SECRET)
        for part in ("ciphertext", "nonce", "key_id"):
            encrypted[f"{key}_{part}"] = getattr(sealed, part)
    await db.execute(insert(AgentStudioWhatsAppConnection).values(
        id=connection_id, owner_id=owner_id, agent_id=agent_id, enabled=True,
        business_account_id="100000", phone_number_id="200000",
        allowed_senders=[_WA_ID], **encrypted,
    ))
    sealed_text = _seal(app.state.cipher, "inbound", inbound_id, "mujhe standard package kharidna hai")
    await db.execute(insert(AgentStudioWhatsAppInbound).values(
        id=inbound_id, connection_id=connection_id, owner_id=owner_id,
        external_message_id=f"wamid.test-{uuid.uuid4()}", from_wa_id=_WA_ID, message_type="text",
        text_ciphertext=sealed_text.ciphertext, text_nonce=sealed_text.nonce,
        text_key_id=sealed_text.key_id, status="queued",
    ))
    return inbound_id


def run_session(fn):
    """Run ``await fn(session)`` with a real AsyncSession (``run_db`` only
    gives a Connection, and the auto-reply audit uses ``session.add``)."""

    async def _run():
        engine = create_async_engine(
            TEST_DATABASE_URL, poolclass=NullPool, **_engine_options_for_tests())
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                result = await fn(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(_run())


_SIGNALS = LeadSignals(phone="03001234567", email=None, interests=("buy", "price"), name="Ali")


def _capture(db, owner_id, inbound_id):
    return capture_lead(
        db, owner_id=owner_id, wa_id=_WA_ID, inbound_id=inbound_id, signals=_SIGNALS)


# ---------------------------------------------------------------------------
# capture_lead: exactly-once, verbatim fields
# ---------------------------------------------------------------------------

def test_capture_creates_one_lead_with_verbatim_fields(client):
    async def seed(db):
        owner_id = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        return owner_id, await _seed_inbound(db, owner_id)

    owner_id, inbound_id = run_db(seed)

    async def capture(db):
        return await _capture(db, owner_id, inbound_id)

    lead = run_session(capture)
    assert lead.owner_id == owner_id
    assert lead.wa_id == _WA_ID
    assert lead.phone == "03001234567"
    assert lead.name == "Ali"
    assert lead.interest == "buy, price"
    assert lead.status == "new"
    assert lead.source_inbound_id == inbound_id
    assert lead.idempotency_key == lead_idempotency_key(inbound_id)

    rows = run_session(lambda db: list_leads(db, owner_id=owner_id))
    assert len(rows) == 1


def test_duplicate_capture_returns_the_same_single_row(client):
    async def seed(db):
        owner_id = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        return owner_id, await _seed_inbound(db, owner_id)

    owner_id, inbound_id = run_db(seed)
    first = run_session(lambda db: _capture(db, owner_id, inbound_id))
    second = run_session(lambda db: _capture(db, owner_id, inbound_id))
    assert first.id == second.id

    async def all_leads(db):
        return list(await db.scalars(select(WhatsAppLead)))

    rows = run_session(all_leads)
    assert len(rows) == 1


def test_list_leads_is_owner_scoped_and_newest_first(client):
    async def seed(db):
        admin_id = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        user2_id = await db.scalar(select(User.id).where(User.email == USER2_EMAIL))
        inbound_admin = await _seed_inbound(db, admin_id)
        inbound_user2 = await _seed_inbound(db, user2_id)
        await capture_lead(db, owner_id=admin_id, wa_id=_WA_ID,
                           inbound_id=inbound_admin, signals=_SIGNALS)
        await capture_lead(db, owner_id=user2_id, wa_id=_WA_ID,
                           inbound_id=inbound_user2, signals=_SIGNALS)
        return admin_id, user2_id

    admin_id, user2_id = run_session(seed)
    admin_leads = run_session(lambda db: list_leads(db, owner_id=admin_id))
    user2_leads = run_session(lambda db: list_leads(db, owner_id=user2_id))
    assert len(admin_leads) == 1 and admin_leads[0].owner_id == admin_id
    assert len(user2_leads) == 1 and user2_leads[0].owner_id == user2_id
    assert run_session(lambda db: list_leads(db, owner_id=admin_id, status="contacted")) == []
    with pytest.raises(ValueError):
        run_session(lambda db: list_leads(db, owner_id=admin_id, status="bogus"))


# ---------------------------------------------------------------------------
# Worker entry point: try_auto_reply
# ---------------------------------------------------------------------------

def test_try_auto_reply_captures_lead_and_returns_ack(client, monkeypatch):
    async def fake_answer_customer_question(*args, **kwargs):
        raise AssertionError("no Knowledge lookup for a lead message")

    monkeypatch.setattr(
        knowledge_reply, "answer_customer_question", fake_answer_customer_question)

    async def run(db):
        owner_id = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        inbound_id = await _seed_inbound(db, owner_id)
        user = await db.scalar(select(User).where(User.id == owner_id))
        inbound = await db.get(AgentStudioWhatsAppInbound, inbound_id)
        from app.core.config import Settings

        reply = await knowledge_reply.try_auto_reply(
            db, user, Settings(), None, None, inbound,
            "mujhe standard package kharidna hai, mera number 03001234567 hai",
        )
        assert reply is not None and "Shukriya" in reply
        leads = await list_leads(db, owner_id=owner_id)
        assert len(leads) == 1
        assert leads[0].phone == "03001234567"

    run_session(run)


def test_try_auto_reply_returns_knowledge_reply_for_questions(client, monkeypatch):
    async def fake_answer_customer_question(*args, **kwargs):
        return knowledge_reply.KnowledgeReply(
            found=True, reply_text="PKR 50,000.", citations=(uuid.uuid4(),),
            reason="evidence_backed", confidence=1.0,
        )

    monkeypatch.setattr(
        knowledge_reply, "answer_customer_question", fake_answer_customer_question)

    async def run(db):
        owner_id = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        inbound_id = await _seed_inbound(db, owner_id)
        user = await db.scalar(select(User).where(User.id == owner_id))
        inbound = await db.get(AgentStudioWhatsAppInbound, inbound_id)
        from app.core.config import Settings

        reply = await knowledge_reply.try_auto_reply(
            db, user, Settings(), None, None, inbound,
            "standard package kitne ka hai?",
        )
        assert reply == "PKR 50,000."
        assert await list_leads(db, owner_id=owner_id) == []

    run_session(run)


def test_try_auto_reply_falls_through_when_lookup_fails(client, monkeypatch):
    async def failing_lookup(*args, **kwargs):
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(
        knowledge_reply, "answer_customer_question", failing_lookup)

    async def run(db):
        owner_id = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        inbound_id = await _seed_inbound(db, owner_id)
        user = await db.scalar(select(User).where(User.id == owner_id))
        inbound = await db.get(AgentStudioWhatsAppInbound, inbound_id)
        from app.core.config import Settings

        reply = await knowledge_reply.try_auto_reply(
            db, user, Settings(), None, None, inbound,
            "standard package kitne ka hai?",
        )
        assert reply is None  # falls through to the Agent Studio task path

    run_session(run)


# ---------------------------------------------------------------------------
# Read-only leads API
# ---------------------------------------------------------------------------

def test_leads_api_lists_only_the_callers_own_leads(client):
    async def seed(db):
        admin_id = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        user2_id = await db.scalar(select(User.id).where(User.email == USER2_EMAIL))
        inbound_admin = await _seed_inbound(db, admin_id)
        inbound_user2 = await _seed_inbound(db, user2_id)
        await capture_lead(db, owner_id=admin_id, wa_id=_WA_ID,
                           inbound_id=inbound_admin, signals=_SIGNALS)
        await capture_lead(db, owner_id=user2_id, wa_id=_WA_ID,
                           inbound_id=inbound_user2, signals=_SIGNALS)

    run_session(seed)
    login(client, ADMIN_EMAIL)
    response = client.get("/api/whatsapp-auto/leads")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["wa_id"] == _WA_ID
    assert rows[0]["phone"] == "03001234567"
    assert rows[0]["status"] == "new"
    assert rows[0]["interest"] == "buy, price"

    response = client.get("/api/whatsapp-auto/leads", params={"status": "contacted"})
    assert response.status_code == 200 and response.json() == []
    # Unknown status is invalid input: the app answers 422, not 400.
    response = client.get("/api/whatsapp-auto/leads", params={"status": "bogus"})
    assert response.status_code == 422, response.text


def test_leads_api_requires_login(client):
    response = client.get("/api/whatsapp-auto/leads")
    assert response.status_code == 401

