"""Step AA slice 3: scheduled report worker — due claiming, quiet hours, grace
window, dedupe, permission-scoped bodies, retry with backoff."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import platform
from app.db.finance_models import FinInvoice, FinInvoiceBatch, FinPayment
from app.db.models import AccessGroup, User
from app.db.report_schedule_models import ReportDelivery, ReportScheduleRow
from app.db.session import test_engine_options as _test_engine_options
from app.platform.sync import group_scope_sync
from app.services.scheduled_report_worker import (
    backoff_delay,
    deliver_pending_report_deliveries,
    process_scheduled_reports,
)
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, USER2_EMAIL, USER_EMAIL

pytestmark = pytest.mark.usefixtures("clean_tables")

PKT = ZoneInfo("Asia/Karachi")


def run_async(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _at(hour, minute=0, day=28):
    return datetime(2026, 11, day, hour, minute, tzinfo=PKT)


async def _setup(sm, *, recipients="user", template="daily_cash_summary",
                 channels=("in_app", "whatsapp"), cadence="daily", weekday=0,
                 overdue_days=7):
    """One Finance department the recipient may read, with invoices/payments.

    Invoices (PKR): 100000 pending 10 days old (overdue), 50000 pending 2 days
    old, 20000 verified. One USD invoice (100). Payments: 30000 verified,
    25000 awaiting approval 10 days old (overdue).
    """
    async with sm() as db:
        admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
        recipient_email = USER_EMAIL if recipients == "user" else USER2_EMAIL
        recipient = await db.scalar(select(User).where(User.email == recipient_email))
        group = AccessGroup(name="Finance", kind="department", source="local")
        db.add(group)
        await db.flush()
        scope_id = await db.run_sync(
            lambda s: group_scope_sync(s.connection(), group.id))
        if recipients == "user":
            # Only the "user" recipient may read Finance; "user2" must not, so
            # the worker has nothing to report to them.
            await platform.ensure_role(db, "finance.viewer", {"finance.read"})
            await platform.grant_membership(
                db, platform.principal_for(admin), user_id=recipient.id,
                scope_id=scope_id, role_key="finance.viewer")
        batch = FinInvoiceBatch(group_id=group.id, name="B1", created_by=admin.id,
                               currency="PKR", status="open")
        db.add(batch)
        await db.flush()
        today = date(2026, 11, 28)
        old, recent = today - timedelta(days=10), today - timedelta(days=2)
        for seq, (status, amount, currency, when) in enumerate([
            ("pending", Decimal("100000"), "PKR", old),
            ("pending", Decimal("50000"), "PKR", recent),
            ("verified", Decimal("20000"), "PKR", recent),
            ("pending", Decimal("100"), "USD", recent),
        ], start=1):
            db.add(FinInvoice(group_id=group.id, batch_id=batch.id, seq=seq,
                              invoice_number=f"INV-{seq:04d}", status=status,
                              amount=amount, currency=currency, invoice_date=when,
                              source_kind="json", source_digest=f"digest-{seq}"))
        from app.db.finance_models import FinVendor
        vendor = FinVendor(group_id=group.id, name="Alpha Supplies", name_key="alpha supplies",
                           bank_account="PK00ALPHA0001", currency="PKR")
        db.add(vendor)
        await db.flush()
        for status, amount, created in [
            ("verified", Decimal("30000"), datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)),
            ("awaiting_approval", Decimal("25000"), datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)),
        ]:
            db.add(FinPayment(group_id=group.id, batch_id=batch.id, vendor_id=vendor.id,
                              amount=amount, currency="PKR", payee_account="PK00ALPHA0001",
                              invoice_ids=[], status=status, created_at=created))
        schedule = ReportScheduleRow(
            template=template, cadence=cadence, time_of_day=_at(8, 0).time(),
            timezone="Asia/Karachi", weekday=weekday, channels=list(channels),
            recipients=[str(recipient.id)], quiet_start=_at(22, 0).time(),
            quiet_end=_at(7, 0).time(), overdue_days=overdue_days, enabled=True,
            created_by=admin.id)
        db.add(schedule)
        await db.commit()
        return {"schedule_id": schedule.id, "recipient_id": recipient.id,
                "group_id": group.id, "batch_id": batch.id}


async def _deliveries(sm, **filters):
    async with sm() as db:
        query = select(ReportDelivery)
        for key, value in filters.items():
            query = query.where(getattr(ReportDelivery, key) == value)
        return list((await db.scalars(query)).all())


async def _schedule(sm, schedule_id):
    async with sm() as db:
        row = await db.get(ReportScheduleRow, schedule_id)
        return row.last_sent_at


def test_worker_sends_in_app_and_retries_whatsapp_without_sender():
    async def go(sm):
        s = await _setup(sm)
        handled = await process_scheduled_reports(sm, now=_at(8, 5))
        assert handled == 1
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        by_channel = {row.channel: row for row in rows}
        assert by_channel["in_app"].status == "sent"
        assert by_channel["in_app"].sent_at is not None
        whatsapp = by_channel["whatsapp"]
        assert whatsapp.status == "retry"
        assert whatsapp.last_error_code == "channel_not_configured"
        assert whatsapp.next_attempt_at is not None
        assert whatsapp.attempts == 1
        last_sent = await _schedule(sm, s["schedule_id"])
        assert last_sent == _at(8, 0)

    run_async(go)


def test_worker_does_not_send_the_same_period_twice():
    async def go(sm):
        s = await _setup(sm)
        assert await process_scheduled_reports(sm, now=_at(8, 5)) == 1
        assert await process_scheduled_reports(sm, now=_at(8, 6)) == 0
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        assert len(rows) == 2  # one per channel, no duplicates

    run_async(go)


def test_worker_skips_quiet_hours_and_long_missed_occurrences():
    async def go(sm):
        s = await _setup(sm)
        assert await process_scheduled_reports(sm, now=_at(23, 0)) == 0
        assert await process_scheduled_reports(sm, now=_at(10, 0)) == 0  # past the 1h grace
        assert await _deliveries(sm, schedule_id=s["schedule_id"]) == []
        assert await _schedule(sm, s["schedule_id"]) is None

    run_async(go)


def test_worker_delivers_via_callback_with_correct_totals_and_retries_on_failure():
    calls = []

    async def flaky_deliver(delivery, schedule, recipient, subject, body):
        calls.append((delivery.channel, subject, body, delivery.id))
        if len(calls) == 1:
            raise RuntimeError("boom")

    async def go(sm):
        s = await _setup(sm, channels=("whatsapp",))
        assert await process_scheduled_reports(sm, now=_at(8, 5), deliver=flaky_deliver) == 1
        assert len(calls) == 1
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        assert len(rows) == 1 and rows[0].status == "retry"
        assert rows[0].last_error_code == "RuntimeError"
        assert rows[0].attempts == 1
        # The worker passes the delivery row itself, so the callback can use
        # the delivery id as its exactly-once key.
        assert calls[0][3] == rows[0].id
        body = calls[0][2]
        assert "Total invoiced: 170000.00 PKR" in body
        assert "Total paid (verified): 30000.00 PKR" in body
        assert "Total invoiced: 100.00 USD" in body
        assert "Overdue: 1 invoice(s), 1 payment(s) awaiting approval" in body

        sent = []

        async def good_deliver(delivery, schedule, recipient, subject, body):
            sent.append((delivery.channel, body))

        # Too early: the retry is backed off for 15 minutes.
        assert await deliver_pending_report_deliveries(sm, now=_at(8, 10), deliver=good_deliver) == 0
        assert await deliver_pending_report_deliveries(sm, now=_at(8, 25), deliver=good_deliver) == 1
        assert len(sent) == 1 and sent[0][0] == "whatsapp"
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        assert rows[0].status == "sent" and rows[0].sent_at is not None

    run_async(go)


def test_worker_sends_nothing_without_finance_read_but_advances_the_period():
    async def go(sm):
        s = await _setup(sm, recipients="user2")  # USER2 has no finance.read on Finance
        assert await process_scheduled_reports(sm, now=_at(8, 5)) == 1
        assert await _deliveries(sm, schedule_id=s["schedule_id"]) == []
        assert await _schedule(sm, s["schedule_id"]) == _at(8, 0)

    run_async(go)


def test_retry_loop_marks_failed_after_max_attempts():
    async def go(sm):
        s = await _setup(sm, channels=("whatsapp",))
        await process_scheduled_reports(sm, now=_at(8, 5))  # no sender -> retry, attempts=1
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        assert len(rows) == 1

        async def failing(delivery, schedule, recipient, subject, body):
            raise RuntimeError("still down")

        # Fast-forward past each backoff (which caps at 6 hours), one retry at a time.
        now = _at(8, 5)
        for _ in range(7):
            now = now + timedelta(hours=7)
            attempted = await deliver_pending_report_deliveries(sm, now=now, deliver=failing)
            assert attempted == 1
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        assert rows[0].status == "failed" and rows[0].attempts == 8
        assert rows[0].next_attempt_at is None
        # A failed delivery is never attempted again.
        assert await deliver_pending_report_deliveries(
            sm, now=now + timedelta(days=1), deliver=failing) == 0

    run_async(go)


async def _set_weekly_dates(sm):
    """Pin created/verified dates so the weekly window is deterministic.

    The fixture inserts rows without created_at, so the database stamps them
    with the real clock; the weekly builder needs known dates instead.
    """
    async with sm() as db:
        invoices = list((await db.scalars(
            select(FinInvoice).order_by(FinInvoice.seq))).all())
        for row, day in zip(invoices, (24, 25, 26, 27)):
            row.created_at = datetime(2026, 11, day, 9, 0, tzinfo=timezone.utc)
        payments = list((await db.scalars(select(FinPayment))).all())
        for row in payments:
            if row.status == "verified":
                row.verified_at = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)
        await db.commit()


def test_weekly_template_sends_with_totals():
    async def go(sm):
        # 2026-11-28 is a Saturday (weekday 5); the weekly occurrence is today 08:00.
        # in_app never calls the deliver callback (the delivery record itself
        # is the notification), so whatsapp is included for the body capture.
        s = await _setup(sm, template="weekly_invoices", cadence="weekly", weekday=5)
        await _set_weekly_dates(sm)
        seen = []

        async def capture(delivery, schedule, recipient, subject, body):
            seen.append((subject, body))

        assert await process_scheduled_reports(
            sm, now=_at(8, 5), deliver=capture) == 1
        assert len(seen) == 1
        subject, body = seen[0]
        assert subject == "Weekly invoices — 2026-11-28"
        assert "Weekly invoices — 2026-11-22 to 2026-11-28 (PKR)" in body
        assert "New invoices: 3 — 170000.00 PKR" in body
        assert "Paid this week (verified): 1 — 30000.00 PKR" in body
        assert "Weekly invoices — 2026-11-22 to 2026-11-28 (USD)" in body
        assert "New invoices: 1 — 100.00 USD" in body
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        assert len(rows) == 2 and all(row.status == "sent" for row in rows)

    run_async(go)


def test_overdue_alert_sends_only_when_something_is_overdue():
    async def go(sm):
        # in_app never calls the deliver callback (see the weekly test).
        s = await _setup(sm, template="overdue_alert")
        seen = []

        async def capture(delivery, schedule, recipient, subject, body):
            seen.append((subject, body))

        assert await process_scheduled_reports(
            sm, now=_at(8, 5), deliver=capture) == 1
        assert len(seen) == 1
        subject, body = seen[0]
        assert subject == "Overdue alert — 2026-11-28"
        assert "Overdue alert — 2026-11-28 (PKR)" in body
        assert "Overdue: 2 item(s) — 125000.00 PKR" in body
        assert "INV-0001" in body and "10d overdue" in body
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        assert len(rows) == 2 and all(row.status == "sent" for row in rows)

        # Nothing overdue past 30 days: no delivery, but the period advances.
        # A second _setup would create a second "Finance" department, which
        # the unique (kind, name) constraint forbids in one test DB — so the
        # second schedule is added directly to the same setup.
        async with sm() as db:
            admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
            second = ReportScheduleRow(
                template="overdue_alert", cadence="daily", time_of_day=_at(8, 0).time(),
                timezone="Asia/Karachi", weekday=0, channels=["in_app"],
                recipients=[str(s["recipient_id"])], quiet_start=_at(22, 0).time(),
                quiet_end=_at(7, 0).time(), overdue_days=30, enabled=True,
                created_by=admin.id)
            db.add(second)
            await db.commit()
            second_id = second.id
        assert await process_scheduled_reports(sm, now=_at(8, 5)) == 1
        assert await _deliveries(sm, schedule_id=second_id) == []
        assert await _schedule(sm, second_id) == _at(8, 0)

    run_async(go)


def test_backoff_delay_caps_at_six_hours():
    assert backoff_delay(1) == timedelta(minutes=15)
    assert backoff_delay(2) == timedelta(minutes=30)
    assert backoff_delay(10) == timedelta(hours=6)
