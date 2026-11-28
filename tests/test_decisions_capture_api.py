"""Step M slice 4: capture API — governance / drill / simulation capture endpoints.

Runs on the dedicated `infraspine_decision_test` database only (see
`.test-env-decision125.sh`, never committed). The admin holds
`decision.record` / `decision.read` / `decision.approve` on the org
scope; the plain user holds `decision.read` only; the approver holds
`decision.approve` only.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.continuity.models import Member as ContinuityMember
from app.continuity.models import Plan as ContinuityPlan
from app.continuity.models import Run as ContinuityRun
from app.continuity.models import Tenant as ContinuityTenant
from app.db.models import User
from app.governance.models import ActionRequest, Decision as GovernanceDecision
from app.governance.models import Member as GovernanceMember
from app.governance.models import Resource as GovernanceResource
from app.governance.models import Tenant as GovernanceTenant
from app.main import app
from app.platform import access as platform_access
from app.platform.approvals import decide_approval
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, USER2_EMAIL, USER_EMAIL  # noqa: F401

pytestmark = pytest.mark.usefixtures("clean_tables")


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


async def _principal(db, user_id):
    return platform_access.principal_for(await db.get(User, user_id))


def _session(client, email):
    client.cookies.clear()
    from tests.conftest import login

    login(client, email)
    return dict(client.cookies)


def _as(client, cookies):
    client.cookies.clear()
    client.cookies.update(cookies)
    return client


@pytest.fixture
def org_setup(client):
    """Decision roles + one approved governance request + one drill run."""
    async def setup(db):
        admin_id = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        user_id = await db.scalar(select(User.id).where(User.email == USER_EMAIL))
        user2_id = await db.scalar(select(User.id).where(User.email == USER2_EMAIL))
        admin = await db.get(User, admin_id)
        actor = platform_access.principal_for(admin)
        await platform_access.ensure_role(
            db, "m-test-decisions", {"decision.record", "decision.read", "decision.approve"},
            name="M test decisions")
        await platform_access.ensure_role(
            db, "m-test-reader", {"decision.read"}, name="M test reader")
        await platform_access.ensure_role(
            db, "m-test-approver", {"decision.approve"}, name="M test approver")
        org = await platform_access.ensure_org_scope(db)
        await platform_access.grant_membership(
            db, actor, user_id=admin_id, scope_id=org.id, role_key="m-test-decisions")
        await platform_access.grant_membership(
            db, actor, user_id=user_id, scope_id=org.id, role_key="m-test-reader")
        await platform_access.grant_membership(
            db, actor, user_id=user2_id, scope_id=org.id, role_key="m-test-approver")

        gov_tenant = GovernanceTenant(name="Gov tenant")
        db.add(gov_tenant)
        await db.flush()
        db.add(GovernanceMember(tenant_id=gov_tenant.id, user_id=admin_id))
        gov_resource = GovernanceResource(
            tenant_id=gov_tenant.id, kind="document", external_id=uuid.uuid4(),
            classification="internal", label="Doc")
        db.add(gov_resource)
        await db.flush()
        gov_request = ActionRequest(
            tenant_id=gov_tenant.id, requester_user_id=admin_id, resource_id=gov_resource.id,
            action="document.share", action_hash="h" * 64, policy_version=1, risk="low",
            required_approvals=1, status="ready",
            expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(gov_request)
        await db.flush()
        db.add(GovernanceDecision(
            tenant_id=gov_tenant.id, request_id=gov_request.id, actor_id=admin_id,
            decision="approve", reason="looks good"))
        gov_pending = ActionRequest(
            tenant_id=gov_tenant.id, requester_user_id=admin_id, resource_id=gov_resource.id,
            action="document.share", action_hash="i" * 64, policy_version=1, risk="low",
            required_approvals=1, status="pending",
            expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(gov_pending)
        await db.flush()
        db.add(GovernanceDecision(
            tenant_id=gov_tenant.id, request_id=gov_pending.id, actor_id=admin_id,
            decision="approve", reason="looks good"))

        bc_tenant = ContinuityTenant(name="BC tenant")
        db.add(bc_tenant)
        await db.flush()
        db.add(ContinuityMember(tenant_id=bc_tenant.id, user_id=admin_id, role="owner",
                                all_departments=True, active=True))
        bc_plan = ContinuityPlan(
            tenant_id=bc_tenant.id, version=1, title="Plan", status="approved",
            data={}, digest="p" * 64, requested_by=admin_id,
            approval_expires_at=datetime.now(UTC) + timedelta(days=1),
            review_due_at=datetime.now(UTC) + timedelta(days=30))
        db.add(bc_plan)
        await db.flush()
        bc_run = ContinuityRun(
            tenant_id=bc_tenant.id, plan_id=bc_plan.id, idempotency_key="k-1",
            digest="d" * 64, mode="drill", status="active", requested_by=admin_id,
            approval_expires_at=datetime.now(UTC) + timedelta(days=1),
            disruption_at=datetime.now(UTC) + timedelta(days=2),
            activated_at=datetime(2026, 11, 20, 9, 0, tzinfo=UTC))
        db.add(bc_run)
        bc_live = ContinuityRun(
            tenant_id=bc_tenant.id, plan_id=bc_plan.id, idempotency_key="k-2",
            digest="e" * 64, mode="live", status="active", requested_by=admin_id,
            approval_expires_at=datetime.now(UTC) + timedelta(days=1),
            disruption_at=datetime.now(UTC) + timedelta(days=2))
        db.add(bc_live)
        await db.flush()
        return {
            "org_id": org.id, "admin_id": admin_id, "user_id": user_id, "user2_id": user2_id,
            "gov_request_id": gov_request.id, "gov_pending_id": gov_pending.id,
            "drill_run_id": bc_run.id, "live_run_id": bc_live.id,
        }

    return client.portal.call(lambda: _tx(setup))


def _body(**overrides):
    payload = {
        "title": "Approve the supplier switch",
        "rationale": "The drill showed the backup supplier restores service in 40 minutes.",
        "kind": "operations",
    }
    payload.update(overrides)
    return payload


def _approve_request(client, setup, *, requester_cookies, approver_id, action_type, decision_id, kind):
    """Request an approval through the API, approve it as another user; return the approval id."""
    payload = {"action_type": action_type, "decision_id": str(decision_id), "kind": kind}
    response = _as(client, requester_cookies).post(
        f"/api/decisions/scopes/{setup['org_id']}/approvals", json=payload)
    assert response.status_code == 201, response.text
    data = response.json()
    approval_id, action_hash = uuid.UUID(data["approval_id"]), data["action_hash"]

    async def decide(db):
        await decide_approval(db, await _principal(db, approver_id), approval_id,
                              "approve", presented_hash=action_hash)

    client.portal.call(lambda: _tx(decide))
    return approval_id


def test_capture_governance(client, org_setup):
    admin = _session(client, ADMIN_EMAIL)
    reader = _session(client, USER_EMAIL)
    org_id = org_setup["org_id"]
    base = f"/api/decisions/scopes/{org_id}/capture/governance"

    created = _as(client, admin).post(
        base, json=_body(request_id=str(org_setup["gov_request_id"])))
    assert created.status_code == 201, created.text
    data = created.json()
    assert data["status"] == "active"
    assert data["evidence"] == [
        {"kind": "governance_proposal", "ref": str(org_setup["gov_request_id"]),
         "label": "Governance proposal: document.share"},
        {"kind": "governance_decision", "ref": str(org_setup["gov_request_id"]),
         "label": "Governance decision: document.share"},
    ]
    # GET filters per reader: the admin is a governance member, so both refs stay visible.
    got = _as(client, admin).get(f"/api/decisions/scopes/{org_id}/{data['id']}")
    assert got.status_code == 200, got.text
    assert got.json()["hidden_evidence"] == 0

    # Unknown request: 404. Pending request: 422.
    assert _as(client, admin).post(
        base, json=_body(request_id=str(uuid.uuid4()))).status_code == 404
    assert _as(client, admin).post(
        base, json=_body(request_id=str(org_setup["gov_pending_id"]))).status_code == 422
    # A reader may not capture (403).
    assert _as(client, reader).post(
        base, json=_body(request_id=str(org_setup["gov_request_id"]))).status_code == 403


def test_capture_governance_sensitive_needs_approval(client, org_setup):
    admin = _session(client, ADMIN_EMAIL)
    org_id = org_setup["org_id"]
    base = f"/api/decisions/scopes/{org_id}/capture/governance"
    decision_id = uuid.uuid4()

    refused = _as(client, admin).post(base, json=_body(
        request_id=str(org_setup["gov_request_id"]), kind="finance",
        decision_id=str(decision_id)))
    assert refused.status_code == 403, refused.text

    approval_id = _approve_request(
        client, org_setup, requester_cookies=admin, approver_id=org_setup["user2_id"],
        action_type="decision.record", decision_id=decision_id, kind="finance")
    created = _as(client, admin).post(base, json=_body(
        request_id=str(org_setup["gov_request_id"]), kind="finance",
        decision_id=str(decision_id), approval_id=str(approval_id)))
    assert created.status_code == 201, created.text
    assert created.json()["id"] == str(decision_id)


def test_capture_drill_and_listing(client, org_setup):
    admin = _session(client, ADMIN_EMAIL)
    reader = _session(client, USER_EMAIL)
    approver = _session(client, USER2_EMAIL)
    org_id = org_setup["org_id"]
    base = f"/api/decisions/scopes/{org_id}/capture/drill"

    first = _as(client, admin).post(
        base, json=_body(run_id=str(org_setup["drill_run_id"]), title="First drill decision"))
    assert first.status_code == 201, first.text
    assert first.json()["evidence"] == [
        {"kind": "continuity_drill", "ref": str(org_setup["drill_run_id"]),
         "label": "Continuity drill run"},
    ]
    assert first.json()["decided_at"] == "2026-11-20T09:00:00+00:00"
    second = _as(client, admin).post(
        base, json=_body(run_id=str(org_setup["drill_run_id"]), title="Second drill decision"))
    assert second.status_code == 201, second.text
    # An unrelated decision must not appear in the drill listing.
    unrelated = _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}", json=_body(title="Unrelated decision"))
    assert unrelated.status_code == 201, unrelated.text

    # The listing is keyed by the drill, so it needs continuity
    # membership on the drill's tenant as well as decision.read on
    # the scope: the admin (a drill member) lists; the reader holds
    # decision.read on the scope but is not a drill member, so the
    # drill is not revealed to them (404, same rule as slice 3's
    # evidence filtering).
    listed = _as(client, admin).get(
        f"/api/decisions/scopes/{org_id}/drills/{org_setup['drill_run_id']}/decisions")
    assert listed.status_code == 200, listed.text
    assert listed.json()["run_id"] == str(org_setup["drill_run_id"])
    assert [d["id"] for d in listed.json()["decisions"]] == [
        second.json()["id"], first.json()["id"]]
    assert _as(client, reader).get(
        f"/api/decisions/scopes/{org_id}/drills/{org_setup['drill_run_id']}/decisions"
    ).status_code == 404

    # Unknown run: 404. Live run: 422. Approver (no decision.read): 403.
    assert _as(client, admin).post(
        base, json=_body(run_id=str(uuid.uuid4()))).status_code == 404
    assert _as(client, admin).post(
        base, json=_body(run_id=str(org_setup["live_run_id"]))).status_code == 422
    assert _as(client, approver).get(
        f"/api/decisions/scopes/{org_id}/drills/{org_setup['drill_run_id']}/decisions"
    ).status_code == 403


def test_capture_simulation(client, org_setup):
    admin = _session(client, ADMIN_EMAIL)
    reader = _session(client, USER_EMAIL)
    org_id = org_setup["org_id"]
    base = f"/api/decisions/scopes/{org_id}/capture/simulation"

    created = _as(client, admin).post(base, json=_body(
        simulation_id="sim-42", simulation_label="Supplier outage simulation"))
    assert created.status_code == 201, created.text
    assert created.json()["evidence"] == [
        {"kind": "simulation", "ref": "sim-42", "label": "Supplier outage simulation"},
    ]
    # Write responses echo as submitted; on GET the simulation ref is hidden (AL unmerged).
    got = _as(client, reader).get(f"/api/decisions/scopes/{org_id}/{created.json()['id']}")
    assert got.status_code == 200, got.text
    assert got.json()["evidence"] == []
    assert got.json()["hidden_evidence"] == 1

    # Empty simulation id: 422. Reader may not capture: 403.
    assert _as(client, admin).post(
        base, json=_body(simulation_id="")).status_code == 422
    assert _as(client, reader).post(
        base, json=_body(simulation_id="sim-43")).status_code == 403
