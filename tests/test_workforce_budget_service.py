"""Step J (AI Workforce OS), slice 5: budget enforcement (DB tests).

All tests run on the dedicated ``infraspine_j121_test`` database only (see
``/home/hatch/.agent121-test-env.sh``, never committed). Run:
``source /home/hatch/.agent121-test-env.sh && python -m pytest tests/test_workforce_budget_service.py -q``
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.agent_studio.service import AgentStudioService, _encode_state
from app.agent_studio.worker import process_next_agent_studio_run
from app.core.config import get_settings
from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.agent_studio_models import AgentStudioAgent, AgentStudioRun
from app.db.models import User
from app.main import app
from app.platform import access as platform_access
from app.services.audit import AuditContext
from app.workforce.budget import BudgetLimits
from app.workforce.budget_service import WorkforceBudgetService
from app.workforce.registry_service import WorkforceRegistryService
from app.workforce.skill_service import SkillRegistryService
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL
from tests.test_agent_studio_pause import _process_args, _start_inline, _stub_execute
from tests.test_agent_studio_queue import _create_agent_and_thread, _enqueue


async def _tx(fn, *, commit=True):
    """Run ``await fn(db)`` in one session; commit unless it raises."""
    async with app.state.sessionmaker() as db:
        try:
            result = await fn(db)
        except Exception:
            await db.rollback()
            raise
        if commit:
            await db.commit()
        return result


async def _user_id(db, email):
    return await db.scalar(select(User.id).where(User.email == email))


async def _grant(db, role_key, permissions):
    admin_id = await _user_id(db, ADMIN_EMAIL)
    admin = await db.get(User, admin_id)
    actor = platform_access.principal_for(admin)
    await platform_access.ensure_role(db, role_key, permissions, name=f"J176 {role_key}")
    org = await platform_access.ensure_org_scope(db)
    await platform_access.grant_membership(
        db, actor, user_id=admin_id, scope_id=org.id, role_key=role_key)
    return admin_id


@pytest.fixture
def granted(client, clean_tables):
    """Grant the admin workforce permissions; returns the admin user id."""
    return client.portal.call(
        lambda: _tx(lambda db: _grant(db, "j176-test-workforce",
                                      {"workforce.manage", "workforce.read"})))


def _actor_for(client, email):
    async def _load(db):
        user = await db.scalar(select(User).where(User.email == email))
        actor = platform_access.principal_for(user)
        ctx = AuditContext(actor_user_id=user.id, actor_email=user.email,
                           ip_address=None, request_id=None)
        return actor, ctx
    return client.portal.call(lambda: _tx(_load, commit=False))


@pytest.fixture
def admin_actor(client, granted):
    return _actor_for(client, ADMIN_EMAIL)


@pytest.fixture
def owner_actor(client, clean_tables):
    return _actor_for(client, USER_EMAIL)


@pytest.fixture
def stranger_actor(client, clean_tables):
    return _actor_for(client, USER2_EMAIL)


def _svc(db, actor_ctx):
    actor, ctx = actor_ctx
    return WorkforceBudgetService(db, actor, ctx)


def _reg(db, actor_ctx):
    actor, ctx = actor_ctx
    return WorkforceRegistryService(db, actor, ctx)


async def _make_studio_agent(db, owner_email, name="Budget agent"):
    owner_id = await _user_id(db, owner_email)
    agent = AgentStudioAgent(owner_id=owner_id, name=name,
                             instructions="Do the task.",
                             tool_permissions=["web_search"], approval_tools=[],
                             delegate_agent_ids=[])
    db.add(agent)
    await db.flush()
    return agent.id


async def _insert_run(db, agent_id, thread_id, owner_id, *, cost=None, status="completed"):
    """A finished run row with a recorded cost (the budget's spend input)."""
    run_id = uuid.uuid4()
    sealed = _encode_state(app.state.cipher, run_id, {})
    run = AgentStudioRun(
        id=run_id, agent_id=agent_id, thread_id=thread_id, owner_id=owner_id,
        status=status, state_ciphertext=sealed.ciphertext, state_nonce=sealed.nonce,
        state_key_id=sealed.key_id, estimated_cost_usd=cost)
    db.add(run)
    await db.flush()
    return run_id


def _run_row(client, run_id):
    async def load(db):
        run = await db.get(AgentStudioRun, run_id)
        return run.status, run.attempt_count, run.next_attempt_at, run.claimed_at
    return client.portal.call(lambda: _tx(load, commit=False))


# -- budgets -------------------------------------------------------------------

def test_budget_defaults_are_ten_dollars_a_month(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        budget = await svc.get_budget("studio", agent_id)
        assert budget["monthly_spend_limit_usd"] == Decimal("10")
        assert budget["daily_spend_limit_usd"] is None
        assert budget["explicit"] is False
        return True

    assert client.portal.call(lambda: _tx(run))


def test_set_budget_round_trip_and_gates(client, admin_actor, stranger_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        saved = await svc.set_budget("studio", agent_id, limits=BudgetLimits(
            daily_spend_limit_usd=Decimal("1"), monthly_spend_limit_usd=Decimal("5"),
            daily_call_limit=10, monthly_call_limit=100))
        assert saved["explicit"] is True
        assert saved["daily_spend_limit_usd"] == Decimal("1")
        assert saved["monthly_call_limit"] == 100
        with pytest.raises(InvalidInputError):
            await svc.set_budget("studio", agent_id, limits=BudgetLimits(
                monthly_spend_limit_usd=Decimal("-1")))
        stranger = _svc(db, stranger_actor)
        with pytest.raises(PermissionDeniedError):
            await stranger.set_budget("studio", agent_id, limits=BudgetLimits())
        with pytest.raises(NotFoundError):
            await svc.get_budget("studio", uuid.uuid4())
        return True

    assert client.portal.call(lambda: _tx(run))


def test_clear_budget_restores_defaults(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        await svc.set_budget("studio", agent_id, limits=BudgetLimits(
            monthly_spend_limit_usd=Decimal("1")))
        await svc.clear_budget("studio", agent_id)
        assert (await svc.get_budget("studio", agent_id))["explicit"] is False
        return True

    assert client.portal.call(lambda: _tx(run))


def test_org_quota_round_trip(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        quota = await svc.get_org_quota()
        assert quota["explicit"] is False and quota["max_agents"] is None
        saved = await svc.set_org_quota(max_agents=5, monthly_spend_limit_usd=Decimal("50"))
        assert saved["max_agents"] == 5
        assert saved["monthly_spend_limit_usd"] == Decimal("50")
        with pytest.raises(InvalidInputError):
            await svc.set_org_quota(max_agents=-1, monthly_spend_limit_usd=None)
        return True

    assert client.portal.call(lambda: _tx(run))


# -- start decisions ------------------------------------------------------------

def test_check_start_allowed_with_no_usage(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        decision = await svc.check_start("studio", agent_id)
        assert decision.allowed is True and decision.reason == ""
        return True

    assert client.portal.call(lambda: _tx(run))


def test_check_start_denies_when_monthly_spend_exceeded(admin_client, admin_actor):
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    owner_id = admin_client.portal.call(
        lambda: _tx(lambda db: _user_id(db, ADMIN_EMAIL), commit=False))

    async def spend(db):
        await _insert_run(db, uuid.UUID(agent_id), uuid.UUID(thread_id), owner_id,
                          cost=Decimal("10.00"))
        return True

    assert admin_client.portal.call(lambda: _tx(spend))

    async def run(db):
        decision = await _svc(db, admin_actor).check_start("studio", uuid.UUID(agent_id))
        assert decision.allowed is False
        assert decision.reason == "monthly_spend_exceeded"
        return True

    assert admin_client.portal.call(lambda: _tx(run))


def test_check_start_denies_when_call_limit_exceeded(admin_client, admin_actor):
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    owner_id = admin_client.portal.call(
        lambda: _tx(lambda db: _user_id(db, ADMIN_EMAIL), commit=False))

    async def setup(db):
        svc = _svc(db, admin_actor)
        await svc.set_budget("studio", uuid.UUID(agent_id), limits=BudgetLimits(
            monthly_spend_limit_usd=None, monthly_call_limit=1))
        await _insert_run(db, uuid.UUID(agent_id), uuid.UUID(thread_id), owner_id,
                          cost=Decimal("0.01"), status="completed")
        return True

    assert admin_client.portal.call(lambda: _tx(setup))

    async def run(db):
        decision = await _svc(db, admin_actor).check_start("studio", uuid.UUID(agent_id))
        assert decision.allowed is False
        assert decision.reason == "monthly_call_limit_exceeded"
        return True

    assert admin_client.portal.call(lambda: _tx(run))


def test_check_start_denies_for_paused_agent(client, admin_actor, owner_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        await _reg(db, owner_actor).pause_agent("studio", agent_id, reason="maintenance")
        decision = await svc.check_start("studio", agent_id)
        assert decision.allowed is False
        assert decision.reason == "not_active:paused"
        return True

    assert client.portal.call(lambda: _tx(run))


def test_org_quota_blocks_all_starts(admin_client, admin_actor):
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    owner_id = admin_client.portal.call(
        lambda: _tx(lambda db: _user_id(db, ADMIN_EMAIL), commit=False))

    async def setup(db):
        svc = _svc(db, admin_actor)
        await svc.set_org_quota(max_agents=None, monthly_spend_limit_usd=Decimal("0.01"))
        await _insert_run(db, uuid.UUID(agent_id), uuid.UUID(thread_id), owner_id,
                          cost=Decimal("0.02"))
        return True

    assert admin_client.portal.call(lambda: _tx(setup))

    async def run(db):
        svc = _svc(db, admin_actor)
        other = await _make_studio_agent(db, USER_EMAIL, name="Other agent")
        decision = await svc.check_start("studio", other)
        assert decision.allowed is False
        assert decision.reason == "org_monthly_spend_exceeded"
        await svc.set_org_quota(max_agents=0, monthly_spend_limit_usd=None)
        decision = await svc.check_start("studio", other)
        assert decision.reason == "org_agent_quota_exceeded"
        return True

    assert admin_client.portal.call(lambda: _tx(run))


# -- Studio wiring ---------------------------------------------------------------

def test_start_run_refuses_when_budget_exceeded(admin_client, admin_actor):
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    owner_id = admin_client.portal.call(
        lambda: _tx(lambda db: _user_id(db, ADMIN_EMAIL), commit=False))

    async def setup(db):
        svc = _svc(db, admin_actor)
        await svc.set_budget("studio", uuid.UUID(agent_id), limits=BudgetLimits(
            monthly_spend_limit_usd=Decimal("0.01")))
        await _insert_run(db, uuid.UUID(agent_id), uuid.UUID(thread_id), owner_id,
                          cost=Decimal("0.02"))
        return True

    assert admin_client.portal.call(lambda: _tx(setup))

    async def run(db):
        user = await db.get(User, owner_id)
        service = AgentStudioService(db, user, get_settings(), app.state.cipher,
                                     app.state.llm_runtime, app.state.rag)
        with pytest.raises(InvalidInputError, match="budget stop"):
            await service.start_run(uuid.UUID(agent_id), uuid.UUID(thread_id),
                                    "Do more work", execute_inline=True)
        return True

    assert admin_client.portal.call(lambda: _tx(run))


def test_registry_paused_agent_run_stays_queued(admin_client, admin_actor, monkeypatch):
    executed = _stub_execute(monkeypatch)
    agent_id, thread_id = _create_agent_and_thread(admin_client)

    async def pause(db):
        await _reg(db, admin_actor).pause_agent(
            "studio", uuid.UUID(agent_id), reason="workforce pause")
        return True

    assert admin_client.portal.call(lambda: _tx(pause))
    run_id, status, answer = _start_inline(
        admin_client, agent_id, thread_id, "Work while registry-paused", "j176-paused")
    assert (status, answer) == ("queued", None)
    assert executed == []


def test_worker_claim_skips_registry_paused_agent(admin_client, admin_actor):
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    queued = _enqueue(admin_client, agent_id, thread_id, "Queued work", "j176-claim")
    assert queued.status_code == 202, queued.text

    async def pause(db):
        await _reg(db, admin_actor).pause_agent(
            "studio", uuid.UUID(agent_id), reason="workforce pause")
        return True

    assert admin_client.portal.call(lambda: _tx(pause))
    assert admin_client.portal.call(process_next_agent_studio_run, *_process_args()) is False


def test_execute_claimed_hands_back_on_budget(admin_client, admin_actor, monkeypatch):
    executed = _stub_execute(monkeypatch)
    agent_id, thread_id = _create_agent_and_thread(admin_client)
    owner_id = admin_client.portal.call(
        lambda: _tx(lambda db: _user_id(db, ADMIN_EMAIL), commit=False))

    # The run must be created WHILE the agent is under budget: start_run
    # refuses over-budget creation outright (422), so the hand-back path
    # is only reachable when the budget is exceeded after creation.
    queued = _enqueue(admin_client, agent_id, thread_id, "Work over budget", "j176-handback")
    assert queued.status_code == 202, queued.text
    run_id = uuid.UUID(queued.json()["id"])

    async def setup(db):
        svc = _svc(db, admin_actor)
        await svc.set_budget("studio", uuid.UUID(agent_id), limits=BudgetLimits(
            monthly_spend_limit_usd=Decimal("0.01")))
        await _insert_run(db, uuid.UUID(agent_id), uuid.UUID(thread_id), owner_id,
                          cost=Decimal("0.02"))
        return True

    assert admin_client.portal.call(lambda: _tx(setup))
    assert admin_client.portal.call(process_next_agent_studio_run, *_process_args()) is True
    assert executed == []
    status, attempts, next_attempt_at, claimed_at = _run_row(admin_client, run_id)
    assert status == "queued" and attempts == 0 and claimed_at is None
    assert next_attempt_at is not None
    assert (next_attempt_at - datetime.now(UTC)).total_seconds() > 1800


# -- revocation hook ---------------------------------------------------------------

def test_revoke_user_access_pauses_agents_and_freezes_skills(admin_client):
    admin_client.portal.call(
        lambda: _tx(lambda db: _grant(
            db, "j176-test-revoke", {"identity.revoke", "workforce.manage",
                                     "workforce.read"})))

    async def setup(db):
        owner_id = await _user_id(db, USER_EMAIL)
        agent = AgentStudioAgent(owner_id=owner_id, name="Revoked owner agent",
                                 instructions="Do the task.",
                                 tool_permissions=["web_search"], approval_tools=[],
                                 delegate_agent_ids=[])
        db.add(agent)
        await db.flush()
        admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
        actor = platform_access.principal_for(admin)
        ctx = AuditContext(actor_user_id=admin.id, actor_email=admin.email,
                           ip_address=None, request_id=None)
        skills = SkillRegistryService(db, actor, ctx)
        skill = await skills.create_skill("invoice triage")
        await skills.add_skill("studio", agent.id, skill.id)
        await skills.verify_skill("studio", agent.id, skill.id)
        return agent.id, owner_id

    agent_id, owner_id = admin_client.portal.call(lambda: _tx(setup))

    async def revoke(db):
        admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
        actor = platform_access.principal_for(admin)
        counts = await platform_access.revoke_user_access(
            db, actor, owner_id, "test revocation")
        return counts

    counts = admin_client.portal.call(lambda: _tx(revoke))
    assert counts["workforce_agents_paused"] == 1
    assert counts["workforce_skills_frozen"] == 1

    async def check(db):
        admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
        actor = platform_access.principal_for(admin)
        ctx = AuditContext(actor_user_id=admin.id, actor_email=admin.email,
                           ip_address=None, request_id=None)
        view = await WorkforceRegistryService(db, actor, ctx).get_agent("studio", agent_id)
        assert view.effective_status == "paused"
        skills = await SkillRegistryService(db, actor, ctx).agent_skills(
            "studio", agent_id)
        assert skills[0]["status"] == "expired"
        return True

    assert admin_client.portal.call(lambda: _tx(check))


# -- API routes --------------------------------------------------------------------

def test_budget_api_round_trip(admin_client, granted):
    created = admin_client.post("/api/workforce/delegation-policies", json={
        "rules": [{"from_kind": "studio", "to_kind": "studio"}],
        "max_depth": 2})
    assert created.status_code == 200, created.text
    agent_id, _thread_id = _create_agent_and_thread(admin_client)
    got = admin_client.get(f"/api/workforce/agents/studio/{agent_id}/budget")
    assert got.status_code == 200, got.text
    assert got.json()["monthly_spend_limit_usd"] == 10.0
    assert got.json()["explicit"] is False
    put = admin_client.put(f"/api/workforce/agents/studio/{agent_id}/budget", json={
        "monthly_spend_limit_usd": 5.0, "daily_call_limit": 20})
    assert put.status_code == 200, put.text
    assert put.json()["monthly_spend_limit_usd"] == 5.0
    assert put.json()["daily_call_limit"] == 20
    check = admin_client.get(f"/api/workforce/agents/studio/{agent_id}/budget/check")
    assert check.status_code == 200 and check.json() == {"allowed": True, "reason": ""}
    quota = admin_client.put("/api/workforce/org-quota", json={"max_agents": 50})
    assert quota.status_code == 200 and quota.json()["max_agents"] == 50
    cleared = admin_client.delete(f"/api/workforce/agents/studio/{agent_id}/budget")
    assert cleared.status_code == 204
    got = admin_client.get(f"/api/workforce/agents/studio/{agent_id}/budget")
    assert got.json()["explicit"] is False
