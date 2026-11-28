"""Forecast-driven proposals (Step L slice 6).

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
the ORM mirror / backfill). Findings come from real slice-1
forecasts (uncovered processes, supplier single point of
failure) and from real calendar warnings (backup overlap).
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.errors import ConflictError, PermissionDeniedError
from app.db.models import User
from app.db.session import test_engine_options as _test_engine_options
from app.predictive_continuity import proposals, proposals_records
from app.predictive_continuity.forecast import forecast_unavailability
from app.predictive_continuity.models import ForecastProposal
from app.predictive_continuity.records import record_scan
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
from tests.test_company_graph import member, tenant, user_id
from tests.test_predictive_continuity_records import IDS, _snapshot

pytestmark = pytest.mark.usefixtures("clean_tables")

NOW = datetime(2026, 11, 28, tzinfo=UTC)
APPROVER_EMAIL = "approver@example.com"
VIEWER_EMAIL = "viewer@example.com"
FUTURE = datetime(2027, 6, 1, tzinfo=UTC)


def run(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _seed_extra_users():
    async def seed(sm):
        async with sm() as db:
            for email in (APPROVER_EMAIL, VIEWER_EMAIL):
                await db.execute(
                    User.__table__.insert().values(
                        email=email, role="user", password_hash=_PASSWORD_HASH))
            await db.commit()

    run(seed)


def _setup(admin_client, *, name="Forecast Proposals Atlas"):
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


def _forecast_for(root_key, *, horizon_days=7):
    return forecast_unavailability(
        _snapshot(), [IDS[root_key]], horizon_days=horizon_days, now=NOW)


def _findings_for(root_key, *, horizon_days=7):
    forecast = _forecast_for(root_key, horizon_days=horizon_days)
    return proposals.findings_from_forecast(forecast, tenant_id=UUID(int=0))


def _uncovered_finding():
    """The pr-3 uncovered-process finding from the p-1 forecast (critical, no money)."""
    findings = _findings_for("p-1")
    return next(f for f in findings if f["code"] == "uncovered_process"
                and f["resource_id"] == IDS["pr-3"])


def _money_finding():
    """The pr-1 uncovered-process finding (critical, PKR 1500).

    The stock snapshot has Bilal recorded as pr-1's backup, so a
    p-1 forecast leaves pr-1 covered. This drops the backup edge:
    pr-1 is then uncovered (no_backup_recorded) with its linked
    March batch (PKR 1500) as money at risk.
    """
    snapshot = _snapshot()
    snapshot["relationships"] = [
        edge for edge in snapshot["relationships"] if edge["id"] != "e-4"]
    forecast = forecast_unavailability(snapshot, [IDS["p-1"]], now=NOW)
    findings = proposals.findings_from_forecast(forecast, tenant_id=UUID(int=0))
    finding = next(f for f in findings if f["money"])
    assert finding["money"] == {"PKR": 1500.0}
    return finding


def _supplier_finding():
    """The supplier SPOF finding from the sup-1 forecast (owner class)."""
    findings = _findings_for("sup-1")
    assert len(findings) == 1 and findings[0]["code"] == "supplier_spof"
    return findings[0]


def _calendar_finding():
    warnings = [{
        "type": "backup_unavailable",
        "process_id": IDS["pr-1"],
        "process_name": "Pay suppliers",
        "backup_person_id": IDS["p-2"],
        "backup_person_name": "Bilal",
        "primary_ids": [IDS["p-1"]],
        "overlap_start": "2026-12-05",
        "overlap_end": "2026-12-09",
        "lead_days": 3,
        "severity": "warning",
    }]
    return proposals.findings_from_calendar(warnings, tenant_id=UUID(int=0))[0]


def _record_forecast(tid, root_key="p-1"):
    forecast = _forecast_for(root_key)
    return run(lambda sm: _record_scan(sm, tid, forecast))


async def _record_scan(sm, tid, forecast):
    async with sm() as db:
        records = await record_scan(db, tid, {
            "scanned_at": NOW.isoformat(),
            "forecasts": [{
                "root_id": IDS["p-1"], "root_kind": "person", "horizon_days": 7,
                "fingerprint": forecast["fingerprint"], "forecast": forecast,
            }],
        }, _snapshot())
        await db.commit()
        return records[0]


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
                    PlatformOutbox.event_type == "forecast.proposal_action"))).all()
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
            return (await db.scalar(select(func.count()).select_from(ForecastProposal))) or 0

    return run(count)


# -------------------------------------------------------------------------------- service

def test_service_create_from_forecast_finding(admin_client):
    _t, tid, principals = _setup(admin_client)
    result = _create(tid, principals, _uncovered_finding(), "forecast.request_backup")
    proposal = result["proposal"]
    assert result["created"] is True
    assert proposal["status"] == "pending_approval"
    assert proposal["action_type"] == "forecast.request_backup"
    assert proposal["required_permission"] == "company_graph.role.approver"
    assert proposal["duty"] == "forecast.apply.coverage"
    assert proposal["approver_class"] == "coverage"
    assert proposal["finding"]["code"] == "uncovered_process"
    approval = _approval_row(result["approval_id"])
    assert approval.required_permission == "company_graph.role.approver"
    assert approval.action_hash == proposal["action_hash"]


def test_service_create_is_idempotent(admin_client):
    _t, tid, principals = _setup(admin_client)
    first = _create(tid, principals, _uncovered_finding(), "forecast.request_backup")
    second = _create(tid, principals, _uncovered_finding(), "forecast.request_backup")
    assert second["created"] is False
    assert second["proposal"]["id"] == first["proposal"]["id"]
    assert _proposal_count() == 1


def test_service_create_from_supplier_finding_is_owner_class(admin_client):
    _t, tid, principals = _setup(admin_client)
    result = _create(tid, principals, _supplier_finding(), "forecast.request_supplier_review")
    proposal = result["proposal"]
    assert proposal["required_permission"] == "company_graph.role.owner"
    assert proposal["duty"] == "forecast.apply"
    assert proposal["approver_class"] == "owner"
    approval = _approval_row(result["approval_id"])
    assert approval.required_permission == "company_graph.role.owner"


def test_service_create_from_calendar_finding(admin_client):
    _t, tid, principals = _setup(admin_client)
    result = _create(tid, principals, _calendar_finding(), "forecast.request_backup_review")
    proposal = result["proposal"]
    assert proposal["source"] == "calendar"
    assert proposal["action_type"] == "forecast.request_backup_review"
    assert proposal["required_permission"] == "company_graph.role.approver"
    assert proposal["finding"]["money"] is None


def test_service_draft_hash_matches_real_actionspec(admin_client):
    _t, tid, principals = _setup(admin_client)
    result = _create(tid, principals, _uncovered_finding(), "forecast.request_backup")
    proposal = result["proposal"]
    spec = ActionSpec(
        proposal["action"]["type"],
        UUID(proposal["action"]["scope_id"]),
        dict(proposal["action"]["params"]),
        amount=None, currency=None, subject=proposal["action"]["subject"])
    assert spec.hash() == proposal["action_hash"]
    assert spec.canonical() == proposal["action"]


def test_service_full_flow_approve_execute_replay(admin_client):
    _t, tid, principals = _setup(admin_client)
    result = _create(tid, principals, _uncovered_finding(), "forecast.request_backup")
    pid = result["proposal"]["id"]
    decided = _decide(tid, principals, pid, by="approver",
                      presented_hash=result["proposal"]["action_hash"])
    assert decided["status"] == "approved"
    executed = _execute(tid, principals, pid, by="user")
    assert executed["proposal"]["status"] == "verified"
    assert executed["execution"]["status"] == "verified"
    assert executed["replayed"] is False
    events = _outbox_for(pid)
    assert len(events) == 1
    assert events[0].payload["action_hash"] == result["proposal"]["action_hash"]
    replayed = _execute(tid, principals, pid, by="user")
    assert replayed["replayed"] is True
    assert replayed["execution"]["status"] == "verified"
    assert len(_outbox_for(pid)) == 1


def test_service_owner_flow_money_proposal(admin_client):
    _t, tid, principals = _setup(admin_client)
    result = _create(tid, principals, _money_finding(), "forecast.request_backup")
    pid = result["proposal"]["id"]
    assert result["proposal"]["finding"]["money"] == {"PKR": 1500.0}
    decided = _decide(tid, principals, pid, by="user",
                      presented_hash=result["proposal"]["action_hash"])
    assert decided["status"] == "approved"
    executed = _execute(tid, principals, pid, by="user2")
    assert executed["proposal"]["status"] == "verified"


def test_service_decide_wrong_hash_is_conflict(admin_client):
    _t, tid, principals = _setup(admin_client)
    result = _create(tid, principals, _uncovered_finding(), "forecast.request_backup")
    with pytest.raises(ConflictError):
        _decide(tid, principals, result["proposal"]["id"], by="approver",
                presented_hash="0" * 64)
    assert _get(tid, result["proposal"]["id"])["status"] == "pending_approval"


def test_service_proposer_cannot_approve(admin_client):
    _t, tid, principals = _setup(admin_client)
    result = _create(tid, principals, _uncovered_finding(), "forecast.request_backup")
    with pytest.raises(PermissionDeniedError):
        _decide(tid, principals, result["proposal"]["id"], by="admin",
                presented_hash=result["proposal"]["action_hash"])


def test_service_approver_cannot_approve_owner_class(admin_client):
    _t, tid, principals = _setup(admin_client)
    result = _create(tid, principals, _supplier_finding(), "forecast.request_supplier_review")
    with pytest.raises(PermissionDeniedError):
        _decide(tid, principals, result["proposal"]["id"], by="approver",
                presented_hash=result["proposal"]["action_hash"])


def test_service_executor_cannot_be_approver_or_proposer(admin_client):
    _t, tid, principals = _setup(admin_client)
    result = _create(tid, principals, _uncovered_finding(), "forecast.request_backup")
    pid = result["proposal"]["id"]
    _decide(tid, principals, pid, by="approver",
            presented_hash=result["proposal"]["action_hash"])
    with pytest.raises(PermissionDeniedError):
        _execute(tid, principals, pid, by="admin")
    with pytest.raises(PermissionDeniedError):
        _execute(tid, principals, pid, by="approver")


def test_service_execute_before_approve_is_conflict(admin_client):
    _t, tid, principals = _setup(admin_client)
    result = _create(tid, principals, _uncovered_finding(), "forecast.request_backup")
    with pytest.raises(ConflictError):
        _execute(tid, principals, result["proposal"]["id"], by="user")


def test_service_expired_approval_marks_proposal_expired(admin_client):
    _t, tid, principals = _setup(admin_client)
    result = _create(tid, principals, _uncovered_finding(), "forecast.request_backup")
    pid = result["proposal"]["id"]

    async def expire(sm):
        async with sm() as db:
            approval = await db.get(PlatformApprovalRequest, UUID(result["approval_id"]))
            approval.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await db.commit()

    run(expire)
    with pytest.raises(ConflictError):
        _decide(tid, principals, pid, by="approver",
                presented_hash=result["proposal"]["action_hash"])
    assert _get(tid, pid)["status"] == "expired"


def test_service_reject_then_repropose(admin_client):
    _t, tid, principals = _setup(admin_client)
    result = _create(tid, principals, _uncovered_finding(), "forecast.request_backup")
    pid = result["proposal"]["id"]
    decided = _decide(tid, principals, pid, by="approver", decision="reject",
                      presented_hash=result["proposal"]["action_hash"])
    assert decided["status"] == "rejected"
    again = _create(tid, principals, _uncovered_finding(), "forecast.request_backup")
    assert again["created"] is True
    assert again["proposal"]["id"] == pid
    assert again["proposal"]["status"] == "pending_approval"
    assert again["approval_id"] != result["approval_id"]


def test_service_cancel_blocks_reproposal(admin_client):
    _t, tid, principals = _setup(admin_client)
    result = _create(tid, principals, _uncovered_finding(), "forecast.request_backup")
    pid = result["proposal"]["id"]

    async def cancel(sm):
        async with sm() as db:
            return await proposals_records.cancel_proposal(
                db, principals["admin"], tid, UUID(pid))

    cancelled = run(cancel)
    assert cancelled["status"] == "cancelled"
    with pytest.raises(ConflictError):
        _create(tid, principals, _uncovered_finding(), "forecast.request_backup")


def test_service_prune_keeps_actionable(admin_client):
    _t, tid, principals = _setup(admin_client)
    pending = _create(tid, principals, _uncovered_finding(), "forecast.request_backup")
    rejected = _create(tid, principals, _supplier_finding(), "forecast.request_supplier_review")
    _decide(tid, principals, rejected["proposal"]["id"], by="user", decision="reject",
            presented_hash=rejected["proposal"]["action_hash"])

    async def prune(sm):
        async with sm() as db:
            return await proposals_records.prune_proposals(
                db, tid, keep=100, max_age_days=90, now=FUTURE)

    assert run(prune) == 1
    assert _get(tid, pending["proposal"]["id"])["status"] == "pending_approval"
    assert _get(tid, rejected["proposal"]["id"]) is None


def test_service_unknown_finding_code_is_value_error(admin_client):
    _t, tid, principals = _setup(admin_client)
    finding = _uncovered_finding()
    finding["code"] = "mystery_kind"
    with pytest.raises(ValueError):
        _create(tid, principals, finding, "forecast.request_backup")


# -------------------------------------------------------------------------------- API

@pytest.fixture
def seed(admin_client):
    _t, tid, principals = _setup(admin_client)
    record = _record_forecast(tid)
    return {"tenant_id": _t, "tid": tid, "record_id": str(record.id)}


def _api_create(client, tenant_id, payload):
    return client.post(f"/api/predictive-continuity/{tenant_id}/proposals", json=payload)


def test_api_create_list_get_counts(client, seed):
    login(client, ADMIN_EMAIL)
    response = _api_create(client, seed["tenant_id"], {
        "forecast_record_id": seed["record_id"],
        "finding_code": "uncovered_process",
        "resource_id": IDS["pr-3"],
        "action_kind": "forecast.request_backup",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True
    assert body["proposal"]["status"] == "pending_approval"
    assert body["proposal"]["finding"]["code"] == "uncovered_process"

    response = client.get(f"/api/predictive-continuity/{seed['tenant_id']}/proposals")
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 1
    response = client.get(f"/api/predictive-continuity/{seed['tenant_id']}/proposals/counts")
    assert response.json()["counts"]["pending_approval"] == 1
    pid = body["proposal"]["id"]
    response = client.get(f"/api/predictive-continuity/{seed['tenant_id']}/proposals/{pid}")
    assert response.status_code == 200, response.text
    assert response.json()["proposal"]["id"] == pid


def test_api_create_from_caller_finding(client, seed):
    login(client, ADMIN_EMAIL)
    response = _api_create(client, seed["tenant_id"], {
        "finding": _calendar_finding(),
        "action_kind": "forecast.request_backup_review",
    })
    assert response.status_code == 200, response.text
    assert response.json()["proposal"]["action_type"] == "forecast.request_backup_review"


def test_api_create_from_record_missing_finding_is_404(client, seed):
    login(client, ADMIN_EMAIL)
    response = _api_create(client, seed["tenant_id"], {
        "forecast_record_id": seed["record_id"],
        "finding_code": "supplier_spof",
        "resource_id": IDS["sup-1"],
        "action_kind": "forecast.request_supplier_review",
    })
    assert response.status_code == 404, response.text


def test_api_create_needs_exactly_one_source(client, seed):
    login(client, ADMIN_EMAIL)
    response = _api_create(client, seed["tenant_id"], {
        "finding": _calendar_finding(),
        "forecast_record_id": seed["record_id"],
        "finding_code": "uncovered_process",
        "resource_id": IDS["pr-3"],
        "action_kind": "forecast.request_backup",
    })
    assert response.status_code in (400, 422), response.text
    response = _api_create(client, seed["tenant_id"], {
        "action_kind": "forecast.request_backup",
    })
    assert response.status_code in (400, 422), response.text


def test_api_full_flow_via_http(client, seed):
    login(client, ADMIN_EMAIL)
    created = _api_create(client, seed["tenant_id"], {
        "forecast_record_id": seed["record_id"],
        "finding_code": "uncovered_process",
        "resource_id": IDS["pr-3"],
        "action_kind": "forecast.request_backup",
    }).json()
    pid = created["proposal"]["id"]
    action_hash = created["proposal"]["action_hash"]
    login(client, APPROVER_EMAIL)
    response = client.post(
        f"/api/predictive-continuity/{seed['tenant_id']}/proposals/{pid}/decide",
        json={"decision": "approve", "presented_hash": action_hash})
    assert response.status_code == 200, response.text
    assert response.json()["proposal"]["status"] == "approved"
    login(client, USER_EMAIL)
    response = client.post(
        f"/api/predictive-continuity/{seed['tenant_id']}/proposals/{pid}/execute")
    assert response.status_code == 200, response.text
    assert response.json()["proposal"]["status"] == "verified"
    assert response.json()["execution"]["status"] == "verified"


def test_api_viewer_money_is_redacted(client, seed):
    login(client, ADMIN_EMAIL)
    created = _api_create(client, seed["tenant_id"], {
        "finding": _money_finding(),
        "action_kind": "forecast.request_backup",
    }).json()
    pid = created["proposal"]["id"]
    assert created["proposal"]["finding"]["money"] == {"PKR": 1500.0}
    assert created["proposal"]["money_redacted"] is False
    login(client, VIEWER_EMAIL)
    response = client.get(
        f"/api/predictive-continuity/{seed['tenant_id']}/proposals/{pid}")
    assert response.status_code == 200, response.text
    proposal = response.json()["proposal"]
    assert proposal["finding"]["money"] is None
    assert proposal["money_redacted"] is True
    response = client.get(f"/api/predictive-continuity/{seed['tenant_id']}/proposals")
    assert response.json()["proposals"][0]["finding"]["money"] is None


def test_api_non_member_and_unknown_ids_get_404(client, seed):
    login(client, ADMIN_EMAIL)
    other = tenant(client, "Other Atlas")
    login(client, VIEWER_EMAIL)
    response = client.get(f"/api/predictive-continuity/{other}/proposals")
    assert response.status_code == 404, response.text
    login(client, ADMIN_EMAIL)
    response = client.get(
        f"/api/predictive-continuity/{seed['tenant_id']}/proposals/{uuid.uuid4()}")
    assert response.status_code == 404, response.text


def test_api_unauthenticated_gets_401(client, seed):
    # The seed fixture logs in as admin on the shared client; drop
    # the session cookie so this request is genuinely unauthenticated.
    client.cookies.clear()
    response = client.get(f"/api/predictive-continuity/{seed['tenant_id']}/proposals")
    assert response.status_code == 401, response.text
