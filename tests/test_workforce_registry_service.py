"""Step J (AI Workforce OS), slice 3: org registry + lifecycle (DB tests).

All tests run on the dedicated ``infraspine_j121_test`` database only (see
``/home/hatch/.agent121-test-env.sh``, never committed). Run:
``source /home/hatch/.agent121-test-env.sh && python -m pytest tests/test_workforce_registry_service.py -q``
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.agent_studio_models import AgentStudioAgent
from app.db.models import AccessGroup, AgentProfile, DepartmentManager, User
from app.db.workforce_registry_models import (
    WorkforceLifecycleEvent,
    WorkforceRegistryAgent,
)
from app.db.workforce_skill_models import WorkforceAgentSkill, WorkforceSkill
from app.governance.models import AgentIdentity, Department, Member, Tenant
from app.main import app
from app.platform import access as platform_access
from app.services.audit import AuditContext
from app.workforce.registry_service import WorkforceRegistryService
from app.workforce.skill_service import SkillRegistryService
from app.workforce.skills import skill_status
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
        db, "j159-test-workforce", {"workforce.manage", "workforce.read"},
        name="J159 test workforce")
    org = await platform_access.ensure_org_scope(db)
    await platform_access.grant_membership(
        db, actor, user_id=admin_id, scope_id=org.id, role_key="j159-test-workforce")
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
def owner_actor(client, clean_tables):
    return _actor_for(client, USER_EMAIL)


@pytest.fixture
def stranger_actor(client, clean_tables):
    return _actor_for(client, USER2_EMAIL)


def _svc(db, actor_ctx):
    actor, ctx = actor_ctx
    return WorkforceRegistryService(db, actor, ctx)


async def _make_studio_agent(db, owner_email, name="Triage agent"):
    owner_id = await _user_id(db, owner_email)
    agent = AgentStudioAgent(owner_id=owner_id, name=name,
                             instructions="Triage invoices.",
                             tool_permissions=["web_search"], approval_tools=[],
                             delegate_agent_ids=[])
    db.add(agent)
    await db.flush()
    return agent.id


async def _make_governance_agent(db, owner_email):
    owner_id = await _user_id(db, owner_email)
    tenant = Tenant(name="J159 tenant")
    db.add(tenant)
    await db.flush()
    dept = Department(tenant_id=tenant.id, name="J159 dept")
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
    # The department_managers table refuses a manager without the
    # ``manager`` role (same fix Agent 121 made for slice 2).
    await db.execute(
        update(User).where(User.email == manager_email).values(role="manager"))
    group = AccessGroup(name="J159 dept", kind="department", source="local",
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


async def _revoke_user(db, email):
    """Mirror revoke_user_access's account effect: the user can no longer sign in."""
    await db.execute(update(User).where(User.email == email).values(is_active=False))


async def _verified_skill(db, admin_actor, agent_kind, agent_id, name="invoice triage"):
    """Attach + verify a skill for an agent; returns the skill id."""
    actor, ctx = admin_actor
    svc = SkillRegistryService(db, actor, ctx)
    skill = await svc.create_skill(name)
    await svc.add_skill(agent_kind, agent_id, skill.id)
    await svc.verify_skill(agent_kind, agent_id, skill.id)
    return skill.id


# -- registry reads ------------------------------------------------------------

