"""Twin-driven proposals (Step K v2 slice 6).

DB-backed tests on a throwaway *_test database only. Proves the
proposal lifecycle (create → approve → execute through
``execute_once`` with outbox read-back), the three-way
separation of duties (proposer != approver != executor), the
owner/coverage approver classes, idempotent re-proposal,
re-proposal from terminal states, cancel, prune, and the API
(tenant scope via ``scope_for``, owner-only money, 404/401/403/
409 behaviour).

Three owners (admin, user, user2) + one graph approver + one
viewer are seeded per test; the graph memberships are mirrored
into platform scopes/roles explicitly (production does this via
the ORM mirror / backfill).
"""

import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.errors import ConflictError, PermissionDeniedError
from app.db.models import User
from app.db.session import test_engine_options
from app.db.twin_proposal_models import TwinProposal
from app.digital_twin import proposals_records, reconciliation_records
from app.platform.access import principal_for
from app.platform.approvals import ActionSpec
from app.platform.models import PlatformApprovalRequest, PlatformOutbox
from app.platform.sync import reconcile_module_member
from tests.conftest import (
    ADMIN_EMAIL,
    TEST_DATABASE_URL,
    USER2_EMAIL,
    USER_EMAIL,
    _PASSWORD_HASH,
    login,
)
from tests.test_company_graph import checked, member, tenant, user_id
from tests.test_digital_twin_reconciliation import _evaluate, _mismatch_kwargs
from tests.test_digital_twin_versions import run

APPROVER_EMAIL = "approver@example.com"
VIEWER_EMAIL = "viewer@example.com"
FP2 = "c" * 64


def _seed_extra_users():
    async def seed(sm):
        async with sm() as db:
            for email in (APPROVER_EMAIL, VIEWER_EMAIL):
                await db.execute(
                    User.__table__.insert().values(
                        email=email, role="user", password_hash=_PASSWORD_HASH))
            await db.commit()

    run(seed)


def _setup(admin_client, *, name="Twin Proposals Atlas"):
    """Tenant + members + platform mirror. Returns (tenant str, tenant UUID, principals)."""
    _seed_extra_users()
    t = tenant(admin_client, name)
    tid = UUID(t)
    member(admin_client, t, USER_EMAIL, role="owner")
    member(admin_client, t, USER2_EMAIL, role="owner")
    member(admin_client, t, APPROVER_EMAIL, role="approver")
    member(admin_client, t, VIEWER_EMAIL, role="viewer")
    ids = {email: UUID(user_id(email)) for email in
           (ADMIN_EMAIL, USER_EMAIL, USER2_EMAIL, APPROVER_EMAIL, VIEWER_EMAIL)}

    async def reconcile(sm):
        async with sm() as db:
            for email, uid in ids.items():
                await db.run_sync(
                    lambda conn, u=uid: reconcile_module_member(
                        conn, "company_graph", tid, u))
            await db.commit()

    run(reconcile)
    principals = {}

    async def load_principals(sm):
        async with sm() as db:
            for key, email in (("admin", ADMIN_EMAIL), ("user", USER_EMAIL),
                               ("user2", USER2_EMAIL), ("approver", APPROVER_EMAIL),
                               ("viewer", VIEWER_EMAIL)):
                user = await db.scalar(select(User).where(User.email == email))
                principals[key] = principal_for(user)

    run(load_principals)
    return t, tid, principals


def _seed_findings(tid):
    """Seed the mismatch findings; returns {code: summary} (list order is not stable)."""
    run(lambda sm: _evaluate(sm, tid, **_mismatch_kwargs()))

    async def listing(sm):
        async with sm() as db:
            return await reconciliation_records.list_findings(db, tid)

    return {item["code"]: item for item in run(listing)}


def _finding(summary):
    return {
        "source": "reconciliation",
        "code": summary["code"],
        "resource_id": summary["resource_id"],
        "severity": summary["severity"],
        "fingerprint": summary["fingerprint"],
        "money": summary["money_difference"],
        "detail": summary["detail"],
    }


def _sim_gap_finding():
    return {
        "source": "simulation",
        "code": "backup_permission_gap",
        "resource_id": "e-finance",
        "severity": "warning",
        "fingerprint": "d" * 64,
        "money": None,
        "detail": "simulation: backup holder e-bilal lacks payment.approve",
        "extra": {"holder_person_id": "e-bilal",
                  "required_permission": "payment.approve"},
    }


