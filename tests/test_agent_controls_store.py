"""Step AK slice 2: control-record persistence, spend ledger, and HTTP API.

All tests run on the dedicated `infraspine_ak_test` database only (see
`.test-env.sh`, never committed). The admin user is granted `agents.manage` /
`agents.read` on the organisation scope per test, because the legacy admin
role deliberately does not imply them (deny by default).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from app.agent_controls.store import (
    create_control,
    get_control_record,
    kill_control,
    list_control_records,
    record_spend,
    record_spend_once,
    spend_total_for,
    update_control,
)
from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.agent_controls_models import AgentControlEvent, AgentSpendLedger
from app.db.agent_studio_models import AgentStudioApproval, AgentStudioRun
from app.db.models import User
from app.main import app
from app.platform import access as platform_access
from tests.conftest import ADMIN_EMAIL, USER_EMAIL, TEST_DATABASE_URL, login, run_db
from tests.test_agent_studio_queue import _create_agent_and_thread, _enqueue


async def _tx(fn, *, commit=True):
    """Run `await fn(db)` in one session; commit unless it raises."""
    async with app.state.sessionmaker() as db:
        try:
            result = await fn(db)
        except Exception:
            await db.rollback()
            raise
        if commit:
            await db.commit()
        return result


async def _admin_user_id(db):
    return await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))


async def _grant_controls(db):
    """Give the admin `agents.manage` + `agents.read` on the org scope."""
    admin_id = await _admin_user_id(db)
    admin = await db.get(User, admin_id)
    actor = platform_access.principal_for(admin)
    await platform_access.ensure_role(db, "ak-test-controls", {"agents.manage", "agents.read"},
                                      name="AK test controls")
    org = await platform_access.ensure_org_scope(db)
    await platform_access.grant_membership(db, actor, user_id=admin_id, scope_id=org.id,
                                           role_key="ak-test-controls")
    return admin_id


@pytest.fixture
def ak_grant(client):
    """Grant the admin control permissions; returns the admin user id."""
    return client.portal.call(lambda: _tx(_grant_controls))


@pytest.fixture
def admin_actor(client, ak_grant):
    """A platform Principal for the admin, usable with the store."""
    async def _load(db):
        admin = await db.get(User, ak_grant)
        return platform_access.principal_for(admin)
    return client.portal.call(lambda: _tx(_load, commit=False))


def _new_agent_id():
    return uuid.uuid4()


async def _create(db, actor, agent_id, **overrides):
    fields = {"agent_id": agent_id, "data_scopes": frozenset({"knowledge:finance"}),
              "permissions": frozenset({"documents.read"}),
              "allowed_actions": frozenset({"knowledge_search"}),
              "spend_limit_usd": Decimal("10.00")}
    fields.update(overrides)
    return await create_control(db, actor, **fields)


def test_create_control_round_trips_and_audits(client, admin_actor):
    agent_id = _new_agent_id()

    async def run(db):
        row = await _create(db, admin_actor, agent_id)
        assert row.version == 1
        assert row.status == "active"
        assert row.spend_used_usd == Decimal("0")
        assert row.spend_limit_usd == Decimal("10.00")
        found = await get_control_record(db, agent_id)
        assert found is not None and found.agent_id == agent_id
        events = await db.scalars(select(AgentControlEvent).where(
            AgentControlEvent.agent_id == agent_id))
        events = list(events)
        assert len(events) == 1 and events[0].event == "agent.control_created"
        assert events[0].after["spend_limit_usd"] == "10.0000"
        return True

    assert client.portal.call(lambda: _tx(run))


def test_create_duplicate_conflicts(client, admin_actor):
    agent_id = _new_agent_id()

    async def run(db):
        await _create(db, admin_actor, agent_id)
        with pytest.raises(ConflictError):
            await _create(db, admin_actor, agent_id)

    client.portal.call(lambda: _tx(run))


def test_create_rejects_wildcard_and_bad_money(client, admin_actor):
    agent_id = _new_agent_id()

    async def run(db):
        with pytest.raises(ValueError, match="wildcard"):
            await _create(db, admin_actor, agent_id, permissions=frozenset({"*"}))
        with pytest.raises(ValueError, match="wildcard"):
            await _create(db, admin_actor, uuid.uuid4(), allowed_actions=frozenset({"*"}))
        with pytest.raises(ValueError):
            await _create(db, admin_actor, uuid.uuid4(), spend_limit_usd=Decimal("-1"))

    client.portal.call(lambda: _tx(run))


def test_update_spend_limit_bumps_version_and_audits(client, admin_actor):
    agent_id = _new_agent_id()

    async def run(db):
        await _create(db, admin_actor, agent_id)
        row = await update_control(db, admin_actor, agent_id=agent_id, reason="raise the cap",
                                   spend_limit_usd=Decimal("25.00"))
        assert row.version == 2
        assert row.spend_limit_usd == Decimal("25.00")
        events = list(await db.scalars(select(AgentControlEvent).where(
            AgentControlEvent.agent_id == agent_id).order_by(AgentControlEvent.id)))
        assert [e.event for e in events] == ["agent.control_created", "agent.control_updated"]
        assert events[1].before["spend_limit_usd"] == "10.0000"
        assert events[1].after["spend_limit_usd"] == "25.0000"
        assert events[1].reason == "raise the cap"

    client.portal.call(lambda: _tx(run))


def test_update_cannot_kill_via_status_or_change_unknown_field(client, admin_actor):
    agent_id = _new_agent_id()

    async def run(db):
        await _create(db, admin_actor, agent_id)
        with pytest.raises(InvalidInputError):
            await update_control(db, admin_actor, agent_id=agent_id, reason="sneaky",
                                 status="killed")
        with pytest.raises(InvalidInputError):
            await update_control(db, admin_actor, agent_id=agent_id, reason="sneaky",
                                 killed_at=datetime.now(UTC))

    client.portal.call(lambda: _tx(run))


def test_update_missing_record_is_404(client, admin_actor):
    async def run(db):
        with pytest.raises(NotFoundError):
            await update_control(db, admin_actor, agent_id=uuid.uuid4(), reason="x",
                                 spend_limit_usd=Decimal("1"))

    client.portal.call(lambda: _tx(run))


def test_update_requires_manage_permission(client, ak_grant):
    agent_id = _new_agent_id()

    async def run(db):
        admin = await db.get(User, ak_grant)
        actor = platform_access.principal_for(admin)
        await _create(db, actor, agent_id)
        user = await db.scalar(select(User).where(User.email == USER_EMAIL))
        plain = platform_access.principal_for(user)
        with pytest.raises(PermissionDeniedError):
            await update_control(db, plain, agent_id=agent_id, reason="not mine",
                                 spend_limit_usd=Decimal("1"))

    client.portal.call(lambda: _tx(run))


def test_agent_cannot_change_its_own_record(client, admin_actor):
    agent_id = _new_agent_id()

    async def run(db):
        admin_id = await _admin_user_id(db)
        admin = await db.get(User, admin_id)
        await _create(db, admin_actor, agent_id)
        self_actor = platform_access.Principal(admin_id, admin.email, "agent", "agent", 0,
                                               agent_id=agent_id)
        with pytest.raises(PermissionDeniedError, match="own controls"):
            await update_control(db, self_actor, agent_id=agent_id, reason="widen me",
                                 permissions=frozenset({"documents.read", "funds.transfer"}))

    client.portal.call(lambda: _tx(run))


def test_kill_marks_record_and_audits(client, admin_actor):
    agent_id = _new_agent_id()

    async def run(db):
        await _create(db, admin_actor, agent_id)
        row, counts = await kill_control(db, admin_actor, agent_id=agent_id, reason="rogue spend")
        assert row.status == "killed"
        assert row.killed_at is not None
        assert row.kill_reason == "rogue spend"
        assert counts == {"agent_runs": 0, "agent_approvals": 0}
        events = list(await db.scalars(select(AgentControlEvent).where(
            AgentControlEvent.agent_id == agent_id).order_by(AgentControlEvent.id)))
        assert events[-1].event == "agent.killed"
        assert events[-1].after["status"] == "killed"

    client.portal.call(lambda: _tx(run))


def test_kill_cancels_queued_studio_run_and_pending_approval(admin_client, admin_actor):
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    queued = _enqueue(admin_client, agent_id, thread_id, "Do the risky thing", "ak-kill-test")
    assert queued.status_code == 202, queued.text
    run_id = uuid.UUID(queued.json()["id"])

    async def prepare(db):
        await _create(db, admin_actor, uuid.UUID(agent_id))
        approval = AgentStudioApproval(
            run_id=run_id, owner_id=await _admin_user_id(db), tool_name="web_search",
            action_ciphertext=b"x", action_nonce=b"y", action_key_id="test-key")
        db.add(approval)

    admin_client.portal.call(lambda: _tx(prepare))

    async def kill(db):
        row, counts = await kill_control(db, admin_actor, agent_id=uuid.UUID(agent_id),
                                         reason="stop everything")
        assert row.status == "killed"
        assert counts["agent_runs"] == 1
        assert counts["agent_approvals"] == 1
        run = await db.get(AgentStudioRun, run_id)
        assert run.status == "cancelled" and run.failure_code == "agent_killed"
        approval = await db.scalar(select(AgentStudioApproval).where(
            AgentStudioApproval.run_id == run_id))
        assert approval.status == "rejected"

    admin_client.portal.call(lambda: _tx(kill))


def test_kill_twice_second_is_a_repeat_event(client, admin_actor):
    agent_id = _new_agent_id()

    async def run(db):
        await _create(db, admin_actor, agent_id)
        await kill_control(db, admin_actor, agent_id=agent_id, reason="first")
        row, _ = await kill_control(db, admin_actor, agent_id=agent_id, reason="second attempt")
        assert row.status == "killed"
        events = list(await db.scalars(select(AgentControlEvent).where(
            AgentControlEvent.agent_id == agent_id).order_by(AgentControlEvent.id)))
        assert [e.event for e in events] == [
            "agent.control_created", "agent.killed", "agent.kill_repeat"]

    client.portal.call(lambda: _tx(run))


def test_record_spend_is_idempotent_and_sums_from_ledger(client, admin_actor):
    agent_id = _new_agent_id()

    async def run(db):
        await _create(db, admin_actor, agent_id)
        inserted, used = await record_spend(db, agent_id=agent_id, provider="deepseek",
                                            model="deepseek-flash", tokens=100,
                                            cost_usd=Decimal("2.50"), idempotency_key="call-1")
        assert inserted is True and used == Decimal("2.50")
        inserted, used = await record_spend(db, agent_id=agent_id, provider="deepseek",
                                            model="deepseek-flash", tokens=100,
                                            cost_usd=Decimal("2.50"), idempotency_key="call-1")
        assert inserted is False and used == Decimal("2.50")
        inserted, used = await record_spend(db, agent_id=agent_id, provider="deepseek",
                                            model="deepseek-flash", tokens=50,
                                            cost_usd=Decimal("1.25"), idempotency_key="call-2")
        assert inserted is True and used == Decimal("3.75")
        rows = list(await db.scalars(select(AgentSpendLedger).where(
            AgentSpendLedger.agent_id == agent_id)))
        assert len(rows) == 2
        row = await get_control_record(db, agent_id)
        assert row.spend_used_usd == Decimal("3.75")
        assert await spend_total_for(db, agent_id) == Decimal("3.75")

    client.portal.call(lambda: _tx(run))


def test_record_spend_rejects_negative_amounts(client, admin_actor):
    agent_id = _new_agent_id()

    async def run(db):
        await _create(db, admin_actor, agent_id)
        with pytest.raises(InvalidInputError):
            await record_spend(db, agent_id=agent_id, provider="x", model="y", tokens=1,
                               cost_usd=Decimal("-0.01"), idempotency_key="bad-1")
        with pytest.raises(InvalidInputError):
            await record_spend(db, agent_id=agent_id, provider="x", model="y", tokens=-1,
                               cost_usd=Decimal("0"), idempotency_key="bad-2")

    client.portal.call(lambda: _tx(run))


def test_record_spend_once_replays_without_double_count(client, admin_actor):
    agent_id = _new_agent_id()

    async def run(db):
        await _create(db, admin_actor, agent_id)
        first = await record_spend_once(
            db, admin_actor, agent_id=agent_id, provider="deepseek", model="deepseek-flash",
            tokens=10, cost_usd=Decimal("0.40"), idempotency_key="once-1")
        assert first.status == "verified" and first.replayed is False
        second = await record_spend_once(
            db, admin_actor, agent_id=agent_id, provider="deepseek", model="deepseek-flash",
            tokens=10, cost_usd=Decimal("0.40"), idempotency_key="once-1")
        assert second.replayed is True
        assert await spend_total_for(db, agent_id) == Decimal("0.40")

    client.portal.call(lambda: _tx(run))


def test_list_controls_filters_by_status(client, admin_actor):
    first, second = _new_agent_id(), _new_agent_id()

    async def run(db):
        await _create(db, admin_actor, first)
        await _create(db, admin_actor, second)
        await kill_control(db, admin_actor, agent_id=second, reason="off")
        assert len(await list_control_records(db)) == 2
        assert [r.agent_id for r in await list_control_records(db, status="killed")] == [second]
        assert [r.agent_id for r in await list_control_records(db, status="active")] == [first]

    client.portal.call(lambda: _tx(run))


def test_api_full_flow(admin_client, ak_grant):
    agent_id = str(uuid.uuid4())
    created = admin_client.post("/api/agents/controls", json={
        "agent_id": agent_id, "agent_kind": "studio",
        "data_scopes": ["knowledge:finance"], "permissions": ["documents.read"],
        "allowed_actions": ["knowledge_search"], "spend_limit_usd": "10.00"})
    assert created.status_code == 201, created.text
    assert created.json()["spend_state"] == "ok"

    listed = admin_client.get("/api/agents/controls")
    assert listed.status_code == 200 and len(listed.json()["controls"]) == 1

    patched = admin_client.patch(f"/api/agents/controls/{agent_id}", json={
        "reason": "raise the cap", "spend_limit_usd": "25.00"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["spend_limit_usd"] == "25.0000" and patched.json()["version"] == 2

    spent = admin_client.post(f"/api/agents/controls/{agent_id}/spend", json={
        "provider": "deepseek", "model": "deepseek-flash", "tokens": 10,
        "cost_usd": "0.40", "idempotency_key": "api-1"})
    assert spent.status_code == 200, spent.text
    assert spent.json()["spend_used_usd"] == "0.4000" and spent.json()["replayed"] is False
    replayed = admin_client.post(f"/api/agents/controls/{agent_id}/spend", json={
        "provider": "deepseek", "model": "deepseek-flash", "tokens": 10,
        "cost_usd": "0.40", "idempotency_key": "api-1"})
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    assert replayed.json()["spend_used_usd"] == "0.4000"

    summary = admin_client.get(f"/api/agents/controls/{agent_id}/spend")
    assert summary.status_code == 200 and summary.json()["spend_state"] == "ok"
    assert len(summary.json()["recent"]) == 1

    killed = admin_client.post(f"/api/agents/controls/{agent_id}/kill",
                               json={"reason": "end of test"})
    assert killed.status_code == 200, killed.text
    assert killed.json()["status"] == "killed"

    events = admin_client.get(f"/api/agents/controls/{agent_id}/events")
    assert events.status_code == 200
    assert [e["event"] for e in events.json()["events"]] == [
        "agent.killed", "agent.control_updated", "agent.control_created"]


def test_api_without_manage_is_forbidden(client, ak_grant):
    login(client, USER_EMAIL)
    agent_id = str(uuid.uuid4())
    assert client.post("/api/agents/controls", json={"agent_id": agent_id}).status_code == 403
    assert client.get("/api/agents/controls").status_code == 403
    assert client.get(f"/api/agents/controls/{agent_id}").status_code == 403


def test_api_kill_unknown_agent_is_404(admin_client, ak_grant):
    assert admin_client.post(f"/api/agents/controls/{uuid.uuid4()}/kill",
                             json={"reason": "x"}).status_code == 404


def test_migration_0105_roundtrip():
    def tables():
        rows = run_db(lambda c: c.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        ))).all()
        return {row.table_name for row in rows}

    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["configure_logger"] = False
    assert {"agent_control_records", "agent_control_events", "agent_spend_ledger"} <= tables()
    try:
        command.downgrade(cfg, "0103_unified_studio_governance")
        remaining = tables()
        assert "agent_control_records" not in remaining
        assert "agent_control_events" not in remaining
        assert "agent_spend_ledger" not in remaining
    finally:
        command.upgrade(cfg, "head")
    assert {"agent_control_records", "agent_control_events", "agent_spend_ledger"} <= tables()
