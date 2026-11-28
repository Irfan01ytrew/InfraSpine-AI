"""Scheduled report delivery channels (Step AA, slice 4).

This module is the ``deliver`` callback the scheduled-report worker calls
for every WhatsApp/email delivery. It owns the channel sends; the worker
still owns dedupe, retry bookkeeping and the delivery rows.

Channels
--------
* ``in_app`` — no send at all; the delivery record itself is the
  notification (the worker marks it sent). This function returns ``None``.
* ``whatsapp`` — Meta Cloud API text via the Step F service
  (:func:`app.services.agent_studio_whatsapp.send_outbound_text`). The
  sender is the WhatsApp connection owned by the admin who created the
  schedule; the recipient's number comes from the Step F contact list
  (``AgentStudioWhatsAppContact``), so only a number that already messaged
  the business — an allowed, verified sender — can ever receive a report.
  The send goes through ``dispatch_once`` (duty ``channel.reply``) with the
  key ``scheduled-report:<delivery id>``, so a crash after the send never
  re-sends on retry: the stored provider id is replayed instead.
* ``email`` — the shared SMTP transport (:func:`deliver_message`), gated by
  the ``scheduled_report_email_enabled`` setting. The Message-ID is
  deterministic (``<scheduled-report-<delivery id>@infraspine.local>``)
  and is returned as the provider reference.

Limits (recorded honestly, not hidden)
--------------------------------------
* Meta only delivers free-form text inside the 24-hour messaging window.
  A recipient whose last message is older than 24 hours raises
  :class:`WhatsAppWindowExpired`; the worker retries it with backoff and it
  eventually fails. Template messages for expired windows are a later
  slice (Step F gap list); this module never invents a template send.
* There is no Baileys failover here yet: the Baileys bridge only sends
  replies to inbound senders. Cloud API is the only WhatsApp sender in v1.
* Report bodies are summaries only. They never move money, change a payee,
  or approve anything, and no account numbers or secrets are included.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.crypto import CredentialCipher
from app.db.agent_studio_whatsapp_models import (
    AgentStudioWhatsAppContact,
    AgentStudioWhatsAppInbound,
)
from app.db.models import User
from app.db.report_schedule_models import ReportDelivery, ReportScheduleRow
from app.platform.access import principal_for
from app.platform.approvals import ActionSpec
from app.platform.side_effects import dispatch_once, org_scope_id
from app.services.agent_studio_whatsapp import get_connection, send_outbound_text
from app.services.personal_task_email import _mailbox, deliver_message

logger = logging.getLogger(__name__)

# Meta's free-form messaging window: a business may only send a free-form
# text within 24 hours of the recipient's last message.
WHATSAPP_WINDOW = timedelta(hours=24)


class ReportDeliveryError(Exception):
    """A report could not be sent on this channel (worker records it as retry)."""


class WhatsAppNotConfigured(ReportDeliveryError):
    """No enabled WhatsApp connection exists for the schedule's creator."""


class WhatsAppNumberUnknown(ReportDeliveryError):
    """The recipient has no WhatsApp contact on the schedule's connection."""


class WhatsAppNotAllowed(ReportDeliveryError):
    """The recipient's number is not on the connection's allow-list."""


class WhatsAppWindowExpired(ReportDeliveryError):
    """The recipient's 24-hour messaging window has expired."""


class WhatsAppSendFailed(ReportDeliveryError):
    """The Cloud API send did not produce a verifiable provider message id."""


class EmailNotConfigured(ReportDeliveryError):
    """Email delivery for scheduled reports is not enabled."""


def build_report_email(settings: Settings, recipient: User, delivery: ReportDelivery,
                       subject: str, body: str) -> EmailMessage:
    """Build the report email. The Message-ID is deterministic per delivery."""
    message = EmailMessage()
    message["From"] = _mailbox(settings.personal_task_email_from)
    message["To"] = _mailbox(recipient.email)
    message["Subject"] = " ".join(subject.split())
    message["Message-ID"] = f"<scheduled-report-{delivery.id}@infraspine.local>"
    message.set_content(
        body.rstrip()
        + "\n\n---\n"
        + "This is an automatic InfraSpine AI report summary. It never moves money "
        + "or approves anything — sign in to InfraSpine AI to review or act.\n"
    )
    return message


