"""Step C acceptance: the shared platform core (identity, scopes, permissions, approvals, exactly-once execution,
revocation and organisation time)."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, literal, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import platform
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.security import hash_session_token
from app.db.agent_studio_models import AgentStudioAgent
from app.db.models import User, UserSession
from app.db.session import test_engine_options
from app.platform import ActionSpec, VerifiedResult
from app.platform import timezone as org_time
from app.platform.models import (
    PlatformApprovalRequest, PlatformChannelIdentity, PlatformOutbox, PlatformApprovalRequest as Approval,
)
from app.services.approval_delegation import DelegationService, active_delegators
from app.services.audit import AuditContext
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, USER2_EMAIL, USER_EMAIL

pytestmark = pytest.mark.usefixtures("clean_tables")
EXTRA = ("treasury@example.com", "auditor@example.com")


def run(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


async def principal(db, email, channel="web"):
    user = await db.scalar(select(User).where(User.email == email))
    return platform.Principal(user.id, user.email, "user", channel, user.auth_version)


async def setup(sm):
    """org > finance department > payables workspace; a second department; roles for the three finance duties."""
    async with sm() as db:
        for email in EXTRA:
            db.add(User(email=email, role="user", password_hash="x"))
        await db.flush()
        admin = await principal(db, ADMIN_EMAIL)
        org = await platform.ensure_org_scope(db)
        finance = await platform.create_scope(db, kind="department", parent_id=org.id, name="Finance")
        payables = await platform.create_scope(db, kind="workspace", parent_id=finance.id, name="Payables")
        hr = await platform.create_scope(db, kind="department", parent_id=org.id, name="HR")
        await platform.ensure_role(db, "finance_officer", {"invoice.verify", "finance.read"})
        await platform.ensure_role(db, "finance_manager", {"payment.approve", "finance.read"})
        await platform.ensure_role(db, "treasury", {"funds.transfer", "finance.read"})
        await platform.ensure_role(db, "multi_duty", {"invoice.verify", "payment.approve", "funds.transfer"})
        ids = {}
        for email, role in ((USER_EMAIL, "finance_officer"), (USER2_EMAIL, "finance_manager"),
                            (EXTRA[0], "treasury"), (EXTRA[1], "multi_duty")):
            user_id = await db.scalar(select(User.id).where(User.email == email))
            ids[email] = user_id
            await platform.grant_membership(db, admin, user_id=user_id, scope_id=finance.id, role_key=role)
        await db.commit()
        return {"org": org.id, "finance": finance.id, "payables": payables.id, "hr": hr.id, "ids": ids}


def payment(scope_id, amount="125000.00", subject="invoice:INV-204"):
    return ActionSpec("payment.transfer", scope_id, {"payee": "North Star Supplies", "invoice_ids": ["INV-204"]},
                      amount=Decimal(amount), currency="pkr", subject=subject)


# ------------------------------------------------------------------------------------------- scope and permissions

def test_scope_inheritance_404_vs_403_visible_scopes_and_effective_permissions():
    async def go(sm):
        s = await setup(sm)
        async with sm() as db:
            officer = await principal(db, USER_EMAIL)
            assert (await platform.require(db, officer, "invoice.verify", s["payables"])).name == "Payables"
            with pytest.raises(PermissionDeniedError):
                await platform.require(db, officer, "payment.approve", s["finance"])
            with pytest.raises(NotFoundError):  # no membership in HR: not revealed
                await platform.require(db, officer, "invoice.verify", s["hr"])
            visible = await platform.visible_scopes(db, officer, "invoice.verify")
            assert visible.scope_ids == {s["finance"], s["payables"]}
            assert await platform.effective_permissions(db, s["ids"][USER_EMAIL], s["payables"]) == {"invoice.verify", "finance.read"}
            admin = await principal(db, ADMIN_EMAIL)
            # the legacy admin role keeps platform administration but never business duties
            assert (await platform.authorize(db, admin, "platform.manage", s["hr"])).allowed
            assert not (await platform.authorize(db, admin, "funds.transfer", s["finance"])).allowed
            transfer = await platform.authorize(db, await principal(db, EXTRA[0]), "funds.transfer", s["finance"])
            assert transfer.allowed and transfer.requires_approval and transfer.approval_permission == "payment.approve"

    run(go)


def test_time_limited_membership_stops_at_valid_to():
    async def go(sm):
        s = await setup(sm)
        async with sm() as db:
            admin = await principal(db, ADMIN_EMAIL)
            auditor_id = s["ids"][EXTRA[1]]
            hr_member = await platform.grant_membership(db, admin, user_id=auditor_id, scope_id=s["hr"],
                                                        role_key="finance_officer", valid_to=datetime.now(UTC) + timedelta(hours=1))
            await db.commit()
            auditor = await principal(db, EXTRA[1])
            assert (await platform.authorize(db, auditor, "invoice.verify", s["hr"])).allowed
            hr_member.valid_to = datetime.now(UTC) - timedelta(seconds=1)
            hr_member.valid_from = datetime.now(UTC) - timedelta(hours=2)
            await db.commit()
            assert (await platform.authorize(db, auditor, "invoice.verify", s["hr"])).reason == "not_a_member"

    run(go)


def test_principals_resolve_from_session_verified_channel_identity_and_available_agent_only():
    async def go(sm):
        s = await setup(sm)
        async with sm() as db:
            user_id = s["ids"][USER_EMAIL]
            db.add(UserSession(user_id=user_id, token_hash=hash_session_token("tok-1"),
                               expires_at=datetime.now(UTC) + timedelta(hours=1)))
            db.add(PlatformChannelIdentity(user_id=user_id, channel="whatsapp_cloud", external_id="923001234567",
                                           verified_at=datetime.now(UTC), verification_method="dashboard_code"))
            db.add(PlatformChannelIdentity(user_id=s["ids"][USER2_EMAIL], channel="whatsapp_cloud",
                                           external_id="923009999999"))  # not verified
            agent = AgentStudioAgent(owner_id=user_id, name="Helper", instructions="x", tool_permissions=[],
                                     approval_tools=[], delegate_agent_ids=[], max_steps=2)
            db.add(agent)
            await db.commit()
            web = await platform.resolve_principal(db, session_token="tok-1")
            assert (web.user_id, web.channel, web.session_id is not None) == (user_id, "web", True)
            mobile = await platform.resolve_principal(db, channel="whatsapp_cloud", external_id="923001234567")
            assert mobile.user_id == user_id
            # the same permissions on the mobile channel as on the dashboard
            assert await platform.authorize(db, mobile, "invoice.verify", s["finance"]) == \
                await platform.authorize(db, web, "invoice.verify", s["finance"])
            with pytest.raises(PermissionDeniedError):
                await platform.resolve_principal(db, channel="whatsapp_cloud", external_id="923009999999")
            with pytest.raises(PermissionDeniedError):
                await platform.resolve_principal(db, channel="whatsapp_cloud", external_id="920000000000")
            acting = await platform.resolve_principal(db, agent_id=agent.id)
            assert acting.kind == "agent" and acting.user_id == user_id
            agent.paused = True
            await db.commit()
            assert (await platform.authorize(db, acting, "invoice.verify", s["finance"])).reason == "agent_unavailable"
            with pytest.raises(PermissionDeniedError):
                await platform.resolve_principal(db, agent_id=agent.id)

    run(go)


# ------------------------------------------------------------------------------------------- approvals

def test_approval_is_bound_to_the_exact_action_expires_and_is_single_use():
    async def go(sm):
        s = await setup(sm)
        async with sm() as db:
            treasury = await principal(db, EXTRA[0])
            manager = await principal(db, USER2_EMAIL)
            action = payment(s["finance"])
            request = await platform.request_approval(db, treasury, action, required_permission="payment.approve")
            request_id = request.id
            assert request.action_hash == action.hash() and len(request.action_hash) == 64
            assert payment(s["finance"], "125000.0").hash() == action.hash()  # canonical amount
            assert payment(s["finance"], "125001.00").hash() != action.hash()

            with pytest.raises(ConflictError, match="does not match"):
                await platform.decide_approval(db, manager, request.id, "approve", presented_hash="0" * 64)
            with pytest.raises(PermissionDeniedError):  # the requester lacks payment.approve
                await platform.decide_approval(db, treasury, request.id, "approve", presented_hash=request.action_hash)
            approved = await platform.decide_approval(db, manager, request.id, "approve", presented_hash=request.action_hash)
            assert approved.status == "approved"

            with pytest.raises(ConflictError, match="different action"):
                await platform.consume_approval(db, request.id, payment(s["finance"], "999999.00"))
            await db.rollback()
            grant = await platform.consume_approval(db, request_id, action)
            await db.commit()
            assert grant.approved_by == manager.user_id
            with pytest.raises(ConflictError, match="already used"):
                await platform.consume_approval(db, request_id, action)
            await db.rollback()

            stale = await platform.request_approval(db, treasury, payment(s["finance"], subject="invoice:INV-205"),
                                                    required_permission="payment.approve")
            await db.execute(update(Approval).where(Approval.id == stale.id)
                             .values(expires_at=func.now() - timedelta(seconds=1)))
            await db.commit()
            with pytest.raises(ConflictError, match="expired"):
                await platform.decide_approval(db, manager, stale.id, "approve", presented_hash=stale.action_hash)
            assert (await db.get(Approval, stale.id, populate_existing=True)).status == "expired"

    run(go)


def test_requester_cannot_decide_own_request_and_one_person_holds_one_duty_per_invoice():
    async def go(sm):
        s = await setup(sm)
        async with sm() as db:
            multi = await principal(db, EXTRA[1])  # holds all three duties by role
            own = await platform.request_approval(db, multi, payment(s["finance"]), required_permission="payment.approve")
            with pytest.raises(PermissionDeniedError, match="your own request"):
                await platform.decide_approval(db, multi, own.id, "approve", presented_hash=own.action_hash)

            verify = ActionSpec("invoice.verify", s["finance"], {"invoice_id": "INV-300"}, subject="invoice:INV-300")
            result = await platform.execute_once(db, multi, "verify:INV-300", verify, duty="invoice.verify",
                                                 dispatch=_ok_dispatch, verify=_ok_verify)
            assert result.status == "verified"
            treasury = await principal(db, EXTRA[0])
            pay = payment(s["finance"], subject="invoice:INV-300")
            request = await platform.request_approval(db, treasury, pay, required_permission="payment.approve")
            with pytest.raises(PermissionDeniedError, match="Separation of duties"):
                await platform.decide_approval(db, multi, request.id, "approve", presented_hash=request.action_hash)

            manager = await principal(db, USER2_EMAIL)
            await platform.decide_approval(db, manager, request.id, "approve", presented_hash=request.action_hash)
            with pytest.raises(PermissionDeniedError):  # transfer needs an approval
                await platform.execute_once(db, treasury, "transfer:INV-300:x", pay, duty="funds.transfer",
                                            dispatch=_ok_dispatch, verify=_ok_verify)
            with pytest.raises(PermissionDeniedError, match="Separation of duties"):  # the verifier cannot transfer
                await platform.execute_once(db, multi, "transfer:INV-300:y", pay, duty="funds.transfer",
                                            dispatch=_ok_dispatch, verify=_ok_verify, approval_id=request.id)
            done = await platform.execute_once(db, treasury, "transfer:INV-300", pay, duty="funds.transfer",
                                               dispatch=_ok_dispatch, verify=_ok_verify, approval_id=request.id)
            assert done.status == "verified"
            assert (await db.get(Approval, request.id, populate_existing=True)).status == "consumed"

    run(go)


async def _ok_dispatch():
    return "ext-1"


async def _ok_verify(ref):
    return VerifiedResult(True, {"ref": ref, "state": "posted"})


# ------------------------------------------------------------------------------------------- execute_once

def test_execute_once_dispatches_once_under_concurrency_and_replays_the_verified_result():
    async def go(sm):
        s = await setup(sm)
        dispatched = []

        async def dispatch():
            dispatched.append(1)
            await asyncio.sleep(0.2)
            return f"bank-ref-{len(dispatched)}"

        async def verify(ref):
            return VerifiedResult(True, {"ref": ref})

        action = ActionSpec("invoice.verify", s["finance"], {"invoice_id": "INV-400"}, subject="invoice:INV-400")

        async def attempt():
            async with sm() as db:
                officer = await principal(db, USER_EMAIL)
                return await platform.execute_once(db, officer, "whatsapp:msg-400", action, duty="invoice.verify",
                                                   dispatch=dispatch, verify=verify)

        results = await asyncio.gather(*(attempt() for _ in range(6)))
        assert len(dispatched) == 1
        assert len({r.execution_id for r in results}) == 1
        assert sum(not r.replayed for r in results) == 1
        again = await attempt()
        assert (again.status, again.external_ref, again.replayed) == ("verified", "bank-ref-1", True)
        assert len(dispatched) == 1
        async with sm() as db:
            officer = await principal(db, USER_EMAIL)
            other = ActionSpec("invoice.verify", s["finance"], {"invoice_id": "INV-401"}, subject="invoice:INV-401")
            with pytest.raises(ConflictError, match="different action"):
                await platform.execute_once(db, officer, "whatsapp:msg-400", other, duty="invoice.verify",
                                            dispatch=dispatch, verify=verify)

    run(go)


def test_a_crash_after_dispatch_is_resumed_by_verifying_not_by_dispatching_again():
    async def go(sm):
        s = await setup(sm)
        dispatched, checks = [], []

        async def dispatch():
            dispatched.append(1)
            return "erp-77"

        async def flaky_verify(ref):
            checks.append(ref)
            if len(checks) == 1:
                raise TimeoutError("read-back unavailable")
            return VerifiedResult(True, {"ref": ref})

        action = ActionSpec("invoice.verify", s["finance"], {"invoice_id": "INV-500"}, subject="invoice:INV-500")
        async with sm() as db:
            officer = await principal(db, USER_EMAIL)
            first = await platform.execute_once(db, officer, "wf:run-1:item-33", action, duty="invoice.verify",
                                                dispatch=dispatch, verify=flaky_verify)
            assert first.status == "dispatched"
            second = await platform.execute_once(db, officer, "wf:run-1:item-33", action, duty="invoice.verify",
                                                 dispatch=dispatch, verify=flaky_verify)
            assert (second.status, second.replayed) == ("verified", True)
            assert dispatched == [1] and checks == ["erp-77", "erp-77"]

            async def failing_dispatch():
                raise ConnectionError("target down")

            failed = await platform.execute_once(
                db, officer, "wf:run-1:item-34",
                ActionSpec("invoice.verify", s["finance"], {"invoice_id": "INV-501"}, subject="invoice:INV-501"),
                duty="invoice.verify", dispatch=failing_dispatch, verify=flaky_verify)
            assert failed.status == "failed"

            async def wrong(ref):
                return VerifiedResult(False, {"state": "not found"})

            unverified = await platform.execute_once(
                db, officer, "wf:run-1:item-35",
                ActionSpec("invoice.verify", s["finance"], {"invoice_id": "INV-502"}, subject="invoice:INV-502"),
                duty="invoice.verify", dispatch=dispatch, verify=wrong)
            assert unverified.status == "failed" and unverified.verified_result == {"state": "not found"}

    run(go)


# ------------------------------------------------------------------------------------------- revocation

def test_revoke_user_access_stops_sessions_channels_principals_memberships_and_requests():
    async def go(sm):
        s = await setup(sm)
        async with sm() as db:
            user_id = s["ids"][USER_EMAIL]
            db.add(UserSession(user_id=user_id, token_hash=hash_session_token("tok-r"),
                               expires_at=datetime.now(UTC) + timedelta(hours=1)))
            db.add(PlatformChannelIdentity(user_id=user_id, channel="whatsapp_baileys", external_id="923001111111",
                                           verified_at=datetime.now(UTC)))
            await db.commit()
            held = await platform.resolve_principal(db, session_token="tok-r")  # e.g. held by a running workflow
            pending_id = (await platform.request_approval(db, held, payment(s["finance"]),
                                                          required_permission="payment.approve")).id
            officer = await principal(db, USER2_EMAIL)
            with pytest.raises(NotFoundError):  # a non-admin cannot revoke (org scope not revealed)
                await platform.revoke_user_access(db, officer, user_id, "test")
            await db.rollback()
            admin = await principal(db, ADMIN_EMAIL)
            counts = await platform.revoke_user_access(db, admin, user_id, "left the company")
            assert counts == {"memberships": 1, "sessions": 1, "channel_identities": 1, "pending_approvals": 1}
            assert (await platform.authorize(db, held, "invoice.verify", s["finance"])).reason == "principal_revoked"
            with pytest.raises(PermissionDeniedError):
                await platform.resolve_principal(db, session_token="tok-r")
            with pytest.raises(PermissionDeniedError):
                await platform.resolve_principal(db, channel="whatsapp_baileys", external_id="923001111111")
            assert (await db.get(PlatformApprovalRequest, pending_id, populate_existing=True)).status == "cancelled"
            fresh = await principal(db, USER_EMAIL)
            assert (await platform.authorize(db, fresh, "invoice.verify", s["finance"])).reason == "not_a_member"
            events = list(await db.scalars(select(PlatformOutbox.event_type).order_by(PlatformOutbox.id)))
            assert "identity.session_revoked" in events
            seen = []

            async def handler(event):
                seen.append(event.event_type)

            assert await platform.drain_outbox(db, handler) == len(events)
            assert seen == events and await platform.drain_outbox(db, handler) == 0

    run(go)


# ------------------------------------------------------------------------------------------- organisation time

def test_org_today_defaults_to_karachi_and_delegation_is_active_at_0030_pkt(monkeypatch):
    half_past_midnight_pkt = datetime(2026, 11, 25, 19, 30, tzinfo=UTC)  # 2026-11-26 00:30 in Asia/Karachi
    monkeypatch.setattr(org_time, "_utcnow", lambda: half_past_midnight_pkt)

    async def go(sm):
        async with sm() as db:
            assert await platform.org_today(db) == date(2026, 11, 26)  # no settings row: default Asia/Karachi
            at = literal(half_past_midnight_pkt)
            assert await db.scalar(select(platform.org_today_sql(at))) == date(2026, 11, 26)
            delegator = await db.scalar(select(User).where(User.email == USER2_EMAIL))
            delegate = await db.scalar(select(User).where(User.email == USER_EMAIL))
            service = DelegationService(db, delegator, AuditContext(delegator.id, delegator.email, None, None))
            await service.create(delegate_user_id=delegate.id, starts_on=date(2026, 11, 26),
                                 ends_on=date(2026, 11, 26), reason="Leave")
            # under UTC days it would still be 25 November and the delegation would not be active yet
            assert [u.email for u in await active_delegators(db, delegate.id)] == [USER2_EMAIL]
            assert await active_delegators(db, delegate.id, date(2026, 11, 25)) == []
            await platform.set_org_time_zone(db, "UTC")
            await db.commit()
            assert await platform.org_today(db) == date(2026, 11, 25)
            assert await db.scalar(select(platform.org_today_sql(at))) == date(2026, 11, 25)

    run(go)
