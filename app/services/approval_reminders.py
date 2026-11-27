"""Reminders, escalation and email for approval steps left waiting for a decision.

A pending step on an open request is reminded every `after` (default 24 hours) since it became pending or was
last reminded, at most `max_reminders` times. Each reminder brings the in-app notification back to unread for
everyone who can decide the step now (including active delegates), adds a timeline note and an audit record.
When `after` passes again after the last reminder, the step is escalated once: the approvers' own managers (or
the administrators when there are none) are alerted and may follow the request up. Row locks with SKIP LOCKED
keep competing worker processes from reminding or escalating the same step twice.

When APPROVAL_EMAIL_ENABLED is on, each alert also queues a content-free email that this worker sends with
retries over the configured SMTP transport; emails for requests closed in the meantime are cancelled.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.approval_models import ApprovalEmail, RequestApprovalStep
from app.db.models import ServiceRequest, ServiceRequestEvent, User
from app.services.approval_delegation import escalation_recipients, notify, notify_step
from app.services.audit import AuditContext, record
from app.services.personal_task_email import send_approval_email

logger = logging.getLogger(__name__)
APPROVAL_REMINDER_POLL_SECONDS = 300
REMIND_AFTER = timedelta(hours=24)
MAX_REMINDERS = 3
EMAIL_BATCH_SIZE = 50
EMAIL_MAX_ATTEMPTS = 8
EMAIL_CLAIM_SECONDS = 300
_OPEN = ("submitted", "acknowledged", "in_progress", "waiting_requester")
_WORKER = AuditContext(None, "approval-reminder-worker", None, None)


def _waited(since: datetime, now: datetime) -> str:
    waited = now - since
    return f"{waited.days} day(s) {waited.seconds // 3600} hour(s)"


async def process_approval_reminders(
    sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime | None = None,
    after: timedelta = REMIND_AFTER, max_reminders: int = MAX_REMINDERS, batch_size: int = 100,
    settings: Settings | None = None,
) -> int:
    """Send due reminders and escalations; returns how many steps were reminded or escalated."""
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("The approval reminder worker requires a timezone-aware timestamp.")
    last_touch = func.coalesce(RequestApprovalStep.last_reminded_at, RequestApprovalStep.pending_since)
    async with sessionmaker() as db:
        due = (await db.execute(
            select(RequestApprovalStep, ServiceRequest)
            .join(ServiceRequest, ServiceRequest.id == RequestApprovalStep.request_id)
            .where(
                RequestApprovalStep.status == "pending",
                RequestApprovalStep.pending_since.is_not(None),
                RequestApprovalStep.escalated_at.is_(None),
                last_touch <= current - after,
                ServiceRequest.status.in_(_OPEN),
            )
            .order_by(RequestApprovalStep.pending_since, RequestApprovalStep.id)
            .limit(batch_size)
            .with_for_update(of=RequestApprovalStep, skip_locked=True)
        )).all()
        for step, request in due:
            if step.reminder_count < max_reminders:
                step.reminder_count += 1
                step.last_reminded_at = current
                recipients = await notify_step(db, request, step, "approval_step_reminder", current)
                note = f"Reminder {step.reminder_count} of {max_reminders}: step {step.step_order} ({step.label}) has waited {_waited(step.pending_since, current)} for a decision."
                action, details = "service_request.step_reminder", {"reminder": step.reminder_count}
            else:
                step.escalated_at = current
                recipients = await notify(db, request, await escalation_recipients(db, request, step), "approval_step_escalated", current)
                note = f"Escalated: step {step.step_order} ({step.label}) was not decided after {max_reminders} reminders ({_waited(step.pending_since, current)}); the approvers' managers were alerted."
                action, details = "service_request.step_escalated", {}
            db.add(ServiceRequestEvent(
                request_id=request.id, actor_user_id=None, actor_email="approval-reminder-worker", action="commented",
                from_status=request.status, to_status=request.status, created_at=current, comment=note,
            ))
            record(db, _WORKER, action, target=str(request.id),
                   details={"step": step.step_order, "label": step.label, "recipient_count": recipients, **details})
        await db.commit()
    await deliver_approval_emails(sessionmaker, settings=settings or get_settings(), now=current)
    return len(due)


async def deliver_approval_emails(sessionmaker: async_sessionmaker[AsyncSession], *, settings: Settings, now: datetime | None = None) -> int:
    """Claim and send a bounded batch of queued approval emails, with retries; logs no addresses."""
    if not settings.approval_email_enabled:
        return 0
    current = now or datetime.now(UTC)
    due = or_(
        and_(ApprovalEmail.status.in_(("pending", "retry")), or_(ApprovalEmail.next_attempt_at.is_(None), ApprovalEmail.next_attempt_at <= current)),
        and_(ApprovalEmail.status == "sending", ApprovalEmail.claimed_until <= current),
    )
    async with sessionmaker() as db:
        rows = (await db.execute(
            select(ApprovalEmail, User.email, User.is_active, ServiceRequest.status)
            .join(User, User.id == ApprovalEmail.recipient_user_id)
            .join(ServiceRequest, ServiceRequest.id == ApprovalEmail.request_id)
            .where(due).order_by(ApprovalEmail.created_at, ApprovalEmail.id)
            .limit(EMAIL_BATCH_SIZE).with_for_update(of=ApprovalEmail, skip_locked=True)
        )).all()
        claimed: list[tuple[uuid.UUID, str, str, str, int]] = []
        for email, address, active, request_status in rows:
            if request_status not in _OPEN or not active:
                email.status, email.claimed_until = "cancelled", None
            elif email.attempts >= EMAIL_MAX_ATTEMPTS:
                email.status, email.claimed_until, email.next_attempt_at = "failed", None, None
            else:
                email.status, email.attempts = "sending", email.attempts + 1
                email.claimed_until = current + timedelta(seconds=EMAIL_CLAIM_SECONDS)
                claimed.append((email.id, address, email.kind, str(email.request_id)[:8], email.attempts))
        await db.commit()

    sent = 0
    for email_id, address, kind, reference, attempt in claimed:
        try:
            await asyncio.to_thread(send_approval_email, settings, address, email_id, kind, reference)
            outcome, error = "sent", None
        except Exception as exc:
            outcome, error = ("retry" if attempt < EMAIL_MAX_ATTEMPTS else "failed"), type(exc).__name__[:64]
            logger.warning("Approval email delivery failed (%s)", error)
        async with sessionmaker() as db:
            email = await db.get(ApprovalEmail, email_id, with_for_update=True)
            if email is not None and email.status == "sending":
                email.status, email.claimed_until, email.last_error_code = outcome, None, error
                if outcome == "sent":
                    email.sent_at, email.next_attempt_at = current, None
                    sent += 1
                else:
                    email.next_attempt_at = current + timedelta(seconds=min(6 * 3600, 15 * 2 ** min(attempt - 1, 10))) if outcome == "retry" else None
                await db.commit()
    return sent


async def run_approval_reminders(
    sessionmaker: async_sessionmaker[AsyncSession], stop: asyncio.Event, *, interval_seconds: float = APPROVAL_REMINDER_POLL_SECONDS,
) -> None:
    """Background scheduler; retries on the next tick and logs only exception types."""
    while not stop.is_set():
        try:
            await process_approval_reminders(sessionmaker)
        except Exception as exc:
            logger.warning("Approval reminder scheduler failed (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