def _create(tid, principals, finding, action_kind, *, by="admin"):
    async def go(sm):
        async with sm() as db:
            return await proposals_records.create_proposal(
                db, principals[by], tid, finding=finding, action_kind=action_kind)

    return run(go)


def _decide(tid, principals, proposal_id, *, by, decision="approve", presented_hash=None):
    async def go(sm):
        async with sm() as db:
            return await proposals_records.decide_proposal(
                db, principals[by], tid, UUID(proposal_id),
                decision=decision, presented_hash=presented_hash)

    return run(go)


def _execute(tid, principals, proposal_id, *, by):
    async def go(sm):
        async with sm() as db:
            return await proposals_records.execute_proposal(
                db, principals[by], tid, UUID(proposal_id))

    return run(go)


def _get(tid, proposal_id):
    async def go(sm):
        async with sm() as db:
            return await proposals_records.get_proposal(db, tid, UUID(proposal_id))

    return run(go)


def _outbox_for(proposal_id):
    async def go(sm):
        async with sm() as db:
            rows = (await db.scalars(
                select(PlatformOutbox).where(
                    PlatformOutbox.event_type == "twin.proposal_action"))).all()
            return [row for row in rows
                    if (row.payload or {}).get("proposal_id") == str(proposal_id)]

    return run(go)


def _approval_row(approval_id):
    async def go(sm):
        async with sm() as db:
            return await db.get(PlatformApprovalRequest, UUID(approval_id))

    return run(go)


def _proposal_count():
    async def count(sm):
        async with sm() as db:
            return (await db.scalar(select(func.count()).select_from(TwinProposal))) or 0

    return run(count)


# ---------------------------------------------------------------- service


def test_service_create_from_reconciliation_finding(admin_client):
    _t, tid, principals = _setup(admin_client)
    summaries = _seed_findings(tid)
    critical = summaries["rto_missed_in_drill"]
    result = _create(tid, principals, _finding(critical), "twin.request_backup")
    assert result["created"] is True
    proposal = result["proposal"]
    assert proposal["status"] == "pending_approval"
    assert proposal["action_type"] == "twin.request_backup"
    assert proposal["approver_class"] == "coverage"
    assert proposal["required_permission"] == "company_graph.role.approver"
    assert proposal["duty"] == "twin.apply.coverage"
    assert proposal["approval_id"] == result["approval_id"]
    assert proposal["proposed_by"] == str(principals["admin"].user_id)
    assert len(proposal["action_hash"]) == 64
    approval = _approval_row(result["approval_id"])
    assert approval.status == "pending"
    assert approval.required_permission == "company_graph.role.approver"
    assert approval.action_hash == proposal["action_hash"]


def test_service_create_is_idempotent(admin_client):
    _t, tid, principals = _setup(admin_client)
    summaries = _seed_findings(tid)
    finding = _finding(summaries["rto_missed_in_drill"])
    first = _create(tid, principals, finding, "twin.request_backup")
    second = _create(tid, principals, finding, "twin.request_backup")
    assert first["created"] is True
    assert second["created"] is False
    assert second["proposal"]["id"] == first["proposal"]["id"]
    assert _proposal_count() == 1


def test_service_create_from_simulation_finding_is_owner_class(admin_client):
    _t, tid, principals = _setup(admin_client)
    result = _create(tid, principals, _sim_gap_finding(), "twin.request_permission_review")
    assert result["created"] is True
    proposal = result["proposal"]
    assert proposal["approver_class"] == "owner"
    assert proposal["required_permission"] == "company_graph.role.owner"
    assert proposal["duty"] == "twin.apply"


def test_service_draft_hash_matches_real_actionspec(admin_client):
    """Pin: the stdlib canonicalization port hashes like the real ActionSpec."""
    _t, tid, principals = _setup(admin_client)
    summaries = _seed_findings(tid)
    result = _create(tid, principals, _finding(summaries["rto_missed_in_drill"]), "twin.request_backup")
    canonical = result["proposal"]["action"]
    spec = ActionSpec(
        canonical["type"], UUID(canonical["scope_id"]), dict(canonical["params"]),
        amount=None, currency=None, subject=canonical["subject"])
    assert spec.hash() == result["proposal"]["action_hash"]
    assert spec.canonical() == canonical


