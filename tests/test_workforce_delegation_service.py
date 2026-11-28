"""Step J (AI Workforce OS), slice 4: delegation policy service (DB tests).

All tests run on the dedicated ``infraspine_j121_test`` database only (see
``/home/hatch/.agent121-test-env.sh``, never committed). Run:
``source /home/hatch/.agent121-test-env.sh && python -m pytest tests/test_workforce_delegation_service.py -q``
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.agent_studio_models import AgentStudioAgent
from app.db.models import AccessGroup, AgentProfile, DepartmentManager, User
from app.governance.models import AgentIdentity, Department, Member, Tenant
from app.main import app
from app.platform import access as platform_access
from app.services.audit import AuditContext
from app.workforce.delegation_service import WorkforceDelegationService
from app.workforce.registry_service import WorkforceRegistryService
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
def owner_actor(client, clean_tables):
    return _actor_for(client, USER_EMAIL)


@pytest.fixture
def stranger_actor(client, clean_tables):
    return _actor_for(client, USER2_EMAIL)


def _svc(db, actor_ctx):
    actor, ctx = actor_ctx
    return WorkforceDelegationService(db, actor, ctx)


def _reg(db, actor_ctx):
    actor, ctx = actor_ctx
    return WorkforceRegistryService(db, actor, ctx)


async def _make_studio_agent(db, owner_email, name="Triage agent", tools=None):
    owner_id = await _user_id(db, owner_email)
    agent = AgentStudioAgent(owner_id=owner_id, name=name,
                             instructions="Triage invoices.",
                             tool_permissions=tools if tools is not None else ["web_search"],
                             approval_tools=[], delegate_agent_ids=[])
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


RULE = {"from_kind": "studio", "to_kind": "studio",
        "allowed_actions": ["invoice.verify"], "allowed_scopes": ["scope-1"]}


# -- policy writes -------------------------------------------------------------

def test_create_policy_versions_and_single_active(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        first = await svc.create_policy([RULE], max_depth=2)
        assert first.version == 1 and first.is_active is True
        second = await svc.create_policy([RULE], max_depth=3)
        assert second.version == 2 and second.max_depth == 3
        policies = await svc.list_policies()
        assert [policy.version for policy in policies] == [2, 1]
        assert [policy.is_active for policy in policies] == [True, False]
        assert (await svc.active_policy()).version == 2
        return True

    assert client.portal.call(lambda: _tx(run))


def test_create_policy_needs_manage(client, stranger_actor):
    async def run(db):
        svc = _svc(db, stranger_actor)
        with pytest.raises(PermissionDeniedError):
            await svc.create_policy([RULE])
        return True

    assert client.portal.call(lambda: _tx(run))


def test_create_policy_validates_rules_and_depth(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        with pytest.raises(InvalidInputError):
            await svc.create_policy([{"from_kind": "studio"}])
        with pytest.raises(InvalidInputError):
            await svc.create_policy([RULE], max_depth=0)
        with pytest.raises(InvalidInputError):
            await svc.create_policy([RULE], max_depth=6)
        return True

    assert client.portal.call(lambda: _tx(run))


# -- decisions -----------------------------------------------------------------

def test_evaluate_without_policy_denies(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        parent = await _make_studio_agent(db, USER_EMAIL, name="Parent")
        child = await _make_studio_agent(db, USER_EMAIL, name="Child")
        decision = await svc.evaluate("studio", parent, "studio", child,
                                      action="invoice.verify", scope_id="scope-1")
        assert decision.allowed is False
        assert decision.reason == "no_active_policy"
        assert decision.policy_version == 0
        return True

    assert client.portal.call(lambda: _tx(run))


def test_evaluate_rule_match_and_mismatch(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        await svc.create_policy([RULE], max_depth=2)
        parent = await _make_studio_agent(db, USER_EMAIL, name="Parent")
        child = await _make_studio_agent(db, USER_EMAIL, name="Child")
        allowed = await svc.evaluate("studio", parent, "studio", child,
                                     action="invoice.verify", scope_id="scope-1")
        assert allowed.allowed is True and allowed.policy_version == 1
        denied = await svc.evaluate("studio", parent, "studio", child,
                                    action="payment.approve", scope_id="scope-1")
        assert denied.allowed is False and denied.reason == "no_delegation_rule"
        return True

    assert client.portal.call(lambda: _tx(run))


def test_evaluate_unknown_agent_is_404_and_bad_kind_is_400(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        parent = await _make_studio_agent(db, USER_EMAIL, name="Parent")
        with pytest.raises(NotFoundError):
            await svc.evaluate("studio", parent, "studio", uuid.uuid4())
        with pytest.raises(NotFoundError):
            await svc.evaluate("studio", uuid.uuid4(), "studio", parent)
        with pytest.raises(InvalidInputError):
            await svc.evaluate("human", parent, "studio", parent)
        return True

    assert client.portal.call(lambda: _tx(run))


def test_evaluate_refuses_paused_child(client, admin_actor, owner_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        await svc.create_policy([RULE], max_depth=2)
        parent = await _make_studio_agent(db, USER_EMAIL, name="Parent")
        child = await _make_studio_agent(db, USER_EMAIL, name="Child")
        await _reg(db, owner_actor).pause_agent("studio", child, reason="maintenance")
        decision = await svc.evaluate("studio", parent, "studio", child,
                                      action="invoice.verify", scope_id="scope-1")
        assert decision.allowed is False
        assert decision.reason == "child_not_active:paused"
        return True

    assert client.portal.call(lambda: _tx(run))


def test_evaluate_refuses_child_whose_owner_was_revoked(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        await svc.create_policy([RULE], max_depth=2)
        parent = await _make_studio_agent(db, ADMIN_EMAIL, name="Parent")
        child = await _make_studio_agent(db, USER_EMAIL, name="Child")
        # Mirror revoke_user_access's account effect: the owner can no longer act.
        await db.execute(update(User).where(User.email == USER_EMAIL).values(is_active=False))
        decision = await svc.evaluate("studio", parent, "studio", child,
                                      action="invoice.verify", scope_id="scope-1")
        assert decision.allowed is False
        assert decision.reason == "child_not_active:paused"
        return True

    assert client.portal.call(lambda: _tx(run))


def test_evaluate_forbidden_tool_and_separated_duty(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        await svc.create_policy([RULE], max_depth=2)
        parent = await _make_studio_agent(db, USER_EMAIL, name="Parent")
        child = await _make_studio_agent(db, USER_EMAIL, name="Child",
                                         tools=["web_search"])
        originator = await _user_id(db, USER_EMAIL)
        forbidden = await svc.evaluate(
            "studio", parent, "studio", child, action="invoice.verify", scope_id="scope-1",
            requested_tools=frozenset({"send_payment"}))
        assert forbidden.allowed is False
        assert forbidden.reason == "tools_not_permitted:send_payment"
        violation = await svc.evaluate(
            "studio", parent, "studio", child, action="invoice.verify", scope_id="scope-1",
            originator_user_id=originator, approver_user_id=originator)
        assert violation.allowed is False
        assert violation.reason == "separated_duty_violation"
        return True

    assert client.portal.call(lambda: _tx(run))


def test_evaluate_governance_child_uses_its_own_tools(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        await svc.create_policy([{
            "from_kind": "studio", "to_kind": "governance",
            "allowed_actions": ["invoice.verify"], "allowed_scopes": ["scope-1"],
        }], max_depth=2)
        parent = await _make_studio_agent(db, USER_EMAIL, name="Parent")
        child = await _make_governance_agent(db, USER_EMAIL)
        allowed = await svc.evaluate(
            "studio", parent, "governance", child, action="invoice.verify",
            scope_id="scope-1", requested_tools=frozenset({"knowledge_search"}))
        assert allowed.allowed is True
        denied = await svc.evaluate(
            "studio", parent, "governance", child, action="invoice.verify",
            scope_id="scope-1", requested_tools=frozenset({"web_search"}))
        assert denied.reason == "tools_not_permitted:web_search"
        return True

    assert client.portal.call(lambda: _tx(run))


# -- audit trail ---------------------------------------------------------------

def test_record_delegation_writes_audit_for_allowed_and_denied(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        await svc.create_policy([RULE], max_depth=2)
        parent = await _make_studio_agent(db, USER_EMAIL, name="Parent")
        child = await _make_studio_agent(db, USER_EMAIL, name="Child")
        run_id = uuid.uuid4()
        allowed, view = await svc.record_delegation(
            "studio", parent, "studio", child, action="invoice.verify",
            scope_id="scope-1", parent_run_id=run_id)
        assert allowed.allowed is True
        assert view.decision == "allowed" and view.policy_version == 1
        assert view.parent_run_id == run_id and view.child_run_id is not None
        assert view.child_run_kind == "inline"
        denied, denied_view = await svc.record_delegation(
            "studio", parent, "studio", child, action="payment.approve",
            scope_id="scope-1", parent_run_id=run_id)
        assert denied.allowed is False and denied_view.decision == "denied"
        audits = await svc.list_audits()
        assert len(audits) == 2
        assert audits[0].decision == "denied" and audits[0].reason == "no_delegation_rule"
        assert audits[1].decision == "allowed"
        assert audits[0].parent_run_id == run_id
        return True

    assert client.portal.call(lambda: _tx(run))


def test_record_delegation_without_policy_still_audits(client, admin_actor):
    async def run(db):
        svc = _svc(db, admin_actor)
        parent = await _make_studio_agent(db, USER_EMAIL, name="Parent")
        child = await _make_studio_agent(db, USER_EMAIL, name="Child")
        decision, view = await svc.record_delegation("studio", parent, "studio", child)
        assert decision.allowed is False and decision.reason == "no_active_policy"
        assert view.policy_version == 0 and view.decision == "denied"
        assert len(await svc.list_audits()) == 1
        return True

    assert client.portal.call(lambda: _tx(run))


# -- API routes ----------------------------------------------------------------

def test_delegation_policy_api_round_trip(admin_client, granted):
    created = admin_client.post("/api/workforce/delegation-policies", json={
        "rules": [RULE], "max_depth": 2})
    assert created.status_code == 200, created.text
    assert created.json()["version"] == 1 and created.json()["is_active"] is True
    active = admin_client.get("/api/workforce/delegation-policies/active")
    assert active.status_code == 200 and active.json()["version"] == 1
    listed = admin_client.get("/api/workforce/delegation-policies")
    assert listed.status_code == 200 and len(listed.json()) == 1
    audits = admin_client.get("/api/workforce/delegations")
    assert audits.status_code == 200 and audits.json() == []
