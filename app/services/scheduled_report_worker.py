"""Scheduled report worker (Step AA, slice 3): due-schedule poller.

The pattern mirrors ``app/services/approval_reminders.py``: poll, claim with
SKIP LOCKED, build, send, record the delivery, retry with backoff. It runs
inside the existing worker process (``app/worker.py``).

Channels: ``in_app`` is always available — the delivery record itself is the
notification (slice 5 shows it in the UI). WhatsApp/email go through the
``deliver`` callback (slice 4, ``app.services.scheduled_report_delivery``):
the callback receives the delivery row first —
``deliver(delivery, schedule_row, recipient, subject, body)`` — and returns
the provider reference (WhatsApp message id, email Message-ID) or ``None``
for ``in_app``. A typed :class:`ReportDeliveryError` from the callback is
recorded as ``retry`` with the error type as the code and backed off, never
silently dropped.

Reports are read-only summaries. The worker only reads finance rows the
recipient may read (``finance.read``, group-scoped, via ``FinanceService``)
and never executes a payment, changes an invoice, or approves anything.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.finance_models import FinInvoice, FinPayment
from app.db.models import User
from app.db.report_schedule_models import ReportDelivery, ReportScheduleRow
from app.finance.service import FinanceService
from app.reports.persistence import schedule_from_row
from app.reports.scheduled import (
    TEMPLATES,
    InvoiceRow,
    PaymentRow,
    build_daily_cash_summary,
    build_overdue_alert,
    build_weekly_invoices_summary,
    previous_due,
    render_daily_cash_summary_text,
    render_overdue_alert_text,
    render_weekly_invoices_text,
    should_send,
)
from app.services.audit import AuditContext, record

logger = logging.getLogger(__name__)
REPORT_POLL_SECONDS = 300
REPORT_CLAIM_SECONDS = 300
REPORT_MAX_ATTEMPTS = 8
REPORT_BATCH_SIZE = 50
_WORKER = AuditContext(None, "scheduled-report-worker", None, None)

# A delivery for one (schedule, period, channel, recipient) is never created
# twice while an earlier attempt is still pending, sending, sent or retrying.
_OPEN_DELIVERY = ("pending", "sending", "sent", "retry")


def backoff_delay(attempt: int) -> timedelta:
    """Retry backoff: 15 minutes doubling, capped at 6 hours (same as approval emails)."""
    return timedelta(seconds=min(6 * 3600, 15 * 60 * 2 ** max(0, attempt - 1)))


def _subject_for(template: str, period: datetime, timezone: str) -> str:
    """Email/WhatsApp subject for one occurrence (the period's local date)."""
    local_date = period.astimezone(ZoneInfo(timezone)).date()
    if template == "weekly_invoices":
        return f"Weekly invoices — {local_date.isoformat()}"
    if template == "overdue_alert":
        return f"Overdue alert — {local_date.isoformat()}"
    return f"Daily cash summary — {local_date.isoformat()}"


async def _build_body(db: AsyncSession, schedule: ReportScheduleRow, recipient: User,
                      period: datetime) -> str | None:
    """Render the report body for one recipient, or None when there is nothing to send.

    Only rows in departments the recipient may read (``finance.read``) are
    included. Amounts are grouped per currency and never converted. The
    period is the scheduled occurrence: the daily report covers that date,
    the weekly report covers the seven days ending on it, the overdue alert lists
    what is stuck as of it. An overdue alert with nothing overdue sends
    nothing; a weekly report with no new invoices and no open exceptions
    sends nothing either.
    """
    from app.db.finance_models import FinVendor

    service = FinanceService(db, recipient, None, None)
    groups = await service.readable_groups()
    if not groups:
        return None
    invoices = list((await db.scalars(select(FinInvoice).where(
        FinInvoice.group_id.in_(groups)))).all())
    payments = list((await db.scalars(select(FinPayment).where(
        FinPayment.group_id.in_(groups)))).all())
    currencies = sorted({row.currency for row in invoices + payments if row.currency})
    if not currencies:
        return None
    vendors = {row.id: row.name for row in (await db.scalars(select(FinVendor).where(
        FinVendor.group_id.in_(groups)))).all()}
    invoice_rows = [
        InvoiceRow(status=row.status, amount=row.amount, currency=row.currency,
                   invoice_date=row.invoice_date, created_at=row.created_at,
                   invoice_ref=row.invoice_number, vendor_name=row.vendor_name)
        for row in invoices
    ]
    payment_rows = [
        PaymentRow(status=row.status, amount=row.amount, currency=row.currency,
                   created_at=row.created_at, verified_at=row.verified_at,
                   vendor_name=vendors.get(row.vendor_id))
        for row in payments
    ]
    as_of = period.astimezone(ZoneInfo(schedule.timezone)).date()
    parts = []
    for currency in currencies:
        if schedule.template == "weekly_invoices":
            # The weekly report covers the seven days ending on the firing day.
            summary = build_weekly_invoices_summary(
                invoice_rows, payment_rows, week_start=as_of - timedelta(days=6),
                week_end=as_of, currency=currency)
            if summary.new_count or summary.exceptions_open_count:
                parts.append(render_weekly_invoices_text(summary))
        elif schedule.template == "overdue_alert":
            alert = build_overdue_alert(
                invoice_rows, payment_rows, as_of=as_of, currency=currency,
                overdue_days=schedule.overdue_days)
            if alert.total_count:
                parts.append(render_overdue_alert_text(alert))
        else:
            summary = build_daily_cash_summary(
                invoice_rows, payment_rows, as_of=as_of, currency=currency,
                overdue_days=schedule.overdue_days)
            parts.append(render_daily_cash_summary_text(summary))
    return "\n\n".join(parts) if parts else None


async def _process_schedule(sessionmaker: async_sessionmaker[AsyncSession], schedule_id: uuid.UUID,
                            *, now: datetime, deliver) -> int:
    """Process one claimed schedule. Returns 1 when an occurrence was handled, else 0."""
    async with sessionmaker() as db:
        row = await db.get(ReportScheduleRow, schedule_id, with_for_update=True)
        if row is None or not row.enabled:
            if row is not None:
                row.claimed_until = None
                await db.commit()
            return 0
        schedule = schedule_from_row(row)
        if not should_send(schedule, now):
            row.claimed_until = None
            await db.commit()
            return 0
        period = previous_due(schedule, now)
        if schedule.template not in TEMPLATES:
            # Unreachable through the API (the table check-constrains the
            # template), but never invent a report body for an unknown one.
            row.last_sent_at, row.claimed_until = period, None
            record(db, _WORKER, "reports.skipped", target=str(row.id),
                   details={"template": schedule.template, "reason": "unknown_template"})
            await db.commit()
            return 1
        sent, failed, skipped = 0, 0, 0
        for recipient_id in schedule.recipients:
            try:
                recipient_uuid = uuid.UUID(str(recipient_id))
            except ValueError:
                skipped += 1
                continue
            recipient = await db.get(User, recipient_uuid)
            if recipient is None or not recipient.is_active:
                skipped += 1
                continue
            body = await _build_body(db, row, recipient, period)
            if body is None:
                skipped += 1
                continue
            subject = _subject_for(schedule.template, period, schedule.timezone)
            for channel in schedule.channels:
                existing = await db.scalar(select(ReportDelivery).where(
                    ReportDelivery.schedule_id == row.id,
                    ReportDelivery.period_start == period,
                    ReportDelivery.channel == channel,
                    ReportDelivery.recipient_user_id == recipient.id,
                    ReportDelivery.status.in_(_OPEN_DELIVERY),
                ))
                if existing is not None:
                    skipped += 1
                    continue
                delivery = ReportDelivery(
                    schedule_id=row.id, period_start=period, channel=channel,
                    recipient_user_id=recipient.id, status="sending",
                    claimed_until=now + timedelta(seconds=REPORT_CLAIM_SECONDS),
                )
                db.add(delivery)
                await db.flush()
                if channel == "in_app":
                    # The delivery record itself is the in-app notification.
                    delivery.status, delivery.sent_at = "sent", now
                    delivery.claimed_until, delivery.next_attempt_at = None, None
                    sent += 1
                elif deliver is None:
                    delivery.status = "retry"
                    delivery.attempts = 1
                    delivery.last_error_code = "channel_not_configured"
                    delivery.next_attempt_at = now + backoff_delay(1)
                    delivery.claimed_until = None
                    failed += 1
                else:
                    try:
                        ref = await deliver(delivery, row, recipient, subject, body)
                    except Exception as exc:  # noqa: BLE001 - retry on any sender failure
                        logger.warning("Scheduled report delivery failed (%s)", type(exc).__name__)
                        delivery.status = "retry"
                        delivery.attempts = 1
                        delivery.last_error_code = type(exc).__name__[:80]
                        delivery.next_attempt_at = now + backoff_delay(1)
                        delivery.claimed_until = None
                        failed += 1
                    else:
                        delivery.status, delivery.sent_at = "sent", now
                        delivery.provider_ref = ref
                        delivery.claimed_until, delivery.next_attempt_at = None, None
                        sent += 1
        row.last_sent_at, row.claimed_until = period, None
        record(db, _WORKER, "reports.sent", target=str(row.id),
               details={"template": schedule.template, "period": period.isoformat(),
                        "sent": sent, "failed": failed, "skipped": skipped})
        await db.commit()
        return 1


async def process_scheduled_reports(
    sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime | None = None,
    deliver=None, batch_size: int = REPORT_BATCH_SIZE,
) -> int:
    """Claim due schedules and process them; returns how many occurrences were handled.

    ``deliver`` is an async callable ``(delivery, schedule_row, recipient,
    subject, body)`` for WhatsApp/email sends (slice 4,
    ``app.services.scheduled_report_delivery.make_report_deliver``); it
    returns the provider reference or ``None`` for ``in_app``. With
    ``deliver=None`` those channels are recorded as ``retry``; ``in_app``
    is always delivered.
    """
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("The scheduled report worker requires a timezone-aware timestamp.")
    async with sessionmaker() as db:
        due = (await db.execute(
            select(ReportScheduleRow)
            .where(ReportScheduleRow.enabled.is_(True),
                   or_(ReportScheduleRow.claimed_until.is_(None),
                       ReportScheduleRow.claimed_until <= current))
            .order_by(ReportScheduleRow.created_at, ReportScheduleRow.id)
            .limit(batch_size)
            .with_for_update(of=ReportScheduleRow, skip_locked=True)
        )).scalars().all()
        ids = [row.id for row in due]
        for row in due:
            row.claimed_until = current + timedelta(seconds=REPORT_CLAIM_SECONDS)
        await db.commit()
    handled = 0
    for schedule_id in ids:
        handled += await _process_schedule(sessionmaker, schedule_id, now=current, deliver=deliver)
    return handled


async def deliver_pending_report_deliveries(
    sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime | None = None,
    deliver=None, batch_size: int = REPORT_BATCH_SIZE,
) -> int:
    """Claim and retry due deliveries; returns how many were attempted."""
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("The scheduled report worker requires a timezone-aware timestamp.")
    due = or_(
        and_(ReportDelivery.status.in_(("pending", "retry")),
             or_(ReportDelivery.next_attempt_at.is_(None),
                 ReportDelivery.next_attempt_at <= current)),
        and_(ReportDelivery.status == "sending", ReportDelivery.claimed_until <= current),
    )
    async with sessionmaker() as db:
        rows = (await db.execute(
            select(ReportDelivery).where(due)
            .order_by(ReportDelivery.created_at, ReportDelivery.id)
            .limit(batch_size).with_for_update(of=ReportDelivery, skip_locked=True)
        )).scalars().all()
        claimed: list[uuid.UUID] = []
        for delivery in rows:
            if delivery.attempts >= REPORT_MAX_ATTEMPTS:
                delivery.status, delivery.claimed_until, delivery.next_attempt_at = "failed", None, None
            else:
                delivery.status = "sending"
                delivery.attempts += 1
                delivery.claimed_until = current + timedelta(seconds=REPORT_CLAIM_SECONDS)
                claimed.append(delivery.id)
        await db.commit()

    attempted = 0
    for delivery_id in claimed:
        async with sessionmaker() as db:
            delivery = await db.get(ReportDelivery, delivery_id, with_for_update=True)
            if delivery is None or delivery.status != "sending":
                continue
            schedule = await db.get(ReportScheduleRow, delivery.schedule_id)
            recipient = await db.get(User, delivery.recipient_user_id)
            if schedule is None or recipient is None or not recipient.is_active:
                delivery.status, delivery.claimed_until = "cancelled", None
                await db.commit()
                continue
            body = await _build_body(db, schedule, recipient, delivery.period_start)
            if body is None:
                delivery.status, delivery.claimed_until = "cancelled", None
                await db.commit()
                continue
            subject = _subject_for(schedule.template, delivery.period_start, schedule.timezone)
            if delivery.channel == "in_app":
                delivery.status, delivery.sent_at = "sent", current
                delivery.claimed_until, delivery.next_attempt_at = None, None
            elif deliver is None:
                delivery.status = "retry"
                delivery.next_attempt_at = current + backoff_delay(delivery.attempts)
                delivery.last_error_code = "channel_not_configured"
                delivery.claimed_until = None
            else:
                try:
                    ref = await deliver(delivery, schedule, recipient, subject, body)
                except Exception as exc:  # noqa: BLE001 - retry on any sender failure
                    logger.warning("Scheduled report delivery failed (%s)", type(exc).__name__)
                    delivery.last_error_code = type(exc).__name__[:80]
                    if delivery.attempts >= REPORT_MAX_ATTEMPTS:
                        delivery.status, delivery.next_attempt_at = "failed", None
                    else:
                        delivery.status = "retry"
                        delivery.next_attempt_at = current + backoff_delay(delivery.attempts)
                    delivery.claimed_until = None
                else:
                    delivery.status, delivery.sent_at = "sent", current
                    delivery.provider_ref = ref
                    delivery.claimed_until, delivery.next_attempt_at = None, None
            await db.commit()
            attempted += 1
    return attempted


async def run_scheduled_report_worker(
    sessionmaker: async_sessionmaker[AsyncSession], stop: asyncio.Event, *,
    interval_seconds: float = REPORT_POLL_SECONDS, deliver=None,
) -> None:
    """Background scheduler; retries on the next tick and logs only exception types."""
    while not stop.is_set():
        try:
            await process_scheduled_reports(sessionmaker, deliver=deliver)
            await deliver_pending_report_deliveries(sessionmaker, deliver=deliver)
        except Exception as exc:
            logger.warning("Scheduled report scheduler failed (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