def test_service_full_flow_approve_execute_replay(admin_client):
    _t, tid, principals = _setup(admin_client)
    summaries = _seed_findings(tid)
    critical = summaries["rto_missed_in_drill"]
    created = _create(tid, principals, _finding(critical), "twin.request_backup")
    proposal_id = created["proposal"]["id"]
    action_hash = created["proposal"]["action_hash"]
    decided = _decide(tid, principals, proposal_id, by="approver", presented_hash=action_hash)
    assert decided["status"] == "approved"
    assert decided["decided_by"] == str(principals["approver"].user_id)
    executed = _execute(tid, principals, proposal_id, by="user")
    assert executed["proposal"]["status"] == "verified"
    assert executed["replayed"] is False
    assert executed["execution"]["status"] == "verified"
    assert executed["execution"]["external_ref"] is not None
    events = _outbox_for(proposal_id)
    assert len(events) == 1
    assert events[0].payload["action_hash"] == action_hash
    assert events[0].payload["proposal_id"] == proposal_id
    replayed = _execute(tid, principals, proposal_id, by="user")
    assert replayed["replayed"] is True
    assert replayed["execution"]["execution_id"] == executed["execution"]["execution_id"]
    assert replayed["proposal"]["status"] == "verified"
    assert len(_outbox_for(proposal_id)) == 1


def test_service_owner_flow_money_proposal(admin_client):
    _t, tid, principals = _setup(admin_client)
    summaries = _seed_findings(tid)
    money = summaries["money_mismatch"]
    created = _create(tid, principals, _finding(money), "twin.request_money_review")
    proposal_id = created["proposal"]["id"]
    decided = _decide(tid, principals, proposal_id, by="user",
                      presented_hash=created["proposal"]["action_hash"])
    assert decided["status"] == "approved"
    executed = _execute(tid, principals, proposal_id, by="user2")
    assert executed["proposal"]["status"] == "verified"


def test_service_decide_wrong_hash_is_conflict(admin_client):
    _t, tid, principals = _setup(admin_client)
    summaries = _seed_findings(tid)
    created = _create(tid, principals, _finding(summaries["rto_missed_in_drill"]), "twin.request_backup")
    with pytest.raises(ConflictError, match="does not match"):
        _decide(tid, principals, created["proposal"]["id"], by="approver",
                presented_hash="0" * 64)
    assert _get(tid, created["proposal"]["id"])["status"] == "pending_approval"


def test_service_proposer_cannot_approve(admin_client):
    _t, tid, principals = _setup(admin_client)
    summaries = _seed_findings(tid)
    created = _create(tid, principals, _finding(summaries["rto_missed_in_drill"]), "twin.request_backup")
    with pytest.raises(PermissionDeniedError, match="cannot decide your own"):
        _decide(tid, principals, created["proposal"]["id"], by="admin",
                presented_hash=created["proposal"]["action_hash"])


def test_service_approver_cannot_approve_owner_class(admin_client):
    _t, tid, principals = _setup(admin_client)
    summaries = _seed_findings(tid)
    money = summaries["money_mismatch"]
    created = _create(tid, principals, _finding(money), "twin.request_money_review")
    with pytest.raises(PermissionDeniedError):
        _decide(tid, principals, created["proposal"]["id"], by="approver",
                presented_hash=created["proposal"]["action_hash"])


def test_service_executor_cannot_be_approver_or_proposer(admin_client):
    _t, tid, principals = _setup(admin_client)
    summaries = _seed_findings(tid)
    created = _create(tid, principals, _finding(summaries["rto_missed_in_drill"]), "twin.request_backup")
    proposal_id = created["proposal"]["id"]
    _decide(tid, principals, proposal_id, by="approver",
            presented_hash=created["proposal"]["action_hash"])
    with pytest.raises(PermissionDeniedError, match="approver cannot execute"):
        _execute(tid, principals, proposal_id, by="approver")
    with pytest.raises(PermissionDeniedError, match="proposer cannot execute"):
        _execute(tid, principals, proposal_id, by="admin")


