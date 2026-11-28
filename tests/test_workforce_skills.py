"""Step J (AI Workforce OS), slice 2: skill-registry service + migration (DB tests).

All tests run on the dedicated ``infraspine_j121_test`` database only (see
``/home/hatch/.agent121-test-env.sh``, never committed). Run:
``source /home/hatch/.agent121-test-env.sh && python -m pytest tests/test_workforce_skills.py -q``
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.agent_studio_models import AgentStudioAgent
from app.db.models import AccessGroup, AgentProfile, DepartmentManager, User
from app.db.workforce_skill_models import WorkforceAgentSkill, WorkforceSkill
from app.governance.models import AgentIdentity, Department, Member, Tenant
from app.main import app
from app.platform import access as platform_access
from app.services.audit import AuditContext
from app.workforce.assignment import WorkforceAgent, WorkTask, assign_task
from app.workforce.skill_service import SkillRegistryService
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
    """Give the admin workforce.manage + workforce.read on the org scope."""
    admin_id = await _user_id(db, ADMIN_EMAIL)
    admin = await db.get(User, admin_id)
    actor = platform_access.principal_for(admin)
    await platform_access.ensure_role(
        db, "j121-test-workforce", {"workforce.manage", "workforce.read"},
        name="J121 test workforce")
    org = await platform_access.ensure_org_scope(db)
    await platform_access.grant_membership(
        db, actor, user_id=admin_id, scope_id=org.id, role_key="j121-test-workforce")
    return admin_id


@pytest.fixture
def granted(client, clean_tables):
    """Grant the admin workforce permissions; returns the admin user id."""
    return client.portal.call(lambda: _tx(_grant_manage))


def _actor_for(client, email):
    """(Principal, AuditContext) for ``email`` — plain data, safe to hold
    across sessions; the service itself is built inside each test's session."""
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
def manager_actor(client, clean_tables):
    """USER2 promoted to the ``manager`` role (the ``department_managers``
    table refuses a manager without that role) — returns their actor."""

    async def _promote(db):
        await db.execute(
            update(User).where(User.email == USER2_EMAIL).values(role="manager")
        )

    client.portal.call(lambda: _tx(_promote))
    return _actor_for(client, USER2_EMAIL)


@pytest.fixture
def owner_actor(client, clean_tables):
    return _actor_for(client, USER_EMAIL)


@pytest.fixture
def stranger_actor(client, clean_tables):
    return _actor_for(client, USER2_EMAIL)


def _svc(db, actor_ctx):
    actor, ctx = actor_ctx
    return SkillRegistryService(db, actor, ctx)


async def _make_studio_agent(db, owner_email):
    owner_id = await _user_id(db, owner_email)
    agent = AgentStudioAgent(owner_id=owner_id, name="Triage agent",
                             instructions="Triage invoices.",
                             tool_permissions=["web_search"], approval_tools=[],
                             delegate_agent_ids=[])
    db.add(agent)
    await db.flush()
    return agent.id


async def _make_governance_agent(db, owner_email):
    owner_id = await _user_id(db, owner_email)
    tenant = Tenant(name="J121 tenant")
    db.add(tenant)
    await db.flush()
    dept = Department(tenant_id=tenant.id, name="J121 dept")
    db.add(dept)
    await db.flush()
    db.add(Member(tenant_id=tenant.id, user_id=owner_id, department_id=dept.id))
    await db.flush()
    identity = AgentIdentity(tenant_id=tenant.id, owner_user_id=owner_id,
                             name="Gov agent", tools=["knowledge_search"])
    db.add(identity)
    await db.flush()
    return identity.id


async def _make_profile_agent(db, manager_email):
    manager_id = await _user_id(db, manager_email)
    group = AccessGroup(name="J121 dept", kind="department", source="local",
                        created_by=manager_id)
    db.add(group)
    await db.flush()
    db.add(DepartmentManager(group_id=group.id, manager_user_id=manager_id,
                             assigned_by=manager_id))
    agent_key = uuid.uuid4()
    profile = AgentProfile(agent_key=agent_key, version=1, name="Profile agent",
                           agent_type="knowledge_research",
                           department_group_id=group.id,
                           knowledge_scope="department_only",
                           tool_policy=["knowledge_search"], enabled=True,
                           created_by_user_id=manager_id)
    db.add(profile)
    await db.flush()
    return agent_key


# -- catalog -------------------------------------------------------------------

