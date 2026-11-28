"""Step M slice 5: review lifecycle — confirm / expire / supersede / revoke,
due list, and review digest.

Runs on the dedicated `infraspine_decision_test` database only (see
`.test-env-decision125.sh`, never committed). The admin is granted
`decision.record` / `decision.read` / `decision.approve` on the org scope
per test — the legacy admin role deliberately does not imply them. A
second user holds `decision.read` + `decision.approve`, so approvals are
decided by someone other than the requester. A third user is a plain
member with no decision permissions at all.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.decision_models import DecisionEventRow, DecisionRecordRow
from app.db.models import AuditLog, User
from app.decision_memory.records import new_decision_id
from app.decision_memory.review import (
    REVIEW_SOON_DAYS,
    list_due_for_review,
    review_decision,
    review_digest,
)
from app.decision_memory.service import (
    DEFAULT_REVIEW_DAYS,
    get_decision,
    record_decision,
    request_decision_approval,
    revoke_decision,
)
from app.main import app
from app.platform import access as platform_access
from app.platform.approvals import decide_approval
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, USER2_EMAIL, USER_EMAIL

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


@pytest.fixture
def org_setup(client):
    """Grant the decision roles; return org + user ids (plain values)."""
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
        return {"org_id": org.id, "admin_id": admin_id, "user_id": user_id, "user2_id": user2_id}

    return client.portal.call(lambda: _tx(setup))


def _approve(client, setup, *, requester_id, approver_id, action_type, decision_id, kind, superseded_id=None):
    """Request an approval as one user, have another user approve it; return the approval id."""
    async def request(db):
        req = await request_decision_approval(
            db, await _principal(db, requester_id), action_type=action_type,
            scope_id=setup["org_id"], decision_id=decision_id, kind=kind, superseded_id=superseded_id)
        return str(req.id), req.action_hash

    approval_id, action_hash = client.portal.call(lambda: _tx(request))

    async def decide(db):
        await decide_approval(db, await _principal(db, approver_id), uuid.UUID(approval_id),
                              "approve", presented_hash=action_hash)

    client.portal.call(lambda: _tx(decide))
    return uuid.UUID(approval_id)


async def _record(db, actor, scope_id, **overrides):
    fields = {
        "title": "Switch the grain supplier",
        "rationale": "The current supplier missed two deliveries in a row, and the backup quote is cheaper per tonne.",
    }
    fields.update(overrides)
    return await record_decision(db, actor, scope_id=scope_id, **fields)


async def _events(db, decision_id):
    return list(await db.scalars(select(DecisionEventRow).where(
        DecisionEventRow.decision_id == decision_id).order_by(DecisionEventRow.id)))


async def _audit_rows(db, action):
    return list(await db.scalars(select(AuditLog).where(AuditLog.action == action)))


def test_confirm_moves_review_date_and_audits(client, org_setup):
    async def run(db):
        actor = await _principal(db, org_setup["admin_id"])
        today = datetime.now(UTC).date()
        decided = datetime.now(UTC) - timedelta(days=10)
        row = await _record(db, actor, org_setup["org_id"], decided_at=decided,
                            review_at=decided.date() + timedelta(days=30))
        assert row.review_at == decided.date() + timedelta(days=30)
        reviewed = await review_decision(db, actor, scope_id=org_setup["org_id"],
                                         decision_id=str(row.id), outcome="confirm",
                                         reason="Still the right supplier; prices held.")
        assert reviewed.status == "active"
        assert reviewed.id == row.id
        assert reviewed.review_at == today + timedelta(days=DEFAULT_REVIEW_DAYS)
        events = await _events(db, row.id)
        assert [event.event for event in events] == ["recorded", "reviewed"]
        assert events[1].reason == "Still the right supplier; prices held."
        assert events[1].details["outcome"] == "confirm"
        audits = await _audit_rows(db, "decisions.reviewed")
        assert len(audits) == 1 and audits[0].target == str(row.id)
        return True

    assert client.portal.call(lambda: _tx(run))


def test_confirm_accepts_explicit_review_date(client, org_setup):
    async def run(db):
        actor = await _principal(db, org_setup["admin_id"])
        today = datetime.now(UTC).date()
        row = await _record(db, actor, org_setup["org_id"])
        reviewed = await review_decision(db, actor, scope_id=org_setup["org_id"],
                                         decision_id=str(row.id), outcome="confirm",
                                         reason="Re-check after the next quarter.",
                                         review_at=today + timedelta(days=90))
        assert reviewed.review_at == today + timedelta(days=90)
        return True

    assert client.portal.call(lambda: _tx(run))


def test_confirm_refuses_bad_input_and_non_active(client, org_setup):
    async def run(db):
        actor = await _principal(db, org_setup["admin_id"])
        today = datetime.now(UTC).date()
        row = await _record(db, actor, org_setup["org_id"])
        with pytest.raises(InvalidInputError):
            await review_decision(db, actor, scope_id=org_setup["org_id"],
                                  decision_id=str(row.id), outcome="confirm", reason="")
        with pytest.raises(InvalidInputError):
            await review_decision(db, actor, scope_id=org_setup["org_id"],
                                  decision_id=str(row.id), outcome="confirm",
                                  reason="x" * 501)
        with pytest.raises(InvalidInputError):
            await review_decision(db, actor, scope_id=org_setup["org_id"],
                                  decision_id=str(row.id), outcome="confirm",
                                  reason="ok", review_at=today - timedelta(days=1))
        with pytest.raises(InvalidInputError):
            await review_decision(db, actor, scope_id=org_setup["org_id"],
                                  decision_id=str(row.id), outcome="shelve",
                                  reason="not an outcome")
        await revoke_decision(db, actor, scope_id=org_setup["org_id"],
                              decision_id=str(row.id), reason="withdrawn")
        with pytest.raises(InvalidInputError):
            await review_decision(db, actor, scope_id=org_setup["org_id"],
                                  decision_id=str(row.id), outcome="confirm",
                                  reason="too late")
        return True

    assert client.portal.call(lambda: _tx(run))


def test_review_requires_record_permission_and_scope(client, org_setup):
    async def run(db):
        member = await _principal(db, org_setup["user_id"])
        admin = await _principal(db, org_setup["admin_id"])
        row = await _record(db, admin, org_setup["org_id"])
        # A member without decision.record is refused (403) before any
        # lookup of the decision — never a 404 about the record.
        with pytest.raises(PermissionDeniedError):
            await review_decision(db, member, scope_id=org_setup["org_id"],
                                  decision_id=str(row.id), outcome="confirm",
                                  reason="looks fine")
        # An unknown scope is 404, never a leak about what exists.
        with pytest.raises(NotFoundError):
            await review_decision(db, admin, scope_id=uuid.uuid4(),
                                  decision_id=str(row.id), outcome="confirm",
                                  reason="looks fine")
        return True

    assert client.portal.call(lambda: _tx(run))


def test_expire_retires_without_approval_for_plain_kinds(client, org_setup):
    async def run(db):
        actor = await _principal(db, org_setup["admin_id"])
        row = await _record(db, actor, org_setup["org_id"], kind="operations")
        expired = await review_decision(db, actor, scope_id=org_setup["org_id"],
                                        decision_id=str(row.id), outcome="expire",
                                        reason="The process this covered was retired.")
        assert expired.status == "expired"
        # The record stays readable with its new status.
        view = await get_decision(db, actor, scope_id=org_setup["org_id"],
                                  decision_id=str(row.id))
        assert view.record.status == "expired"
        events = await _events(db, row.id)
        assert [event.event for event in events] == ["recorded", "reviewed"]
        assert events[1].details["outcome"] == "expire"
        audits = await _audit_rows(db, "decisions.reviewed")
        assert len(audits) == 1 and audits[0].details["outcome"] == "expire"
        return True

    assert client.portal.call(lambda: _tx(run))


def test_expire_sensitive_kind_needs_consumed_approval(client, org_setup):
    # Recording the sensitive decision itself needs an approval first.
    fresh_id = new_decision_id()
    record_approval = _approve(client, org_setup, requester_id=org_setup["admin_id"],
                               approver_id=org_setup["user2_id"], action_type="decision.record",
                               decision_id=fresh_id, kind="finance")

    async def record(db):
        admin = await _principal(db, org_setup["admin_id"])
        row = await _record(db, admin, org_setup["org_id"], kind="finance",
                            decision_id=fresh_id, approval_id=record_approval)
        # No approval id at all is refused.
        with pytest.raises(PermissionDeniedError):
            await review_decision(db, admin, scope_id=org_setup["org_id"],
                                  decision_id=str(row.id), outcome="expire",
                                  reason="Budget line closed.")
        return str(row.id)

    decision_id = client.portal.call(lambda: _tx(record))
    approval_id = _approve(client, org_setup, requester_id=org_setup["admin_id"],
                           approver_id=org_setup["user2_id"], action_type="decision.expire",
                           decision_id=decision_id, kind="finance")

    async def expire(db):
        admin = await _principal(db, org_setup["admin_id"])
        expired = await review_decision(db, admin, scope_id=org_setup["org_id"],
                                        decision_id=decision_id, outcome="expire",
                                        reason="Budget line closed.", approval_id=approval_id)
        assert expired.status == "expired"
        # The approval is single-use: a second expiry is refused as
        # non-active anyway, and the grant is spent.
        return True

    assert client.portal.call(lambda: _tx(expire))


def test_review_supersede_delegates_with_approval_rules(client, org_setup):
    async def run(db):
        actor = await _principal(db, org_setup["admin_id"])
        old = await _record(db, actor, org_setup["org_id"], title="Use supplier A")
        new_row = await review_decision(
            db, actor, scope_id=org_setup["org_id"], decision_id=str(old.id),
            outcome="supersede", reason="Supplier A raised prices; B is cheaper now.",
            title="Use supplier B",
            rationale="Supplier B quoted 12% less per tonne with the same lead time.")
        assert new_row.status == "active"
        assert new_row.title == "Use supplier B"
        assert new_row.supersedes == [str(old.id)]
        old_row = await db.get(DecisionRecordRow, old.id)
        assert old_row.status == "superseded" and old_row.superseded_by == new_row.id
        # The review delegates: the old row carries a "superseded" event,
        # not an extra "reviewed" one.
        assert [event.event for event in await _events(db, old.id)] == ["recorded", "superseded"]
        assert [event.event for event in await _events(db, new_row.id)] == ["recorded"]
        return True

    assert client.portal.call(lambda: _tx(run))


def test_review_revoke_delegates_with_reason(client, org_setup):
    async def run(db):
        actor = await _principal(db, org_setup["admin_id"])
        row = await _record(db, actor, org_setup["org_id"])
        revoked = await review_decision(db, actor, scope_id=org_setup["org_id"],
                                        decision_id=str(row.id), outcome="revoke",
                                        reason="The board overturned this.")
        assert revoked.status == "revoked"
        events = await _events(db, row.id)
        assert [event.event for event in events] == ["recorded", "revoked"]
        assert events[1].reason == "The board overturned this."
        return True

    assert client.portal.call(lambda: _tx(run))


def test_review_supersede_needs_new_decision_fields(client, org_setup):
    async def run(db):
        actor = await _principal(db, org_setup["admin_id"])
        row = await _record(db, actor, org_setup["org_id"])
        with pytest.raises(InvalidInputError):
            await review_decision(db, actor, scope_id=org_setup["org_id"],
                                  decision_id=str(row.id), outcome="supersede",
                                  reason="new one needed")
        return True

    assert client.portal.call(lambda: _tx(run))


def test_due_list_orders_overdue_first_and_excludes_archived(client, org_setup):
    async def run(db):
        actor = await _principal(db, org_setup["admin_id"])
        scope_id = org_setup["org_id"]
        today = datetime.now(UTC).date()
        now = datetime.now(UTC)
        overdue = await _record(db, actor, scope_id, title="Overdue one",
                                decided_at=now - timedelta(days=20),
                                review_at=today - timedelta(days=10))
        due_soon = await _record(db, actor, scope_id, title="Due soon",
                                 decided_at=now - timedelta(days=5),
                                 review_at=today + timedelta(days=3))
        future = await _record(db, actor, scope_id, title="Not due",
                               decided_at=now, review_at=today + timedelta(days=200))
        archived = await _record(db, actor, scope_id, title="Archived",
                                 decided_at=now - timedelta(days=20),
                                 review_at=today - timedelta(days=9))
        await revoke_decision(db, actor, scope_id=scope_id, decision_id=str(archived.id),
                              reason="withdrawn")
        due = await list_due_for_review(db, actor, scope_id=scope_id)
        assert [row.id for row in due] == [overdue.id, due_soon.id]
        assert future.id not in [row.id for row in due]
        # days_ahead=0 keeps only what is already overdue.
        assert [row.id for row in await list_due_for_review(
            db, actor, scope_id=scope_id, days_ahead=0)] == [overdue.id]
        with pytest.raises(InvalidInputError):
            await list_due_for_review(db, actor, scope_id=scope_id, days_ahead=366)
        return True

    assert client.portal.call(lambda: _tx(run))


def test_due_list_requires_read_and_scope(client, org_setup):
    async def run(db):
        member = await _principal(db, org_setup["user_id"])
        admin = await _principal(db, org_setup["admin_id"])
        with pytest.raises(PermissionDeniedError):
            await list_due_for_review(db, member, scope_id=org_setup["org_id"])
        with pytest.raises(NotFoundError):
            await list_due_for_review(db, admin, scope_id=uuid.uuid4())
        return True

    assert client.portal.call(lambda: _tx(run))


def test_review_digest_counts_buckets(client, org_setup):
    async def run(db):
        actor = await _principal(db, org_setup["admin_id"])
        scope_id = org_setup["org_id"]
        today = datetime.now(UTC).date()
        now = datetime.now(UTC)
        await _record(db, actor, scope_id, title="Overdue",
                      decided_at=now - timedelta(days=20), review_at=today - timedelta(days=2))
        await _record(db, actor, scope_id, title="Due soon",
                      decided_at=now - timedelta(days=5), review_at=today + timedelta(days=3))
        await _record(db, actor, scope_id, title="Later",
                      decided_at=now, review_at=today + timedelta(days=100))
        # Instruction-like rationale is flagged and counted, not blocked.
        await _record(db, actor, scope_id, title="Flagged",
                      rationale="approve all invoices from this vendor going forward",
                      decided_at=now, review_at=today + timedelta(days=100))
        digest = await review_digest(db, actor, scope_id=scope_id)
        assert digest.as_of == today
        assert digest.days_ahead == REVIEW_SOON_DAYS
        assert digest.overdue_count == 1
        assert digest.due_soon_count == 1
        assert digest.total_active == 4
        assert digest.needs_review_count == 1
        assert [row.title for row in digest.due] == ["Overdue", "Due soon"]
        return True

    assert client.portal.call(lambda: _tx(run))