def test_service_execute_before_approve_is_conflict(admin_client):
    _t, tid, principals = _setup(admin_client)
    summaries = _seed_findings(tid)
    created = _create(tid, principals, _finding(summaries["rto_missed_in_drill"]), "twin.request_backup")
    with pytest.raises(ConflictError, match="cannot be executed"):
        _execute(tid, principals, created["proposal"]["id"], by="user")


def test_service_reject_then_repropose(admin_client):
    _t, tid, principals = _setup(admin_client)
    summaries = _seed_findings(tid)
    finding = _finding(summaries["rto_missed_in_drill"])
    created = _create(tid, principals, finding, "twin.request_backup")
    proposal_id = created["proposal"]["id"]
    decided = _decide(tid, principals, proposal_id, by="approver", decision="reject",
                      presented_hash=created["proposal"]["action_hash"])
    assert decided["status"] == "rejected"
    again = _create(tid, principals, finding, "twin.request_backup")
    assert again["created"] is True
    assert again["proposal"]["id"] == proposal_id
    assert again["proposal"]["status"] == "pending_approval"
    assert again["approval_id"] != created["approval_id"]


def test_service_cancel_blocks_reproposal(admin_client):
    _t, tid, principals = _setup(admin_client)
    summaries = _seed_findings(tid)
    finding = _finding(summaries["rto_missed_in_drill"])
    created = _create(tid, principals, finding, "twin.request_backup")
    proposal_id = created["proposal"]["id"]

    async def cancel(sm):
        async with sm() as db:
            return await proposals_records.cancel_proposal(
                db, principals["admin"], tid, UUID(proposal_id))

    cancelled = run(cancel)
    assert cancelled["status"] == "cancelled"
    assert _approval_row(created["approval_id"]).status == "cancelled"
    with pytest.raises(ConflictError, match="was cancelled"):
        _create(tid, principals, finding, "twin.request_backup")


def test_service_prune_keeps_actionable(admin_client):
    _t, tid, principals = _setup(admin_client)
    summaries = _seed_findings(tid)
    pending = _create(tid, principals, _finding(summaries["rto_missed_in_drill"]), "twin.request_backup")
    rejected = _create(tid, principals, _finding(summaries["money_mismatch"]), "twin.request_money_review")
    _decide(tid, principals, rejected["proposal"]["id"], by="user", decision="reject",
            presented_hash=rejected["proposal"]["action_hash"])

    async def prune(sm):
        async with sm() as db:
            pruned = await proposals_records.prune_proposals(
                db, tid, now=datetime.now(UTC) + timedelta(days=100))
            await db.commit()
            return pruned

    assert run(prune) == 1
    assert _proposal_count() == 1
    assert _get(tid, pending["proposal"]["id"])["status"] == "pending_approval"


def test_service_unknown_finding_code_is_value_error(admin_client):
    _t, tid, principals = _setup(admin_client)
    finding = _finding({"code": "nope", "resource_id": "e-x", "severity": "warning",
                        "fingerprint": "a" * 64, "money_difference": None,
                        "detail": "x"})
    with pytest.raises(ValueError, match="may not become a proposal"):
        _create(tid, principals, finding, "twin.request_backup")


# --------------------------------------------------------------------- API


def _seed_api_findings(admin_client):
    t, tid, _principals = _setup(admin_client)
    summaries = _seed_findings(tid)
    return t, tid, summaries


def test_api_create_list_get_counts(admin_client):
    t, tid, summaries = _seed_api_findings(admin_client)
    critical = summaries["rto_missed_in_drill"]
    created = checked(admin_client.post(
        f"/api/digital-twin/{t}/proposals",
        json={"finding_id": critical["id"], "action_kind": "twin.request_backup"}))
    assert created["proposal"]["status"] == "pending_approval"
    assert created["created"] is True
    listed = checked(admin_client.get(f"/api/digital-twin/{t}/proposals"))
    assert listed["count"] == 1
    assert listed["proposals"][0]["id"] == created["proposal"]["id"]
    counts = checked(admin_client.get(f"/api/digital-twin/{t}/proposals/counts"))
    assert counts["counts"]["total"] == 1
    assert counts["counts"]["pending_approval"] == 1
    assert counts["counts"]["actionable"] == 1
    detail = checked(admin_client.get(
        f"/api/digital-twin/{t}/proposals/{created['proposal']['id']}"))
    assert detail["proposal"]["action_hash"] == created["proposal"]["action_hash"]