async def _send_whatsapp(
    sessionmaker: async_sessionmaker[AsyncSession],
    delivery: ReportDelivery,
    schedule_row: ReportScheduleRow,
    recipient: User,
    body: str,
    cipher: CredentialCipher | None,
    sender: Callable[..., Awaitable[str]] | None,
) -> str:
    """Send one WhatsApp report; returns the provider message id."""
    if cipher is None:
        raise WhatsAppNotConfigured("no decryption key available for the WhatsApp connection")
    now = datetime.now(UTC)
    async with sessionmaker() as db:
        owner = await db.get(User, schedule_row.created_by) if schedule_row.created_by else None
        if owner is None:
            raise WhatsAppNotConfigured("the schedule has no creator with a WhatsApp connection")
        connection = await get_connection(db, owner)
        if connection is None or not connection.enabled:
            raise WhatsAppNotConfigured("no enabled WhatsApp connection for the schedule creator")
        contact = await db.scalar(
            select(AgentStudioWhatsAppContact)
            .where(AgentStudioWhatsAppContact.connection_id == connection.id,
                   AgentStudioWhatsAppContact.owner_id == recipient.id)
            .order_by(AgentStudioWhatsAppContact.updated_at.desc())
        )
        if contact is None:
            raise WhatsAppNumberUnknown(
                "the recipient has never messaged the WhatsApp business number")
        wa_id = contact.wa_id
        if wa_id not in (connection.allowed_senders or []):
            raise WhatsAppNotAllowed("the recipient's number is not an allowed sender")
        last_inbound = await db.scalar(
            select(AgentStudioWhatsAppInbound.created_at)
            .where(AgentStudioWhatsAppInbound.connection_id == connection.id,
                   AgentStudioWhatsAppInbound.from_wa_id == wa_id)
            .order_by(AgentStudioWhatsAppInbound.created_at.desc())
        )
        if last_inbound is None or now - last_inbound > WHATSAPP_WINDOW:
            raise WhatsAppWindowExpired("the recipient's 24-hour messaging window has expired")
        principal = principal_for(owner, channel="whatsapp_cloud")
        scope_id = await org_scope_id(db)
        key = f"scheduled-report:{delivery.id}"
        action = ActionSpec(
            "whatsapp.send", scope_id,
            {"to": wa_id, "delivery_id": str(delivery.id), "key": key},
            subject=f"scheduled-report:{delivery.id}",
        )
        send = sender or send_outbound_text

        async def dispatch() -> str:
            return await send(connection, cipher, wa_id, body)

        result = await dispatch_once(db, principal, key, action, duty="channel.reply",
                                     dispatch=dispatch)
        if result.status == "failed" or not result.external_ref:
            raise WhatsAppSendFailed("the WhatsApp send produced no provider message id")
        return result.external_ref


async def _send_email(
    settings: Settings,
    delivery: ReportDelivery,
    recipient: User,
    subject: str,
    body: str,
    email_sender: Callable[[Settings, EmailMessage], None] | None,
) -> str:
    """Send one report email; returns the deterministic Message-ID."""
    if not settings.scheduled_report_email_enabled:
        raise EmailNotConfigured("scheduled report email is not enabled")
    message = build_report_email(settings, recipient, delivery, subject, body)
    sender = email_sender or deliver_message
    await asyncio.to_thread(sender, settings, message)
    return str(message["Message-ID"])


async def deliver_report(
    delivery: ReportDelivery,
    schedule_row: ReportScheduleRow,
    recipient: User,
    subject: str,
    body: str,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    cipher: CredentialCipher | None,
    whatsapp_sender: Callable[..., Awaitable[str]] | None = None,
    email_sender: Callable[[Settings, EmailMessage], None] | None = None,
) -> str | None:
    """Send one scheduled-report delivery.

    This is the worker's ``deliver`` callback: ``deliver(delivery,
    schedule_row, recipient, subject, body)``. Returns the provider
    reference (WhatsApp message id, email Message-ID) on success, or
    ``None`` for ``in_app``. Raises a :class:`ReportDeliveryError` subclass
    when the channel cannot send right now; the worker records the typed
    error code and retries with backoff.
    """
    if delivery.channel == "in_app":
        return None
    if delivery.channel == "whatsapp":
        return await _send_whatsapp(
            sessionmaker, delivery, schedule_row, recipient, body, cipher, whatsapp_sender)
    if delivery.channel == "email":
        return await _send_email(
            settings, delivery, recipient, subject, body, email_sender)
    raise ReportDeliveryError(f"unknown delivery channel: {delivery.channel!r}")


def make_report_deliver(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    cipher: CredentialCipher | None,
    *,
    whatsapp_sender: Callable[..., Awaitable[str]] | None = None,
    email_sender: Callable[[Settings, EmailMessage], None] | None = None,
) -> Callable[..., Awaitable[str | None]]:
    """Build the worker's ``deliver`` callback bound to this process's settings."""

    async def _deliver(delivery: ReportDelivery, schedule_row: ReportScheduleRow,
                       recipient: User, subject: str, body: str) -> str | None:
        return await deliver_report(
            delivery, schedule_row, recipient, subject, body,
            sessionmaker=sessionmaker, settings=settings, cipher=cipher,
            whatsapp_sender=whatsapp_sender, email_sender=email_sender)

    return _deliver
