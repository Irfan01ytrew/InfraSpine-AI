"""Step AK slice 3: enforcement wiring.

Proves the gate at every wired point, on the dedicated `infraspine_ak_test`
database only:
- `resolve_principal` refuses a controlled-stopped agent, and `authorize`
  re-checks a principal resolved while it was still active;
- `gate_agent_action` allows / denies / needs-approval, audits the decisions
  that matter, and never lets a denied action reach its dispatch;
- the Studio worker never claims a run whose agent is stopped;
- a governance proposal from a stopped profile agent is refused before
  execution, even though it was approved earlier.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agent_controls.gate import gate_agent_action, gate_agent_liveness, require_agent_action
from app.agent_controls.store import create_control, kill_control
from app.agent_studio.worker import claim_agent_studio_run
from app.core.errors import PermissionDeniedError
from app.db.agent_controls_models import AgentControlEvent
from app.db.models import User
from app.main import app
from app.platform import access as platform_access
from app.services.agent_action_runtime import execute_proposal
from tests.conftest import ADMIN_EMAIL
from tests.test_agent_controls_store import _grant_controls, _tx
from tests.test_agent_studio_queue import _create_agent_and_thread, _enqueue


@pytest.fixture
def ak_grant(client):
    return client.portal.call(lambda: _tx(_grant_controls))


@pytest.fixture
def admin_actor(client, ak_grant):
    async def _load(db):
        admin = await db.get(User, ak_grant)
        return platform_access.principal_for(admin)
    return client.portal.call(lambda: _tx(_load, commit=False))


def _agent_principal(admin_id, email, agent_id):
    return platform_access.Principal(admin_id, email, "agent", "agent", 0, agent_id=agent_id)


async def _controlled_agent(db, actor, agent_id, **overrides):
    fields = {"agent_id": agent_id, "data_scopes": frozenset({"knowledge:finance"}),
              "permissions": frozenset({"documents.read"}),
              "allowed_actions": frozenset({"knowledge_search", "funds.transfer"}),
              "spend_limit_usd": Decimal("10.00")}
    fields.update(overrides)
    return await create_control(db, actor, **fields)


def test_resolve_principal_refuses_killed_agent(admin_client, admin_actor):
    agent_id, _thread = _create_agent_and_thread(admin_client)
    agent_uuid = uuid.UUID(agent_id)

    async def kill(db):
        await _controlled_agent(db, admin_actor, agent_uuid)
        await kill_control(db, admin_actor, agent_id=agent_uuid, reason="rogue")

    admin_client.portal.call(lambda: _tx(kill))

    async def resolve(db):
        with pytest.raises(PermissionDeniedError, match="killed"):
            await platform_access.resolve_principal(db, agent_id=agent_uuid)

    admin_client.portal.call(lambda: _tx(resolve, commit=False))


def test_resolve_principal_allows_active_agent_with_record(admin_client, admin_actor):
    agent_id, _thread = _create_agent_and_thread(admin_client)
    agent_uuid = uuid.UUID(agent_id)

    async def setup(db):
        await _controlled_agent(db, admin_actor, agent_uuid)

    admin_client.portal.call(lambda: _tx(setup))

    async def resolve(db):
        principal = await platform_access.resolve_principal(db, agent_id=agent_uuid)
        assert principal.kind == "agent" and principal.agent_id == agent_uuid

    admin_client.portal.call(lambda: _tx(resolve, commit=False))


def test_resolve_principal_allows_agent_without_record(admin_client):
    agent_id, _thread = _create_agent_and_thread(admin_client)
    agent_uuid = uuid.UUID(agent_id)

    async def resolve(db):
        principal = await platform_access.resolve_principal(db, agent_id=agent_uuid)
        assert principal.kind == "agent" and principal.agent_id == agent_uuid

    admin_client.portal.call(lambda: _tx(resolve, commit=False))


def test_authorize_rechecks_after_kill(admin_client, admin_actor):
    agent_id, _thread = _create_agent_and_thread(admin_client)
    agent_uuid = uuid.UUID(agent_id)

    async def setup(db):
        await _controlled_agent(db, admin_actor, agent_uuid)
        principal = await platform_access.resolve_principal(db, agent_id=agent_uuid)
        org = await platform_access.ensure_org_scope(db)
        decision = await platform_access.authorize(db, principal, "documents.read", org.id)
        assert decision.allowed is True
        await kill_control(db, admin_actor, agent_id=agent_uuid, reason="stopped mid-flight")
        decision = await platform_access.authorize(db, principal, "documents.read", org.id)
        assert decision.allowed is False and decision.reason == "agent_killed"

    admin_client.portal.call(lambda: _tx(setup))


def test_gate_allows_listed_action(client, admin_actor, ak_grant):
    agent_id = uuid.uuid4()

    async def run(db):
        await _controlled_agent(db, admin_actor, agent_id)
        admin = await db.get(User, ak_grant)
        principal = _agent_principal(admin.id, admin.email, agent_id)
        decision = await gate_agent_action(db, principal, action="knowledge_search",
                                           scope="knowledge:finance", permission="documents.read")
        assert decision.outcome == "allow"

    client.portal.call(lambda: _tx(run))


def test_gate_denies_unlisted_action_and_audits_it(client, admin_actor, ak_grant):
    agent_id = uuid.uuid4()
    dispatched = []

    async def run(db):
        await _controlled_agent(db, admin_actor, agent_id)
        admin = await db.get(User, ak_grant)
        principal = _agent_principal(admin.id, admin.email, agent_id)
        decision = await gate_agent_action(db, principal, action="document.delete",
                                           scope="knowledge:finance")
        assert decision.outcome == "deny" and decision.reason == "action_not_allowed"
        if decision.outcome == "allow":
            dispatched.append(True)
        events = list(await db.scalars(select(AgentControlEvent).where(
            AgentControlEvent.agent_id == agent_id)))
        assert [e.event for e in events] == ["agent.control_created", "agent.action_denied"]
        assert events[-1].after["outcome"] == "deny"

    client.portal.call(lambda: _tx(run))
    assert dispatched == []


def test_gate_denies_over_limit_cost_before_dispatch(client, admin_actor, ak_grant):
    agent_id = uuid.uuid4()

    async def run(db):
        await _controlled_agent(db, admin_actor, agent_id, spend_limit_usd=Decimal("1.00"))
        admin = await db.get(User, ak_grant)
        principal = _agent_principal(admin.id, admin.email, agent_id)
        decision = await gate_agent_action(db, principal, action="knowledge_search",
                                           scope="knowledge:finance", permission="documents.read",
                                           estimated_cost_usd=Decimal("1.01"))
        assert decision.outcome == "deny" and decision.reason == "spend_limit_exceeded"
        decision = await gate_agent_action(db, principal, action="knowledge_search",
                                           scope="knowledge:finance", permission="documents.read",
                                           estimated_cost_usd=Decimal("0.90"))
        assert decision.outcome == "allow" and decision.alerts == ("spend_near_limit",)

    client.portal.call(lambda: _tx(run))


def test_gate_needs_approval_for_sensitive_action(client, admin_actor, ak_grant):
    agent_id = uuid.uuid4()

    async def run(db):
        await _controlled_agent(db, admin_actor, agent_id)
        admin = await db.get(User, ak_grant)
        principal = _agent_principal(admin.id, admin.email, agent_id)
        decision = await gate_agent_action(db, principal, action="funds.transfer",
                                           scope="knowledge:finance", permission="documents.read")
        assert decision.outcome == "needs_approval" and decision.reason == "sensitive_action"
        events = list(await db.scalars(select(AgentControlEvent).where(
            AgentControlEvent.agent_id == agent_id)))
        assert events[-1].event == "agent.action_needs_approval"

    client.portal.call(lambda: _tx(run))


def test_gate_allows_non_agent_and_uncontrolled_agent(client, admin_actor, ak_grant):
    agent_id = uuid.uuid4()

    async def run(db):
        await _controlled_agent(db, admin_actor, agent_id)
        admin = await db.get(User, ak_grant)
        user_principal = platform_access.principal_for(admin)
        decision = await gate_agent_action(db, user_principal, action="anything",
                                           scope="knowledge:finance")
        assert decision.outcome == "allow" and decision.reason == "not_agent"
        uncontrolled = _agent_principal(admin.id, admin.email, uuid.uuid4())
        decision = await gate_agent_action(db, uncontrolled, action="knowledge_search",
                                           scope="knowledge:finance")
        assert decision.outcome == "allow" and decision.reason == "no_control_record"

    client.portal.call(lambda: _tx(run))


def test_require_agent_action_raises_on_deny(client, admin_actor, ak_grant):
    agent_id = uuid.uuid4()

    async def run(db):
        await _controlled_agent(db, admin_actor, agent_id)
        admin = await db.get(User, ak_grant)
        principal = _agent_principal(admin.id, admin.email, agent_id)
        with pytest.raises(PermissionDeniedError, match="action_not_allowed"):
            await require_agent_action(db, principal, action="document.delete",
                                       scope="knowledge:finance")

    client.portal.call(lambda: _tx(run))


def test_gate_agent_liveness_for_paused_and_killed(client, admin_actor):
    paused, killed = uuid.uuid4(), uuid.uuid4()

    async def run(db):
        await _controlled_agent(db, admin_actor, paused)
        await _controlled_agent(db, admin_actor, killed)
        from app.agent_controls.store import update_control
        await update_control(db, admin_actor, agent_id=paused, reason="pause",
                             status="paused")
        await kill_control(db, admin_actor, agent_id=killed, reason="off")
        assert (await gate_agent_liveness(db, paused)).reason == "agent_paused"
        assert (await gate_agent_liveness(db, killed)).reason == "agent_killed"
        assert (await gate_agent_liveness(db, uuid.uuid4())).outcome == "allow"

    client.portal.call(lambda: _tx(run))


def test_claim_excludes_killed_agent_run(admin_client, admin_actor):
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    queued = _enqueue(admin_client, agent_id, thread_id, "Run this", "ak-claim-test")
    assert queued.status_code == 202, queued.text
    run_id = uuid.UUID(queued.json()["id"])

    async def kill(db):
        await _controlled_agent(db, admin_actor, uuid.UUID(agent_id))
        await kill_control(db, admin_actor, agent_id=uuid.UUID(agent_id), reason="stop")

    admin_client.portal.call(lambda: _tx(kill))
    async def _claim(run_id):
        return await claim_agent_studio_run(app.state.sessionmaker, run_id=run_id)

    claimed = admin_client.portal.call(_claim, run_id)
    assert claimed is None


def test_claim_claims_active_agent_run(admin_client, admin_actor):
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    queued = _enqueue(admin_client, agent_id, thread_id, "Run this", "ak-claim-active")
    assert queued.status_code == 202, queued.text
    run_id = uuid.UUID(queued.json()["id"])

    async def setup(db):
        await _controlled_agent(db, admin_actor, uuid.UUID(agent_id))

    admin_client.portal.call(lambda: _tx(setup))
    async def _claim(run_id):
        return await claim_agent_studio_run(app.state.sessionmaker, run_id=run_id)

    claimed = admin_client.portal.call(_claim, run_id)
    assert claimed is not None and claimed[0] == run_id


def test_execute_proposal_refuses_killed_profile_agent(client, admin_actor):
    profile_id = uuid.uuid4()

    async def setup(db):
        await _controlled_agent(db, admin_actor, profile_id, agent_kind="profile")
        await kill_control(db, admin_actor, agent_id=profile_id, reason="rogue profile")

    client.portal.call(lambda: _tx(setup))

    async def attempt(db):
        admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
        from app.services.audit import AuditContext
        proposal = SimpleNamespace(agent_profile_id=profile_id, id=uuid.uuid4())
        with pytest.raises(PermissionDeniedError, match="stopped by its controls"):
            await execute_proposal(db, admin, AuditContext(admin.id, admin.email, None, None),
                                   proposal, None)

    client.portal.call(lambda: _tx(attempt, commit=False))