def test_list_agents_shows_all_three_islands(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        studio_id = await _make_studio_agent(db, USER_EMAIL)
        gov_id = await _make_governance_agent(db, USER_EMAIL)
        profile_key = await _make_profile_agent(db, USER_EMAIL)
        views = await svc.list_agents()
        by_kind = {view.kind: view for view in views}
        assert set(by_kind) == {"studio", "governance", "profile"}
        assert by_kind["studio"].agent_id == studio_id
        assert by_kind["governance"].agent_id == gov_id
        assert by_kind["profile"].agent_id == profile_key
        for view in views:
            assert view.effective_status == "active"
            assert view.registry_state == "active"
            assert view.current_skills == ()
            # AK control records are not on this branch; the view says so.
            assert any("AK control record" in note for note in view.notes)
        assert by_kind["studio"].display_name == "Triage agent"
        assert by_kind["profile"].manager_group_id is not None
        assert by_kind["profile"].owner_id is None
        return True

    assert client.portal.call(lambda: _tx(run))


def test_list_agents_needs_read_permission(client, stranger_actor):
    async def run(db):
        svc = _svc(db, stranger_actor)
        with pytest.raises(PermissionDeniedError):
            await svc.list_agents()
        return True

    assert client.portal.call(lambda: _tx(run))


def test_get_agent_unknown_is_404(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        with pytest.raises(NotFoundError):
            await svc.get_agent("studio", uuid.uuid4())
        with pytest.raises(InvalidInputError):
            await svc.get_agent("human", uuid.uuid4())
        return True

    assert client.portal.call(lambda: _tx(run))


def test_owner_can_read_own_agent_but_stranger_cannot(client, owner_actor, stranger_actor):
    async def run(db):
        owner = _svc(db, owner_actor)
        stranger = _svc(db, stranger_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        view = await owner.get_agent("studio", agent_id)
        assert view.agent_id == agent_id
        with pytest.raises(PermissionDeniedError):
            await stranger.get_agent("studio", agent_id)
        return True

    assert client.portal.call(lambda: _tx(run))


# -- lifecycle writes ----------------------------------------------------------

def test_pause_resume_round_trip_with_history(client, owner_actor):
    async def run(db):
        svc = _svc(db, owner_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        paused = await svc.pause_agent("studio", agent_id, reason="maintenance window")
        assert paused.effective_status == "paused"
        assert paused.registry_state == "paused"
        resumed = await svc.resume_agent("studio", agent_id, reason="maintenance done")
        assert resumed.effective_status == "active"
        assert resumed.registry_state == "active"
        events = await svc.lifecycle_history("studio", agent_id)
        assert [(event.from_state, event.to_state) for event in events] == [
            ("paused", "active"), ("active", "paused")]
        assert events[1].reason == "maintenance window"
        assert events[1].actor_user_id is not None
        return True

    assert client.portal.call(lambda: _tx(run))


def test_pause_is_idempotent(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        first = await svc.pause_agent("studio", agent_id, reason="pause once")
        second = await svc.pause_agent("studio", agent_id, reason="pause twice")
        assert first.registry_state == second.registry_state == "paused"
        events = await svc.lifecycle_history("studio", agent_id)
        assert len(events) == 1
        return True

    assert client.portal.call(lambda: _tx(run))


def test_retire_is_terminal(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        retired = await svc.retire_agent("studio", agent_id, reason="decommissioned")
        assert retired.effective_status == "retired"
        with pytest.raises(InvalidInputError):
            await svc.resume_agent("studio", agent_id, reason="bring back")
        with pytest.raises(InvalidInputError):
            await svc.pause_agent("studio", agent_id, reason="pause retired")
        return True

    assert client.portal.call(lambda: _tx(run))


def test_owner_cannot_retire_own_agent(client, owner_actor, admin_actor):
    async def run(db):
        owner = _svc(db, owner_actor)
        admin = _svc(db, admin_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        with pytest.raises(PermissionDeniedError):
            await owner.retire_agent("studio", agent_id, reason="owner retires")
        retired = await admin.retire_agent("studio", agent_id, reason="admin retires")
        assert retired.effective_status == "retired"
        return True

    assert client.portal.call(lambda: _tx(run))


def test_stranger_cannot_change_lifecycle(client, stranger_actor, admin_actor):
    async def run(db):
        stranger = _svc(db, stranger_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        with pytest.raises(PermissionDeniedError):
            await stranger.pause_agent("studio", agent_id, reason="not mine")
        with pytest.raises(PermissionDeniedError):
            await stranger.retire_agent("studio", agent_id, reason="not mine")
        return True

    assert client.portal.call(lambda: _tx(run))


def test_lifecycle_writes_need_a_reason(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        with pytest.raises(InvalidInputError):
            await svc.pause_agent("studio", agent_id, reason="   ")
        return True

    assert client.portal.call(lambda: _tx(run))


def test_lifecycle_history_needs_read_permission(client, stranger_actor):
    async def run(db):
        svc = _svc(db, stranger_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        with pytest.raises(PermissionDeniedError):
            await svc.lifecycle_history("studio", agent_id)
        return True

    assert client.portal.call(lambda: _tx(run))


# -- owner revocation ----------------------------------------------------------

def test_revoked_owner_reads_paused_before_hook_runs(client, admin_actor):
    """The effective status derives the pause even before the hook runs."""
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        await _revoke_user(db, USER_EMAIL)
        view = await svc.get_agent("studio", agent_id)
        assert view.owner_revoked is True
        assert view.effective_status == "paused"
        # No registry row exists yet: the pause is derived, not stored.
        assert view.registry_state == "active"
        assert await db.scalar(select(WorkforceRegistryAgent).where(
            WorkforceRegistryAgent.agent_id == agent_id)) is None
        return True

    assert client.portal.call(lambda: _tx(run))


def test_owner_revocation_pauses_and_freezes_skills(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        skill_id = await _verified_skill(db, admin_actor, "studio", agent_id)
        owner_id = await _user_id(db, USER_EMAIL)
        await _revoke_user(db, USER_EMAIL)
        counts = await svc.apply_owner_revocation(owner_id, reason="employment ended")
        assert counts == {"agents_paused": 1, "agents_already_paused": 0,
                          "agents_retired_skipped": 0, "skills_frozen": 1}
        view = await svc.get_agent("studio", agent_id)
        assert view.effective_status == "paused"
        assert view.registry_state == "paused"
        assert view.current_skills == ()
        assert view.owner_id == owner_id  # ownership was never reassigned
        row = await db.scalar(select(WorkforceRegistryAgent).where(
            WorkforceRegistryAgent.agent_id == agent_id))
        assert row.owner_user_id == owner_id
        assert row.state_reason == "owner access revoked"
        link = await db.scalar(select(WorkforceAgentSkill).where(
            WorkforceAgentSkill.agent_id == agent_id,
            WorkforceAgentSkill.skill_id == skill_id))
        # Frozen, not deleted: verification history stays, status is expired.
        assert link.verified_at is not None
        assert link.verified_by_user_id is not None
        assert link.expires_at is not None
        assert skill_status(link.verified_at, link.expires_at,
                            datetime.now(UTC)) == "expired"
        events = await svc.lifecycle_history("studio", agent_id)
        assert len(events) == 1
        assert (events[0].from_state, events[0].to_state) == ("active", "paused")
        assert events[0].reason == "owner access revoked"
        assert events[0].owner_user_id == owner_id
        return True

    assert client.portal.call(lambda: _tx(run))


def test_owner_revocation_covers_governance_agents(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_id = await _make_governance_agent(db, USER_EMAIL)
        owner_id = await _user_id(db, USER_EMAIL)
        await _revoke_user(db, USER_EMAIL)
        counts = await svc.apply_owner_revocation(owner_id, reason="employment ended")
        assert counts["agents_paused"] == 1
        view = await svc.get_agent("governance", agent_id)
        assert view.effective_status == "paused"
        return True

    assert client.portal.call(lambda: _tx(run))


def test_owner_revocation_counts_paused_and_skips_retired(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        paused_id = await _make_studio_agent(db, USER_EMAIL, name="Already paused")
        retired_id = await _make_studio_agent(db, USER_EMAIL, name="Retired one")
        await svc.pause_agent("studio", paused_id, reason="paused earlier")
        await svc.retire_agent("studio", retired_id, reason="decommissioned")
        owner_id = await _user_id(db, USER_EMAIL)
        await _revoke_user(db, USER_EMAIL)
        counts = await svc.apply_owner_revocation(owner_id, reason="employment ended")
        assert counts == {"agents_paused": 0, "agents_already_paused": 1,
                          "agents_retired_skipped": 1, "skills_frozen": 0}
        assert (await svc.get_agent("studio", retired_id)).effective_status == "retired"
        return True

    assert client.portal.call(lambda: _tx(run))


def test_owner_revocation_leaves_other_owners_agents_alone(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        other_id = await _make_studio_agent(db, USER2_EMAIL, name="Other owner agent")
        skill_id = await _verified_skill(db, admin_actor, "studio", other_id,
                                         name="other skill")
        owner_id = await _user_id(db, USER_EMAIL)
        await _revoke_user(db, USER_EMAIL)
        counts = await svc.apply_owner_revocation(owner_id, reason="employment ended")
        assert counts["agents_paused"] == 0
        view = await svc.get_agent("studio", other_id)
        assert view.effective_status == "active"
        assert view.current_skills == ("other skill",)
        return True

    assert client.portal.call(lambda: _tx(run))


def test_owner_revocation_does_not_pause_profile_agents(client, admin_actor):
    """Profile agents are department-owned; one user's revocation does not pause them."""
    async def run(db):
        svc = _svc(db, admin_actor)
        agent_key = await _make_profile_agent(db, USER_EMAIL)
        owner_id = await _user_id(db, USER_EMAIL)
        await _revoke_user(db, USER_EMAIL)
        counts = await svc.apply_owner_revocation(owner_id, reason="employment ended")
        assert counts["agents_paused"] == 0
        view = await svc.get_agent("profile", agent_key)
        assert view.effective_status == "active"
        assert view.owner_revoked is False
        return True

    assert client.portal.call(lambda: _tx(run))


def test_owner_revocation_refuses_active_or_unknown_owner(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        owner_id = await _user_id(db, USER_EMAIL)
        with pytest.raises(InvalidInputError):
            await svc.apply_owner_revocation(owner_id, reason="not revoked yet")
        with pytest.raises(NotFoundError):
            await svc.apply_owner_revocation(uuid.uuid4(), reason="nobody")
        return True

    assert client.portal.call(lambda: _tx(run))


def test_revoked_owner_cannot_resume_own_agent(client, owner_actor, admin_actor):
    """After revocation the owner is no longer an active owner for the gate."""
    async def run(db):
        owner = _svc(db, owner_actor)
        admin = _svc(db, admin_actor)
        agent_id = await _make_studio_agent(db, USER_EMAIL)
        owner_id = await _user_id(db, USER_EMAIL)
        await _revoke_user(db, USER_EMAIL)
        await admin.apply_owner_revocation(owner_id, reason="employment ended")
        with pytest.raises(PermissionDeniedError):
            await owner.resume_agent("studio", agent_id, reason="let me back in")
        resumed = await admin.resume_agent("studio", agent_id, reason="owner reinstated")
        assert resumed.effective_status == "paused"  # owner still revoked: derived pause holds
        return True

    assert client.portal.call(lambda: _tx(run))


# -- API -----------------------------------------------------------------------

def test_api_list_pause_and_history(admin_client, granted):
    agent_id = admin_client.portal.call(lambda: _tx(
        lambda db: _make_studio_agent(db, USER_EMAIL)))
    response = admin_client.get("/api/workforce/agents")
    assert response.status_code == 200, response.text
    listed = response.json()
    assert len(listed) == 1
    assert listed[0]["agent_kind"] == "studio"
    assert listed[0]["effective_status"] == "active"
    paused = admin_client.post(
        f"/api/workforce/agents/studio/{agent_id}/pause",
        json={"reason": "maintenance window"})
    assert paused.status_code == 200, paused.text
    assert paused.json()["effective_status"] == "paused"
    history = admin_client.get(f"/api/workforce/agents/studio/{agent_id}/history")
    assert history.status_code == 200, history.text
    assert len(history.json()) == 1
    assert history.json()[0]["to_state"] == "paused"


def test_api_unknown_agent_is_404(admin_client, granted):
    response = admin_client.get(f"/api/workforce/agents/studio/{uuid.uuid4()}")
    assert response.status_code == 404


def test_api_unauthenticated_is_401(client):
    response = client.get("/api/workforce/agents")
    assert response.status_code == 401


def test_api_pause_requires_reason(admin_client, granted):
    agent_id = admin_client.portal.call(lambda: _tx(
        lambda db: _make_studio_agent(db, USER_EMAIL)))
    response = admin_client.post(
        f"/api/workforce/agents/studio/{agent_id}/pause", json={"reason": ""})
    assert response.status_code == 422


def test_api_owner_revocation(admin_client, granted):
    agent_id = admin_client.portal.call(lambda: _tx(
        lambda db: _make_studio_agent(db, USER_EMAIL)))
    owner_id = admin_client.portal.call(lambda: _tx(
        lambda db: _user_id(db, USER_EMAIL)))
    admin_client.portal.call(lambda: _tx(lambda db: _revoke_user(db, USER_EMAIL)))
    response = admin_client.post(
        "/api/workforce/owner-revocations",
        json={"owner_id": str(owner_id), "reason": "employment ended"})
    assert response.status_code == 200, response.text
    assert response.json() == {"agents_paused": 1, "agents_already_paused": 0,
                               "agents_retired_skipped": 0, "skills_frozen": 0}
    view = admin_client.get(f"/api/workforce/agents/studio/{agent_id}")
    assert view.status_code == 200, view.text
    assert view.json()["effective_status"] == "paused"