def test_create_skill_round_trips_and_normalizes(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        row = await svc.create_skill("  Invoice   Triage ", description="Triage invoices.")
        assert row.name == "invoice triage"
        found = await db.get(WorkforceSkill, row.id)
        assert found is not None and found.name == "invoice triage"
        listed = await svc.list_skills()
        assert [item.name for item in listed] == ["invoice triage"]
        return True

    assert client.portal.call(lambda: _tx(run))


def test_create_duplicate_skill_conflicts(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        await svc.create_skill("Invoice Triage")
        with pytest.raises(ConflictError):
            await svc.create_skill("invoice triage")
        return True

    assert client.portal.call(lambda: _tx(run))


def test_create_skill_rejects_bad_names(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        for bad in ("", "x", "s" * 61):
            with pytest.raises(InvalidInputError):
                await svc.create_skill(bad)
        return True

    assert client.portal.call(lambda: _tx(run))


def test_create_skill_needs_workforce_manage(client, owner_actor):
    """The catalog is org-wide: an agent owner without workforce.manage gets 403."""
    async def run(db):
        svc = _svc(db, owner_actor)
        with pytest.raises(PermissionDeniedError):
            await svc.create_skill("invoice triage")
        return True

    assert client.portal.call(lambda: _tx(run))


def test_list_skills_needs_read_permission(client, stranger_actor):
    async def run(db):
        svc = _svc(db, stranger_actor)
        with pytest.raises(PermissionDeniedError):
            await svc.list_skills()
        return True

    assert client.portal.call(lambda: _tx(run))


# -- attach / verify / revoke / remove ------------------------------------------

def test_owner_can_verify_own_studio_agent_skill(client, owner_actor, admin_actor):
    """Plan §7.3: the accountable owner verifies without workforce.manage."""
    async def run(db):
        admin = _svc(db, admin_actor)
        owner = _svc(db, owner_actor)
        skill = await admin.create_skill("invoice triage")
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        row = await owner.add_skill("studio", agent_id, skill.id)
        assert row.verified_at is None
        verified = await owner.verify_skill("studio", agent_id, skill.id)
        assert verified.verified_by_user_id is not None
        assert verified.verified_at is not None
        assert verified.expires_at is not None
        assert verified.expires_at > verified.verified_at
        assert await owner.current_skills("studio", agent_id) == ["invoice triage"]
        return True

    assert client.portal.call(lambda: _tx(run))


def test_admin_with_manage_can_verify_any_agent(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        skill = await svc.create_skill("invoice triage")
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        await svc.add_skill("studio", agent_id, skill.id)
        verified = await svc.verify_skill("studio", agent_id, skill.id)
        assert verified.verified_at is not None
        return True

    assert client.portal.call(lambda: _tx(run))


def test_stranger_cannot_verify_or_read_agent_skills(client, stranger_actor, admin_actor):
    async def run(db):
        admin = _svc(db, admin_actor)
        stranger = _svc(db, stranger_actor)
        skill = await admin.create_skill("invoice triage")
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        await admin.add_skill("studio", agent_id, skill.id)
        with pytest.raises(PermissionDeniedError):
            await stranger.verify_skill("studio", agent_id, skill.id)
        with pytest.raises(PermissionDeniedError):
            await stranger.agent_skills("studio", agent_id)
        with pytest.raises(PermissionDeniedError):
            await stranger.current_skills("studio", agent_id)
        return True

    assert client.portal.call(lambda: _tx(run))


def test_verify_unknown_agent_is_404(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        skill = await svc.create_skill("invoice triage")
        with pytest.raises(NotFoundError):
            await svc.verify_skill("studio", uuid.uuid4(), skill.id)
        with pytest.raises(NotFoundError):
            await svc.add_skill("studio", uuid.uuid4(), skill.id)
        # An unknown agent kind is invalid input (422-class), not "not found":
        # the caller asked for a kind that does not exist at all.
        with pytest.raises(InvalidInputError):
            await svc.verify_skill("human", uuid.uuid4(), skill.id)
        return True

    assert client.portal.call(lambda: _tx(run))


def test_verify_unattached_skill_is_404(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        skill = await svc.create_skill("invoice triage")
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        with pytest.raises(NotFoundError):
            await svc.verify_skill("studio", agent_id, skill.id)
        return True

    assert client.portal.call(lambda: _tx(run))


def test_add_skill_is_idempotent(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        skill = await svc.create_skill("invoice triage")
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        first = await svc.add_skill("studio", agent_id, skill.id)
        second = await svc.add_skill("studio", agent_id, skill.id)
        assert first.id == second.id
        rows = await db.scalars(select(WorkforceAgentSkill).where(
            WorkforceAgentSkill.agent_id == agent_id))
        assert len(list(rows)) == 1
        return True

    assert client.portal.call(lambda: _tx(run))


def test_expired_skill_is_not_current(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        skill = await svc.create_skill("invoice triage")
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        await svc.add_skill("studio", agent_id, skill.id)
        verified = await svc.verify_skill("studio", agent_id, skill.id)
        assert await svc.current_skills("studio", agent_id) == ["invoice triage"]
        # Backdate the verification past its expiry: the skill stops counting.
        verified.verified_at = datetime.now(UTC) - timedelta(days=400)
        verified.expires_at = datetime.now(UTC) - timedelta(days=35)
        await db.flush()
        assert await svc.current_skills("studio", agent_id) == []
        listed = await svc.agent_skills("studio", agent_id)
        assert listed[0]["status"] == "expired"
        return True

    assert client.portal.call(lambda: _tx(run))


def test_revoke_clears_verification_but_keeps_row(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        skill = await svc.create_skill("invoice triage")
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        await svc.add_skill("studio", agent_id, skill.id)
        await svc.verify_skill("studio", agent_id, skill.id)
        revoked = await svc.revoke_skill("studio", agent_id, skill.id)
        assert revoked.verified_at is None and revoked.verified_by_user_id is None
        assert revoked.expires_at is None
        assert await svc.current_skills("studio", agent_id) == []
        listed = await svc.agent_skills("studio", agent_id)
        assert listed[0]["status"] == "unverified"
        return True

    assert client.portal.call(lambda: _tx(run))


def test_remove_detaches_skill(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        skill = await svc.create_skill("invoice triage")
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        await svc.add_skill("studio", agent_id, skill.id)
        await svc.remove_skill("studio", agent_id, skill.id)
        assert await svc.agent_skills("studio", agent_id) == []
        with pytest.raises(NotFoundError):
            await svc.verify_skill("studio", agent_id, skill.id)
        return True

    assert client.portal.call(lambda: _tx(run))


def test_verify_with_past_expiry_rejected(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        skill = await svc.create_skill("invoice triage")
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        await svc.add_skill("studio", agent_id, skill.id)
        with pytest.raises(InvalidInputError):
            await svc.verify_skill("studio", agent_id, skill.id,
                                  expires_at=datetime.now(UTC) - timedelta(days=1))
        return True

    assert client.portal.call(lambda: _tx(run))


def test_governance_owner_can_verify(client, owner_actor, admin_actor):
    async def run(db):
        admin = _svc(db, admin_actor)
        owner = _svc(db, owner_actor)
        skill = await admin.create_skill("market research")
        agent_id = await _make_governance_agent(db, USER_EMAIL)
        await owner.add_skill("governance", agent_id, skill.id)
        verified = await owner.verify_skill("governance", agent_id, skill.id)
        assert verified.verified_at is not None
        assert await owner.current_skills("governance", agent_id) == ["market research"]
        return True

    assert client.portal.call(lambda: _tx(run))


def test_department_manager_can_verify_profile_agent(client, manager_actor, admin_actor):
    """Legacy profiles are department-scoped: the department manager is the
    accountable owner for verification (the manager must hold the ``manager``
    role — see the ``manager_actor`` fixture)."""
    async def run(db):
        admin = _svc(db, admin_actor)
        manager = _svc(db, manager_actor)
        skill = await admin.create_skill("training plan")
        agent_key = await _make_profile_agent(db, USER2_EMAIL)
        await manager.add_skill("profile", agent_key, skill.id)
        verified = await manager.verify_skill("profile", agent_key, skill.id)
        assert verified.verified_at is not None
        assert await manager.current_skills("profile", agent_key) == ["training plan"]
        return True

    assert client.portal.call(lambda: _tx(run))


def test_current_skills_feed_slice1_assignment(client, admin_actor):
    """Verified skills match; unverified and expired skills never do."""
    async def run(db):
        svc = _svc(db, admin_actor)
        triage = await svc.create_skill("invoice triage")
        research = await svc.create_skill("market research")
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        await svc.add_skill("studio", agent_id, triage.id)
        await svc.add_skill("studio", agent_id, research.id)
        await svc.verify_skill("studio", agent_id, triage.id)
        current = await svc.current_skills("studio", agent_id)
        assert current == ["invoice triage"]
        agent = WorkforceAgent(agent_id=agent_id, owner_id=uuid.uuid4(), skills=frozenset(current))
        task = WorkTask(task_id="t1", required_skills=frozenset({"invoice triage"}))
        assert assign_task([agent], task).status == "assigned"
        task2 = WorkTask(task_id="t2", required_skills=frozenset({"market research"}))
        assert assign_task([agent], task2).status == "refused"
        return True

    assert client.portal.call(lambda: _tx(run))


def test_skill_never_grants_permission(client, admin_actor):
    """A verified skill is a fact, not a permission: the agent's platform
    permissions are unchanged by verification."""
    async def run(db):
        svc = _svc(db, admin_actor)
        skill = await svc.create_skill("invoice triage")
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        await svc.add_skill("studio", agent_id, skill.id)
        await svc.verify_skill("studio", agent_id, skill.id)
        agent = await db.get(AgentStudioAgent, agent_id)
        assert agent.tool_permissions == ["web_search"]
        return True

    assert client.portal.call(lambda: _tx(run))