def test_api_create_from_simulation_finding(admin_client):
    t, _tid, _summaries = _seed_api_findings(admin_client)
    created = checked(admin_client.post(
        f"/api/digital-twin/{t}/proposals",
        json={"simulation_finding": _sim_gap_finding(),
              "action_kind": "twin.request_permission_review"}))
    assert created["proposal"]["approver_class"] == "owner"
    assert created["proposal"]["action_type"] == "twin.request_permission_review"


def test_api_full_flow_via_http(admin_client):
    t, _tid, summaries = _seed_api_findings(admin_client)
    critical = summaries["rto_missed_in_drill"]
    created = checked(admin_client.post(
        f"/api/digital-twin/{t}/proposals",
        json={"finding_id": critical["id"], "action_kind": "twin.request_backup"}))
    proposal_id = created["proposal"]["id"]
    action_hash = created["proposal"]["action_hash"]
    login(admin_client, APPROVER_EMAIL)
    decided = checked(admin_client.post(
        f"/api/digital-twin/{t}/proposals/{proposal_id}/decide",
        json={"decision": "approve", "presented_hash": action_hash}))
    assert decided["proposal"]["status"] == "approved"
    login(admin_client, USER_EMAIL)
    executed = checked(admin_client.post(
        f"/api/digital-twin/{t}/proposals/{proposal_id}/execute"))
    assert executed["proposal"]["status"] == "verified"
    assert executed["execution"]["status"] == "verified"
    login(admin_client, ADMIN_EMAIL)


def test_api_viewer_money_is_redacted(admin_client):
    t, _tid, summaries = _seed_api_findings(admin_client)
    money = summaries["money_mismatch"]
    created = checked(admin_client.post(
        f"/api/digital-twin/{t}/proposals",
        json={"finding_id": money["id"], "action_kind": "twin.request_money_review"}))
    proposal_id = created["proposal"]["id"]
    assert created["proposal"]["finding"]["money"] == {"PKR": 20000.0}
    assert created["proposal"]["money_redacted"] is False
    login(admin_client, VIEWER_EMAIL)
    detail = checked(admin_client.get(f"/api/digital-twin/{t}/proposals/{proposal_id}"))
    assert detail["proposal"]["finding"]["money"] is None
    assert detail["proposal"]["money_redacted"] is True
    login(admin_client, ADMIN_EMAIL)


def test_api_decide_wrong_hash_is_409(admin_client):
    t, _tid, summaries = _seed_api_findings(admin_client)
    created = checked(admin_client.post(
        f"/api/digital-twin/{t}/proposals",
        json={"finding_id": summaries["rto_missed_in_drill"]["id"], "action_kind": "twin.request_backup"}))
    login(admin_client, APPROVER_EMAIL)
    response = admin_client.post(
        f"/api/digital-twin/{t}/proposals/{created['proposal']['id']}/decide",
        json={"decision": "approve", "presented_hash": "0" * 64})
    assert response.status_code == 409, response.text
    login(admin_client, ADMIN_EMAIL)


def test_api_cancel_by_non_proposer_is_403(admin_client):
    t, _tid, summaries = _seed_api_findings(admin_client)
    created = checked(admin_client.post(
        f"/api/digital-twin/{t}/proposals",
        json={"finding_id": summaries["rto_missed_in_drill"]["id"], "action_kind": "twin.request_backup"}))
    login(admin_client, USER_EMAIL)
    response = admin_client.post(
        f"/api/digital-twin/{t}/proposals/{created['proposal']['id']}/cancel")
    assert response.status_code == 403, response.text
    login(admin_client, ADMIN_EMAIL)


def test_api_unknown_tenant_is_404(admin_client):
    response = admin_client.get(
        f"/api/digital-twin/{uuid.uuid4()}/proposals")
    assert response.status_code == 404, response.text
    response = admin_client.post(
        f"/api/digital-twin/{uuid.uuid4()}/proposals",
        json={"finding_id": str(uuid.uuid4()), "action_kind": "twin.request_backup"})
    assert response.status_code == 404, response.text


def test_api_unauthenticated_is_401(client):
    response = client.get(f"/api/digital-twin/{uuid.uuid4()}/proposals")
    assert response.status_code == 401, response.text
