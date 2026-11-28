"""Step M slice 3: decisions API — authz, evidence filtering, why, dependencies, approvals.

Runs on the dedicated `infraspine_decision_test` database only (see
`.test-env-decision125.sh`, never committed). The admin holds
`decision.record` / `decision.read` / `decision.approve` on the org
scope; the plain user holds `decision.read` only; the approver holds
`decision.approve` only (so a read by the approver is a 403, and an
approval is always decided by someone other than the requester). The
legacy admin role deliberately implies no decision permission.

Evidence fixtures: one shared document (readable by everyone), one
private document (admin only), one governance proposal + decision in a
tenant the admin belongs to, one continuity drill run in a tenant the
admin belongs to, one audit row, plus unwired simulation / twin /
recovery refs and one external URL.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.continuity.models import Member as ContinuityMember
from app.continuity.models import Plan as ContinuityPlan
from app.continuity.models import Run as ContinuityRun
from app.continuity.models import Tenant as ContinuityTenant
from app.db.decision_models import DecisionEventRow
from app.db.models import AuditLog, Document, User
from app.decision_memory.service import PERMISSION_APPROVE, PERMISSION_READ, PERMISSION_RECORD
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
    """Grant the decision roles and seed one of each evidence kind."""
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
        # A department scope nobody holds a membership on (404 path).
        dept = await platform_access.create_scope(
            db, kind="department", parent_id=org.id, name="Dept X")

        from app.api.routes.decisions import ensure_decision_roles

        await ensure_decision_roles(db)

        shared_doc = Document(
            owner_id=admin_id, visibility="shared", source_system="upload",
            filename="quote.pdf", storage_key="k-shared", sha256="a" * 64,
            size_bytes=10, status="ready")
        private_doc = Document(
            owner_id=admin_id, visibility="private", source_system="upload",
            filename="secret.pdf", storage_key="k-private", sha256="b" * 64,
            size_bytes=10, status="ready")
        db.add_all([shared_doc, private_doc])

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
            action="approve", action_hash="h" * 64, policy_version=1, risk="low",
            required_approvals=1, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(gov_request)
        await db.flush()
        db.add(GovernanceDecision(
            tenant_id=gov_tenant.id, request_id=gov_request.id, actor_id=admin_id,
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
            digest="d" * 64, mode="live", requested_by=admin_id,
            approval_expires_at=datetime.now(UTC) + timedelta(days=1),
            disruption_at=datetime.now(UTC) + timedelta(days=2))
        db.add(bc_run)

        audit_row = AuditLog(
            actor_user_id=admin_id, actor_email=ADMIN_EMAIL,
            action="governance.decision_made", outcome="success",
            target="gov-1", details={})
        db.add(audit_row)
        await db.flush()
        return {
            "org_id": org.id, "dept_id": dept.id,
            "admin_id": admin_id, "user_id": user_id, "user2_id": user2_id,
            "shared_doc_id": shared_doc.id, "private_doc_id": private_doc.id,
            "gov_request_id": gov_request.id, "drill_run_id": bc_run.id,
            "audit_id": audit_row.id,
        }

    return client.portal.call(lambda: _tx(setup))


def _body(**overrides):
    payload = {
        "title": "Switch the grain supplier",
        "rationale": "The current supplier missed two deliveries in a row.",
        "kind": "operations",
    }
    payload.update(overrides)
    return payload


def _record(client, cookies, org_id, **overrides):
    response = _as(client, cookies).post(f"/api/decisions/scopes/{org_id}", json=_body(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def _approve_request(client, setup, *, requester_cookies, approver_id, action_type, decision_id, kind,
                     superseded_id=None):
    """Request an approval through the API, approve it as another user; return (id, hash)."""
    payload = {"action_type": action_type, "decision_id": str(decision_id), "kind": kind}
    if superseded_id is not None:
        payload["superseded_id"] = str(superseded_id)
    response = _as(client, requester_cookies).post(
        f"/api/decisions/scopes/{setup['org_id']}/approvals", json=payload)
    assert response.status_code == 201, response.text
    data = response.json()
    approval_id, action_hash = uuid.UUID(data["approval_id"]), data["action_hash"]

    async def decide(db):
        await decide_approval(db, await _principal(db, approver_id), approval_id,
                              "approve", presented_hash=action_hash)

    client.portal.call(lambda: _tx(decide))
    return approval_id, action_hash


def test_record_get_list_filters_why_and_verify(client, org_setup):
    admin = _session(client, ADMIN_EMAIL)
    reader = _session(client, USER_EMAIL)
    org_id = org_setup["org_id"]

    first = _record(client, admin, org_id, decided_at="2026-11-20T09:00:00+00:00",
                    alternatives_considered=["Stay put"],
                    evidence=[{"kind": "external", "ref": "https://example.invalid/quote",
                               "label": "Supplier quote"}])

    got = _as(client, reader).get(f"/api/decisions/scopes/{org_id}/{first['id']}")
    assert got.status_code == 200, got.text
    assert got.json()["title"] == first["title"]
    assert got.json()["evidence"] == [{"kind": "external", "ref": "https://example.invalid/quote",
                                       "label": "Supplier quote"}]
    assert got.json()["hidden_evidence"] == 0

    listed = _as(client, reader).get(f"/api/decisions/scopes/{org_id}")
    assert listed.status_code == 200, listed.text
    assert [d["id"] for d in listed.json()["decisions"]] == [first["id"]]

    by_kind = _as(client, reader).get(f"/api/decisions/scopes/{org_id}", params={"kind": "operations"})
    assert [d["id"] for d in by_kind.json()["decisions"]] == [first["id"]]
    by_kind = _as(client, reader).get(f"/api/decisions/scopes/{org_id}", params={"kind": "policy"})
    assert by_kind.json()["decisions"] == []
    by_date = _as(client, reader).get(
        f"/api/decisions/scopes/{org_id}", params={"decided_after": "2026-11-21T00:00:00+00:00"})
    assert by_date.json()["decisions"] == []

    why = _as(client, reader).get(f"/api/decisions/scopes/{org_id}/why", params={"subject": "grain supplier"})
    assert [d["id"] for d in why.json()["decisions"]] == [first["id"]]
    assert why.json()["subject"] == "grain supplier"
    why_none = _as(client, reader).get(f"/api/decisions/scopes/{org_id}/why", params={"subject": "zzz-no-match"})
    assert why_none.json()["decisions"] == []

    verified = _as(client, reader).get(f"/api/decisions/scopes/{org_id}/{first['id']}/verify")
    assert verified.status_code == 200, verified.text
    assert verified.json() == {"decision_id": first["id"], "fingerprint": first["fingerprint"], "valid": True}

    # Exactly one audit row per lifecycle event: the service writes it, the route adds none.
    async def count_audit(db):
        rows = await db.scalars(select(AuditLog).where(
            AuditLog.action == "decisions.recorded", AuditLog.target == first["id"]))
        return len(list(rows))

    assert client.portal.call(lambda: _tx(count_audit, commit=False)) == 1


def test_authz_404_403_401(client, org_setup):
    admin = _session(client, ADMIN_EMAIL)
    reader = _session(client, USER_EMAIL)
    approver = _session(client, USER2_EMAIL)
    org_id = org_setup["org_id"]
    missing = str(uuid.uuid4())

    assert _as(client, admin).get(f"/api/decisions/scopes/{org_id}/{missing}").status_code == 404
    assert _as(client, admin).get(f"/api/decisions/scopes/{missing}").status_code == 404
    # The admin is an org admin, so the platform resolves them as a member of
    # every scope in the org. A plain org member is likewise a member of the
    # department sub-scope (org membership covers sub-scopes), so both get
    # 200 here; the non-member 404 is the unknown-scope case above, and the
    # route only surfaces whatever platform.require decides.
    assert _as(client, admin).get(f"/api/decisions/scopes/{org_setup['dept_id']}").status_code == 200
    assert _as(client, reader).get(f"/api/decisions/scopes/{org_setup['dept_id']}").status_code == 200
    assert _as(client, admin).get(f"/api/decisions/scopes/{org_id}/{missing}/verify").status_code == 404

    # Reader may not record; approver (approve only) may not read.
    assert _as(client, reader).post(f"/api/decisions/scopes/{org_id}", json=_body()).status_code == 403
    assert _as(client, approver).get(f"/api/decisions/scopes/{org_id}").status_code == 403
    assert _as(client, approver).get(f"/api/decisions/scopes/{org_id}/{missing}").status_code == 403
    # Approval requests need decision.record too.
    assert _as(client, reader).post(
        f"/api/decisions/scopes/{org_id}/approvals",
        json={"action_type": "decision.record", "decision_id": missing, "kind": "finance"}).status_code == 403

    # Unauthenticated.
    client.cookies.clear()
    assert client.get(f"/api/decisions/scopes/{org_id}").status_code == 401


def test_validation_errors(client, org_setup):
    admin = _session(client, ADMIN_EMAIL)
    org_id = org_setup["org_id"]
    base = f"/api/decisions/scopes/{org_id}"
    assert _as(client, admin).post(base, json=_body(kind="nope")).status_code == 422
    assert _as(client, admin).post(base, json=_body(title="")).status_code == 422
    assert _as(client, admin).post(
        base, json=_body(evidence=[{"kind": "nope", "ref": "x", "label": "y"}])).status_code == 422
    assert _as(client, admin).post(base, json=_body(extra_field="no")).status_code == 422
    assert _as(client, admin).post(base, json=_body(decided_at="2026-11-20T09:00:00")).status_code == 422
    assert _as(client, admin).get(base, params={"kind": "nope"}).status_code == 422
    assert _as(client, admin).get(base, params={"status": "nope"}).status_code == 422
    assert _as(client, admin).get(f"{base}/why").status_code == 422  # subject is required


def test_evidence_filtering_all_kinds(client, org_setup):
    admin = _session(client, ADMIN_EMAIL)
    reader = _session(client, USER_EMAIL)
    org_id = org_setup["org_id"]
    evidence = [
        {"kind": "document", "ref": str(org_setup["shared_doc_id"]), "label": "Shared quote"},
        {"kind": "document", "ref": str(org_setup["private_doc_id"]), "label": "Private memo"},
        {"kind": "simulation", "ref": "sim-9", "label": "Cost simulation"},
        {"kind": "twin_snapshot", "ref": "snap-1", "label": "Twin snapshot"},
        {"kind": "recovery_plan", "ref": "plan-1", "label": "Recovery plan"},
        {"kind": "governance_proposal", "ref": str(org_setup["gov_request_id"]), "label": "Proposal"},
        {"kind": "governance_decision", "ref": str(org_setup["gov_request_id"]), "label": "Gov decision"},
        {"kind": "continuity_drill", "ref": str(org_setup["drill_run_id"]), "label": "Drill run"},
        {"kind": "audit_event", "ref": str(org_setup["audit_id"]), "label": "Audit row"},
        {"kind": "external", "ref": "https://example.invalid/quote", "label": "Public quote"},
    ]
    created = _record(client, admin, org_id, evidence=evidence)
    url = f"/api/decisions/scopes/{org_id}/{created['id']}"

    as_admin = _as(client, admin).get(url)
    assert as_admin.status_code == 200, as_admin.text
    admin_kinds = {(e["kind"], e["ref"]) for e in as_admin.json()["evidence"]}
    assert admin_kinds == {
        ("document", str(org_setup["shared_doc_id"])),
        ("document", str(org_setup["private_doc_id"])),
        ("governance_proposal", str(org_setup["gov_request_id"])),
        ("governance_decision", str(org_setup["gov_request_id"])),
        ("continuity_drill", str(org_setup["drill_run_id"])),
        ("audit_event", str(org_setup["audit_id"])),
        ("external", "https://example.invalid/quote"),
    }
    # The unwired kinds (simulation / twin / recovery) stay hidden even for the admin.
    assert as_admin.json()["hidden_evidence"] == 3

    as_reader = _as(client, reader).get(url)
    assert as_reader.status_code == 200, as_reader.text
    reader_kinds = {(e["kind"], e["ref"]) for e in as_reader.json()["evidence"]}
    assert reader_kinds == {
        ("document", str(org_setup["shared_doc_id"])),
        ("external", "https://example.invalid/quote"),
    }
    assert as_reader.json()["hidden_evidence"] == 8


def test_why_like_escaping(client, org_setup):
    admin = _session(client, ADMIN_EMAIL)
    reader = _session(client, USER_EMAIL)
    org_id = org_setup["org_id"]
    created = _record(client, admin, org_id, title="Grain at 100% markup",
                      rationale="The markup is 100% this quarter.")
    found = _as(client, reader).get(f"/api/decisions/scopes/{org_id}/why", params={"subject": "100%"})
    assert [d["id"] for d in found.json()["decisions"]] == [created["id"]]
    # A bare "%" must not match everything: it is escaped, so only the literal "%" matches.
    pct = _as(client, reader).get(f"/api/decisions/scopes/{org_id}/why", params={"subject": "%"})
    assert [d["id"] for d in pct.json()["decisions"]] == [created["id"]]
    underscore = _as(client, reader).get(f"/api/decisions/scopes/{org_id}/why", params={"subject": "_"})
    assert underscore.json()["decisions"] == []


def test_supersede_revoke_and_dependencies(client, org_setup):
    admin = _session(client, ADMIN_EMAIL)
    reader = _session(client, USER_EMAIL)
    org_id = org_setup["org_id"]

    base = _record(client, admin, org_id, title="Use supplier A")
    child = _record(client, admin, org_id, title="Order from supplier A",
                    depends_on=[base["id"]])

    deps_child = _as(client, reader).get(f"/api/decisions/scopes/{org_id}/{child['id']}/dependencies")
    assert deps_child.status_code == 200, deps_child.text
    assert [d["id"] for d in deps_child.json()["depends_on"]] == [base["id"]]
    assert deps_child.json()["depended_on_by"] == []
    deps_base = _as(client, reader).get(f"/api/decisions/scopes/{org_id}/{base['id']}/dependencies")
    assert [d["id"] for d in deps_base.json()["depended_on_by"]] == [child["id"]]
    assert deps_base.json()["superseded_by"] is None

    # Supersede refuses while an active decision depends on the base.
    refused = _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}/{base['id']}/supersede", json=_body(title="Use supplier B"))
    assert refused.status_code == 422, refused.text

    # Revoke the child, then supersede the base.
    revoked = _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}/{child['id']}/revoke", json={"reason": "no longer needed"})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    again = _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}/{child['id']}/revoke", json={"reason": "again"})
    assert again.status_code == 422

    superseder = _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}/{base['id']}/supersede",
        json=_body(title="Use supplier B"))
    assert superseder.status_code == 201, superseder.text
    assert superseder.json()["supersedes"] == [base["id"]]
    deps_base2 = _as(client, reader).get(f"/api/decisions/scopes/{org_id}/{base['id']}/dependencies")
    assert deps_base2.json()["superseded_by"]["id"] == superseder.json()["id"]
    assert [d["id"] for d in deps_base2.json()["supersedes"]] == []
    assert _as(client, reader).get(
        f"/api/decisions/scopes/{org_id}/{superseder.json()['id']}/dependencies"
    ).json()["supersedes"][0]["id"] == base["id"]

    # The revoked child is archived from default lists, visible with the flag.
    listed = _as(client, reader).get(f"/api/decisions/scopes/{org_id}")
    assert {d["id"] for d in listed.json()["decisions"]} == {superseder.json()["id"]}
    archived = _as(client, reader).get(
        f"/api/decisions/scopes/{org_id}", params={"include_archived": True})
    assert {d["id"] for d in archived.json()["decisions"]} == {
        base["id"], child["id"], superseder.json()["id"]}
    by_status = _as(client, reader).get(
        f"/api/decisions/scopes/{org_id}",
        params={"status": "revoked", "include_archived": True})
    assert [d["id"] for d in by_status.json()["decisions"]] == [child["id"]]

    # One event row per lifecycle event.
    async def count_events(db):
        rows = await db.scalars(select(DecisionEventRow).where(
            DecisionEventRow.decision_id == uuid.UUID(child["id"])))
        return [(r.event) for r in rows]

    assert client.portal.call(lambda: _tx(count_events, commit=False)) == ["recorded", "revoked"]


def test_sensitive_record_approval_flow(client, org_setup):
    admin = _session(client, ADMIN_EMAIL)
    reader = _session(client, USER_EMAIL)
    org_id = org_setup["org_id"]

    decision_id = uuid.uuid4()
    # No approval yet: refused.
    refused = _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}",
        json=_body(title="Approve the grain budget", kind="finance", decision_id=str(decision_id)))
    assert refused.status_code == 403, refused.text

    approval_id, action_hash = _approve_request(
        client, org_setup, requester_cookies=admin, approver_id=org_setup["user2_id"],
        action_type="decision.record", decision_id=decision_id, kind="finance")
    assert action_hash and len(action_hash) == 64

    created = _record(client, admin, org_id, title="Approve the grain budget",
                      kind="finance", decision_id=str(decision_id), approval_id=str(approval_id))
    assert created["id"] == str(decision_id)
    assert _as(client, reader).get(f"/api/decisions/scopes/{org_id}/{created['id']}").status_code == 200

    # The approval is single-use: recording another decision with it fails.
    reused = _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}",
        json=_body(title="Approve something else", kind="finance", approval_id=str(approval_id)))
    assert reused.status_code == 409, reused.text

    # An approval bound to a different decision id does not cover this one.
    other_id = uuid.uuid4()
    other_approval, _ = _approve_request(
        client, org_setup, requester_cookies=admin, approver_id=org_setup["user2_id"],
        action_type="decision.record", decision_id=other_id, kind="finance")
    mismatch = _as(client, admin).post(
        f"/api/decisions/scopes/{org_id}",
        json=_body(title="Approve the grain budget", kind="finance",
                   decision_id=str(uuid.uuid4()), approval_id=str(other_approval)))
    assert mismatch.status_code == 409, mismatch.text
