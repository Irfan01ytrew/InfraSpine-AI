"""Step M slice 4: capture paths — record decisions from governance, drills, simulations.

Runs on the dedicated `infraspine_decision_test` database only (see
`.test-env-decision125.sh`, never committed). Fixtures: one governance
tenant the admin belongs to (ready request + approve decision), a second
governance tenant the admin does NOT belong to, a pending request, a
request with no decisions, a reject-only request, one continuity tenant
the admin belongs to (drill run active, live run, pending drill run),
and a second continuity tenant the admin does not belong to.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.continuity.models import Member as ContinuityMember
from app.continuity.models import Plan as ContinuityPlan
from app.continuity.models import Run as ContinuityRun
from app.continuity.models import Tenant as ContinuityTenant
from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.decision_models import DecisionEventRow
from app.db.models import AuditLog, User
from app.decision_memory.capture import (
    list_decisions_for_drill,
    record_from_drill,
    record_from_governance,
    record_from_simulation,
)
from app.decision_memory.records import EvidenceRef, new_decision_id
from app.decision_memory.service import (
    get_decision,
    request_decision_approval,
    revoke_decision,
)
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


def _approve(client, setup, *, requester_id, approver_id, action_type, decision_id, kind):
    """Request an approval as one user, have another user approve it; return the approval id."""
    async def request(db):
        req = await request_decision_approval(
            db, await _principal(db, requester_id), action_type=action_type,
            scope_id=setup["org_id"], decision_id=decision_id, kind=kind)
        return str(req.id), req.action_hash

    approval_id, action_hash = client.portal.call(lambda: _tx(request))

    async def decide(db):
        await decide_approval(db, await _principal(db, approver_id), uuid.UUID(approval_id),
                              "approve", presented_hash=action_hash)

    client.portal.call(lambda: _tx(decide))
    return uuid.UUID(approval_id)


@pytest.fixture
def org_setup(client):
    """Decision roles + governance/continuity fixtures for capture tests."""
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
            db, "m-test-approver", {"decision.read", "decision.approve"},
            name="M test approver")
        org = await platform_access.ensure_org_scope(db)
        await platform_access.grant_membership(
            db, actor, user_id=admin_id, scope_id=org.id, role_key="m-test-decisions")
        await platform_access.grant_membership(
            db, actor, user_id=user2_id, scope_id=org.id, role_key="m-test-approver")

        # One governance tenant: security_members has a UNIQUE(user_id)
        # constraint — a user belongs to exactly one governance tenant —
        # so all requests live in this tenant, and the "foreign" request
        # lives in a second tenant whose member is user2, not the admin.
        gov_tenant = GovernanceTenant(name="Gov tenant")
        db.add(gov_tenant)
        await db.flush()
        db.add(GovernanceMember(tenant_id=gov_tenant.id, user_id=admin_id))
        gov_resource = GovernanceResource(
            tenant_id=gov_tenant.id, kind="document", external_id=uuid.uuid4(),
            classification="internal", label="Doc")
        db.add(gov_resource)
        await db.flush()

        async def gov_request(*, status="ready", decisions=("approve",)):
            request = ActionRequest(
                tenant_id=gov_tenant.id, requester_user_id=admin_id,
                resource_id=gov_resource.id,
                action="document.share", action_hash="h" * 64, policy_version=1, risk="low",
                required_approvals=1, status=status,
                expires_at=datetime.now(UTC) + timedelta(days=1))
            db.add(request)
            await db.flush()
            for verdict in decisions:
                db.add(GovernanceDecision(
                    tenant_id=gov_tenant.id, request_id=request.id, actor_id=admin_id,
                    decision=verdict, reason="reviewed"))
            await db.flush()
            return request

        gov_ready = await gov_request()
        gov_pending = await gov_request(status="pending")
        gov_undecided = await gov_request(decisions=())
        gov_rejected = await gov_request(status="rejected", decisions=("reject",))

        gov_foreign_tenant = GovernanceTenant(name="Gov foreign tenant")
        db.add(gov_foreign_tenant)
        await db.flush()
        db.add(GovernanceMember(tenant_id=gov_foreign_tenant.id, user_id=user2_id))
        gov_foreign_resource = GovernanceResource(
            tenant_id=gov_foreign_tenant.id, kind="document", external_id=uuid.uuid4(),
            classification="internal", label="Doc")
        db.add(gov_foreign_resource)
        await db.flush()
        gov_foreign = ActionRequest(
            tenant_id=gov_foreign_tenant.id, requester_user_id=user2_id,
            resource_id=gov_foreign_resource.id,
            action="document.share", action_hash="h" * 64, policy_version=1, risk="low",
            required_approvals=1, status="ready",
            expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(gov_foreign)
        await db.flush()
        db.add(GovernanceDecision(
            tenant_id=gov_foreign_tenant.id, request_id=gov_foreign.id, actor_id=user2_id,
            decision="approve", reason="reviewed"))
        await db.flush()

        async def bc_run(*, member=True, mode="drill", status="active"):
            tenant = ContinuityTenant(name=f"BC {uuid.uuid4().hex[:8]}")
            db.add(tenant)
            await db.flush()
            if member:
                db.add(ContinuityMember(tenant_id=tenant.id, user_id=admin_id, role="owner",
                                        all_departments=True, active=True))
            plan = ContinuityPlan(
                tenant_id=tenant.id, version=1, title="Plan", status="approved",
                data={}, digest="p" * 64, requested_by=admin_id,
                approval_expires_at=datetime.now(UTC) + timedelta(days=1),
                review_due_at=datetime.now(UTC) + timedelta(days=30))
            db.add(plan)
            await db.flush()
            run = ContinuityRun(
                tenant_id=tenant.id, plan_id=plan.id, idempotency_key=f"k-{uuid.uuid4().hex[:8]}",
                digest="d" * 64, mode=mode, status=status, requested_by=admin_id,
                approval_expires_at=datetime.now(UTC) + timedelta(days=1),
                disruption_at=datetime.now(UTC) + timedelta(days=2),
                activated_at=datetime(2026, 11, 20, 9, 0, tzinfo=UTC))
            db.add(run)
            await db.flush()
            return run

        drill_active = await bc_run()
        drill_live = await bc_run(mode="live")
        drill_pending = await bc_run(status="pending")
        drill_foreign = await bc_run(member=False)

        return {
            "org_id": org.id, "admin_id": admin_id, "user_id": user_id, "user2_id": user2_id,
            "gov_ready_id": gov_ready.id, "gov_pending_id": gov_pending.id,
            "gov_undecided_id": gov_undecided.id, "gov_rejected_id": gov_rejected.id,
            "gov_foreign_id": gov_foreign.id,
            "drill_active_id": drill_active.id, "drill_live_id": drill_live.id,
            "drill_pending_id": drill_pending.id, "drill_foreign_id": drill_foreign.id,
        }

    return client.portal.call(lambda: _tx(setup))


def _capture_body(**overrides):
    fields = {
        "title": "Approve the supplier switch",
        "rationale": "The drill showed the backup supplier restores service in 40 minutes.",
    }
    fields.update(overrides)
    return fields


# ----------------------------------------------------------------------------------------------- governance

def test_governance_capture_happy_path(client, org_setup):
    async def run(db):
        actor = await _principal(db, org_setup["admin_id"])
        approve_at = await db.scalar(select(GovernanceDecision.created_at).where(
            GovernanceDecision.request_id == org_setup["gov_ready_id"],
            GovernanceDecision.decision == "approve"))
        row = await record_from_governance(
            db, actor, scope_id=org_setup["org_id"], request_id=org_setup["gov_ready_id"],
            **_capture_body())
        assert row.status == "active"
        assert row.kind == "other"
        # Both the proposal and the decision are cited, by request id.
        assert row.evidence == [
            {"kind": "governance_proposal", "ref": str(org_setup["gov_ready_id"]),
             "label": "Governance proposal: document.share"},
            {"kind": "governance_decision", "ref": str(org_setup["gov_ready_id"]),
             "label": "Governance decision: document.share"},
        ]
        # decided_at defaults to the approval decision, not to "now".
        assert abs((row.decided_at - approve_at).total_seconds()) < 1
        events = list(await db.scalars(select(DecisionEventRow).where(
            DecisionEventRow.decision_id == row.id)))
        assert len(events) == 1 and events[0].event == "recorded"
        audits = list(await db.scalars(select(AuditLog).where(
            AuditLog.action == "decisions.recorded", AuditLog.target == str(row.id))))
        assert len(audits) == 1
        # The governance reason is never copied into the record.
        assert "reviewed" not in row.rationale
        return True

    assert client.portal.call(lambda: _tx(run))


def test_governance_capture_refusals(client, org_setup):
    async def run(db):
        actor = await _principal(db, org_setup["admin_id"])
        scope_id = org_setup["org_id"]
        body = _capture_body()
        # Not approved yet (pending): refused, even with an approve vote.
        with pytest.raises(InvalidInputError):
            await record_from_governance(
                db, actor, scope_id=scope_id, request_id=org_setup["gov_pending_id"], **body)
        # Approved status but no decision row at all: refused.
        with pytest.raises(InvalidInputError):
            await record_from_governance(
                db, actor, scope_id=scope_id, request_id=org_setup["gov_undecided_id"], **body)
        # Rejected: refused.
        with pytest.raises(InvalidInputError):
            await record_from_governance(
                db, actor, scope_id=scope_id, request_id=org_setup["gov_rejected_id"], **body)
        # Unknown request: 404.
        with pytest.raises(NotFoundError):
            await record_from_governance(
                db, actor, scope_id=scope_id, request_id=uuid.uuid4(), **body)
        # Approved request in a tenant the actor does not belong to: 404, no leak.
        with pytest.raises(NotFoundError):
            await record_from_governance(
                db, actor, scope_id=scope_id, request_id=org_setup["gov_foreign_id"], **body)
        # A member without decision.record is refused (403).
        member = await _principal(db, org_setup["user_id"])
        with pytest.raises(PermissionDeniedError):
            await record_from_governance(
                db, member, scope_id=scope_id, request_id=org_setup["gov_ready_id"], **body)
        return True

    assert client.portal.call(lambda: _tx(run))


def test_governance_capture_sensitive_needs_approval(client, org_setup):
    decision_id = new_decision_id()
    async def without_approval(db):
        actor = await _principal(db, org_setup["admin_id"])
        with pytest.raises(PermissionDeniedError):
            await record_from_governance(
                db, actor, scope_id=org_setup["org_id"],
                request_id=org_setup["gov_ready_id"], kind="finance",
                decision_id=decision_id, **_capture_body())
        return True

    assert client.portal.call(lambda: _tx(without_approval))

    approval_id = _approve(client, org_setup, requester_id=org_setup["admin_id"],
                           approver_id=org_setup["user2_id"], action_type="decision.record",
                           decision_id=decision_id, kind="finance")

    async def with_approval(db):
        actor = await _principal(db, org_setup["admin_id"])
        row = await record_from_governance(
            db, actor, scope_id=org_setup["org_id"],
            request_id=org_setup["gov_ready_id"], kind="finance",
            decision_id=decision_id, approval_id=approval_id, **_capture_body())
        assert row.kind == "finance" and row.status == "active"
        assert row.id == uuid.UUID(decision_id)
        return True

    assert client.portal.call(lambda: _tx(with_approval))


# ----------------------------------------------------------------------------------------------- drill

def test_drill_capture_happy_path(client, org_setup):
    async def run(db):
        actor = await _principal(db, org_setup["admin_id"])
        row = await record_from_drill(
            db, actor, scope_id=org_setup["org_id"], run_id=org_setup["drill_active_id"],
            **_capture_body())
        assert row.status == "active"
        assert row.evidence == [
            {"kind": "continuity_drill", "ref": str(org_setup["drill_active_id"]),
             "label": "Continuity drill run"},
        ]
        # decided_at defaults to when the run was activated.
        assert row.decided_at == datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
        events = list(await db.scalars(select(DecisionEventRow).where(
            DecisionEventRow.decision_id == row.id)))
        assert len(events) == 1 and events[0].event == "recorded"
        return True

    assert client.portal.call(lambda: _tx(run))


def test_drill_capture_refusals(client, org_setup):
    async def run(db):
        actor = await _principal(db, org_setup["admin_id"])
        scope_id = org_setup["org_id"]
        body = _capture_body()
        # A live (non-drill) run cannot capture drill decisions.
        with pytest.raises(InvalidInputError):
            await record_from_drill(
                db, actor, scope_id=scope_id, run_id=org_setup["drill_live_id"], **body)
        # A drill that has not started cannot have decisions taken during it.
        with pytest.raises(InvalidInputError):
            await record_from_drill(
                db, actor, scope_id=scope_id, run_id=org_setup["drill_pending_id"], **body)
        # Unknown run: 404.
        with pytest.raises(NotFoundError):
            await record_from_drill(
                db, actor, scope_id=scope_id, run_id=uuid.uuid4(), **body)
        # Drill in a tenant the actor does not belong to: 404, no leak.
        with pytest.raises(NotFoundError):
            await record_from_drill(
                db, actor, scope_id=scope_id, run_id=org_setup["drill_foreign_id"], **body)
        # A member without decision.record is refused (403).
        member = await _principal(db, org_setup["user_id"])
        with pytest.raises(PermissionDeniedError):
            await record_from_drill(
                db, member, scope_id=scope_id, run_id=org_setup["drill_active_id"], **body)
        return True

    assert client.portal.call(lambda: _tx(run))


def test_drill_decisions_listing(client, org_setup):
    async def run(db):
        actor = await _principal(db, org_setup["admin_id"])
        scope_id = org_setup["org_id"]
        first = await record_from_drill(
            db, actor, scope_id=scope_id, run_id=org_setup["drill_active_id"],
            **_capture_body(title="First drill decision"))
        second = await record_from_drill(
            db, actor, scope_id=scope_id, run_id=org_setup["drill_active_id"],
            **_capture_body(title="Second drill decision"))
        other = await record_from_drill(
            db, actor, scope_id=scope_id, run_id=org_setup["drill_active_id"],
            **_capture_body(title="Third drill decision"))
        await revoke_decision(db, actor, scope_id=scope_id,
                              decision_id=str(other.id), reason="superseded by events")
        # A decision that does not cite the drill is not listed.
        from app.decision_memory.service import record_decision
        await record_decision(db, actor, scope_id=scope_id,
                              title="Unrelated decision", rationale="Nothing to do with drills.")
        # Ordering is newest decided_at first: a decision with an
        # explicitly later decided_at sorts ahead of the three that
        # share the drill's activated_at.
        later = await record_from_drill(
            db, actor, scope_id=scope_id, run_id=org_setup["drill_active_id"],
            decided_at=datetime(2026, 11, 25, 9, 0, tzinfo=UTC),
            **_capture_body(title="Later drill decision"))
        listed = await list_decisions_for_drill(
            db, actor, scope_id=scope_id, run_id=org_setup["drill_active_id"])
        assert {row.id for row in listed} == {later.id, second.id, first.id}
        assert listed[0].id == later.id
        archived = await list_decisions_for_drill(
            db, actor, scope_id=scope_id, run_id=org_setup["drill_active_id"],
            include_archived=True)
        # The three same-day records share decided_at AND created_at
        # (Postgres now() is the transaction start, and this test runs
        # in one transaction), so their relative order is the id
        # tie-break — assert the set, not the order. The revoked row
        # is present only with include_archived.
        assert {row.id for row in archived} == {other.id, later.id, second.id, first.id}
        assert archived[0].id == later.id
        # Unknown run / foreign run: 404.
        with pytest.raises(NotFoundError):
            await list_decisions_for_drill(
                db, actor, scope_id=scope_id, run_id=uuid.uuid4())
        with pytest.raises(NotFoundError):
            await list_decisions_for_drill(
                db, actor, scope_id=scope_id, run_id=org_setup["drill_foreign_id"])
        # A member without decision.read is refused (403).
        plain = await _principal(db, org_setup["user_id"])
        with pytest.raises(PermissionDeniedError):
            await list_decisions_for_drill(
                db, plain, scope_id=scope_id, run_id=org_setup["drill_active_id"])
        return True

    assert client.portal.call(lambda: _tx(run))


# ----------------------------------------------------------------------------------------------- simulation

def test_simulation_capture(client, org_setup):
    async def run(db):
        actor = await _principal(db, org_setup["admin_id"])
        scope_id = org_setup["org_id"]
        row = await record_from_simulation(
            db, actor, scope_id=scope_id, simulation_id="sim-42",
            simulation_label="Supplier outage simulation",
            **_capture_body())
        assert row.status == "active"
        assert row.evidence == [
            {"kind": "simulation", "ref": "sim-42", "label": "Supplier outage simulation"},
        ]
        # The simulation ref is stored as cited but stays hidden: Step AL
        # is unmerged, so no reader can be proven able to read it.
        view = await get_decision(db, actor, scope_id=scope_id, decision_id=str(row.id))
        assert view.hidden_evidence == 1 and view.visible_evidence == ()
        # An empty simulation id is refused.
        with pytest.raises(InvalidInputError):
            await record_from_simulation(
                db, actor, scope_id=scope_id, simulation_id="  ", **_capture_body())
        # A member without decision.record is refused (403).
        member = await _principal(db, org_setup["user_id"])
        with pytest.raises(PermissionDeniedError):
            await record_from_simulation(
                db, member, scope_id=scope_id, simulation_id="sim-43", **_capture_body())
        return True

    assert client.portal.call(lambda: _tx(run))
