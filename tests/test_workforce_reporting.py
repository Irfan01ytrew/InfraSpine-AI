"""Step J (AI Workforce OS), slice 6: workforce reporting (DB tests).

All tests run on the dedicated ``infraspine_j121_test`` database only (see
``/home/hatch/.agent121-test-env.sh``, never committed). Run:
``source /home/hatch/.agent121-test-env.sh && python -m pytest tests/test_workforce_reporting.py -q``
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.agent_studio.service import _encode_state
from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.agent_studio_models import AgentStudioAgent, AgentStudioRun
from app.db.models import User
from app.governance.models import AgentIdentity, Department, Member, Tenant
from app.main import app
from app.platform import access as platform_access
from app.services.audit import AuditContext
from app.workforce.reporting import (
    WorkforceReportingService,
    avg_duration_ms,
    cost_per_task,
    success_rate,
)
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL


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


async def _grant_manage(db):
    admin_id = await _user_id(db, ADMIN_EMAIL)
    admin = await db.get(User, admin_id)
    actor = platform_access.principal_for(admin)
    await platform_access.ensure_role(
        db, "j176-test-workforce", {"workforce.manage", "workforce.read"},
        name="J176 test workforce")
    org = await platform_access.ensure_org_scope(db)
    await platform_access.grant_membership(
        db, actor, user_id=admin_id, scope_id=org.id, role_key="j176-test-workforce")
    return admin_id


@pytest.fixture
def granted(client, clean_tables):
    """Grant the admin workforce permissions; returns the admin user id."""
    return client.portal.call(lambda: _tx(_grant_manage))


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
def stranger_actor(client, clean_tables):
    return _actor_for(client, USER2_EMAIL)


def _svc(db, actor_ctx):
    actor, ctx = actor_ctx
    return WorkforceReportingService(db, actor, ctx)


async def _make_studio_agent(db, owner_email, name="Report agent"):
    owner_id = await _user_id(db, owner_email)
    agent = AgentStudioAgent(owner_id=owner_id, name=name,
                             instructions="Do the task.",
                             tool_permissions=["web_search"], approval_tools=[],
                             delegate_agent_ids=[])
    db.add(agent)
    await db.flush()
    return agent.id


async def _make_governance_agent(db, owner_email):
    owner_id = await _user_id(db, owner_email)
    tenant = Tenant(name="J176 tenant")
    db.add(tenant)
    await db.flush()
    dept = Department(tenant_id=tenant.id, name="J176 dept")
    db.add(dept)
    await db.flush()
    db.add(Member(tenant_id=tenant.id, user_id=owner_id, department_id=dept.id))
    await db.flush()
    identity = AgentIdentity(tenant_id=tenant.id, owner_user_id=owner_id,
                             name="Gov agent", tools=["knowledge_search"])
    db.add(identity)
    await db.flush()
    return identity.id


async def _insert_run(db, agent_id, thread_id, owner_id, *, cost=None, status="completed",
                      duration_ms=None, prompt_tokens=0, completion_tokens=0,
                      llm_calls=0, created_days_ago=0):
    run_id = uuid.uuid4()
    sealed = _encode_state(app.state.cipher, run_id, {})
    now = datetime.now(UTC)
    created_at = now - timedelta(days=created_days_ago)
    started_at = finished_at = None
    if duration_ms is not None:
        started_at = created_at
        finished_at = created_at + timedelta(milliseconds=duration_ms)
    run = AgentStudioRun(
        id=run_id, agent_id=agent_id, thread_id=thread_id, owner_id=owner_id,
        status=status, state_ciphertext=sealed.ciphertext, state_nonce=sealed.nonce,
        state_key_id=sealed.key_id, estimated_cost_usd=cost,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        llm_calls=llm_calls, started_at=started_at, finished_at=finished_at,
        created_at=created_at)
    db.add(run)
    await db.flush()
    return run_id


async def _make_thread(db, agent_id, owner_id):
    from app.agent_studio.service import _encode_thread
    from app.db.agent_studio_models import AgentStudioThread

    thread_id = uuid.uuid4()
    sealed = _encode_thread(app.state.cipher, thread_id, [])
    thread = AgentStudioThread(
        id=thread_id, agent_id=agent_id, owner_id=owner_id, title="Report thread",
        history_ciphertext=sealed.ciphertext, history_nonce=sealed.nonce,
        history_key_id=sealed.key_id)
    db.add(thread)
    await db.flush()
    return thread_id


# -- pure helpers ---------------------------------------------------------------

def test_reporting_helpers():
    assert success_rate(3, 1) == 0.75
    assert success_rate(0, 0) is None
    assert avg_duration_ms([100.0, 300.0]) == 200.0
    assert avg_duration_ms([]) is None
    assert cost_per_task(Decimal("10"), 4) == Decimal("2.50000000")
    assert cost_per_task(Decimal("10"), 0) is None


# -- agent report -----------------------------------------------------------------

def test_agent_report_aggregates_runs(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        owner_id = await _user_id(db, USER_EMAIL)
        thread_id = await _make_thread(db, agent_id, owner_id)
        await _insert_run(db, agent_id, thread_id, owner_id, cost=Decimal("2.00"),
                          status="completed", duration_ms=1000,
                          prompt_tokens=100, completion_tokens=50, llm_calls=2)
        await _insert_run(db, agent_id, thread_id, owner_id, cost=Decimal("3.00"),
                          status="completed", duration_ms=3000,
                          prompt_tokens=200, completion_tokens=100, llm_calls=3)
        await _insert_run(db, agent_id, thread_id, owner_id, status="failed",
                          duration_ms=500)
        await _insert_run(db, agent_id, thread_id, owner_id, status="queued")
        await _insert_run(db, agent_id, thread_id, owner_id, cost=Decimal("9.00"),
                          status="completed", duration_ms=1000, created_days_ago=60)
        report = await svc.agent_report("studio", agent_id, days=30)
        assert report.runs == 4  # the 60-day-old run is outside the window
        assert report.completed == 2 and report.failed == 1
        assert report.runs_by_status["queued"] == 1
        assert report.success_rate == 0.6667
        assert report.avg_duration_ms == 1500.0
        assert report.total_cost_usd == Decimal("5.00")
        assert report.total_prompt_tokens == 300
        assert report.total_completion_tokens == 150
        assert report.total_llm_calls == 5
        assert report.cost_per_task_usd == Decimal("1.25000000")
        assert report.can_start is True
        assert report.monthly_spend_limit_usd == Decimal("10")
        return True

    assert client.portal.call(lambda: _tx(run))


def test_agent_report_for_governance_agent_shows_zero_with_note(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_id = await _make_governance_agent(db, USER_EMAIL)
        report = await svc.agent_report("governance", agent_id)
        assert report.runs == 0
        assert report.total_cost_usd == Decimal("0")
        assert report.success_rate is None
        assert report.avg_duration_ms is None
        assert "No run rows" in report.usage_note
        return True

    assert client.portal.call(lambda: _tx(run))


def test_agent_report_gates_and_validation(client, admin_actor, stranger_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        stranger = _svc(db, stranger_actor)
        with pytest.raises(PermissionDeniedError):
            await stranger.agent_report("studio", agent_id)
        with pytest.raises(NotFoundError):
            await svc.agent_report("studio", uuid.uuid4())
        with pytest.raises(InvalidInputError):
            await svc.agent_report("studio", agent_id, days=0)
        with pytest.raises(InvalidInputError):
            await svc.agent_report("human", agent_id)
        return True

    assert client.portal.call(lambda: _tx(run))


# -- org + spend reports ------------------------------------------------------------

def test_org_report_totals_and_status_counts(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_a = await _make_studio_agent(db, USER_EMAIL, name="Agent A")
        agent_b = await _make_studio_agent(db, USER_EMAIL, name="Agent B")
        owner_id = await _user_id(db, USER_EMAIL)
        thread_a = await _make_thread(db, agent_a, owner_id)
        await _insert_run(db, agent_a, thread_a, owner_id, cost=Decimal("1.50"),
                          status="completed", duration_ms=1000)
        await _insert_run(db, agent_a, thread_a, owner_id, status="failed")
        report = await svc.org_report(days=30)
        assert report.total_agents == 2
        assert report.total_runs == 2
        assert report.total_completed == 1 and report.total_failed == 1
        assert report.success_rate == 0.5
        assert report.total_cost_usd == Decimal("1.50")
        assert report.agents_by_status == {"active": 2}
        names = {item.display_name for item in report.agents}
        assert names == {"Agent A", "Agent B"}
        by_name = {item.display_name: item for item in report.agents}
        assert by_name["Agent A"].total_cost_usd == Decimal("1.50")
        assert by_name["Agent B"].total_cost_usd == Decimal("0")
        spend = await svc.spend_report(days=30)
        assert spend.total_cost_usd == Decimal("1.50")
        assert spend.total_runs == 2
        assert spend.cost_per_task_usd == Decimal("0.75000000")
        assert len(spend.by_agent) == 2
        return True

    assert client.portal.call(lambda: _tx(run))


def test_org_report_api(admin_client, granted):
    response = admin_client.get("/api/workforce/report/org?days=30")
    assert response.status_code == 200, response.text
    assert response.json()["total_agents"] == 0
    spend = admin_client.get("/api/workforce/report/spend")
    assert spend.status_code == 200 and spend.json()["total_runs"] == 0
