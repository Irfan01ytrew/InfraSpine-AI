"""Privacy-minimal SMTP delivery for personal task deadline reminders."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr
from uuid import UUID

from app.core.config import Settings


def _mailbox(value: str) -> str:
    if any(ord(character) < 32 for character in value):
        raise ValueError("Email addresses must not contain control characters.")
    _, address = parseaddr(value)
    if not address or "@" not in address or address.startswith("@") or address.endswith("@"):
        raise ValueError("A valid email mailbox is required.")
    return address


def build_personal_task_reminder_message(sender: str, recipient: str, reminder_id: UUID) -> EmailMessage:
    """Build a stable, content-free reminder message; task title/body never leave the app."""
    message = EmailMessage()
    message["From"] = _mailbox(sender)
    message["To"] = _mailbox(recipient)
    message["Subject"] = "InfraSpine AI assignment deadline reminder"
    message["Message-ID"] = f"<personal-task-reminder-{reminder_id}@infraspine.local>"
    message.set_content(
        "You have an assignment deadline reminder in InfraSpine AI. "
        "Sign in to review your private My Work inbox. This message does not include task details.\n"
    )
    return message


def send_personal_task_reminder_email(settings: Settings, recipient: str, reminder_id: UUID) -> None:
    """Send over verified TLS; the deterministic Message-ID supports downstream deduplication."""
    if not settings.personal_task_email_enabled:
        return
    sender = _mailbox(settings.personal_task_email_from)
    recipient = _mailbox(recipient)
    deliver_message(settings, build_personal_task_reminder_message(sender, recipient, reminder_id))


def build_approval_message(sender: str, recipient: str, email_id: UUID, kind: str, reference: str) -> EmailMessage:
    """Content-free approval notice: no request subject, amount or requester leaves the app."""
    subjects = {
        "pending": "InfraSpine AI: an approval is waiting for you",
        "reminder": "InfraSpine AI reminder: an approval is still waiting for you",
        "escalation": "InfraSpine AI: an approval step has been waiting too long",
    }
    bodies = {
        "pending": "An approval step is waiting for your decision",
        "reminder": "An approval step is still waiting for your decision",
        "escalation": "An approval step assigned to someone who reports to you, or in your organization, has not been decided after several reminders",
    }
    message = EmailMessage()
    message["From"] = _mailbox(sender)
    message["To"] = _mailbox(recipient)
    message["Subject"] = subjects[kind]
    message["Message-ID"] = f"<approval-{kind}-{email_id}@infraspine.local>"
    message.set_content(
        f"{bodies[kind]} (request reference {reference}). Sign in to InfraSpine AI and open Help & requests. "
        "This message does not include request details.\n"
    )
    return message


def send_approval_email(settings: Settings, recipient: str, email_id: UUID, kind: str, reference: str) -> None:
    if not settings.approval_email_enabled:
        return
    sender = _mailbox(settings.personal_task_email_from)
    deliver_message(settings, build_approval_message(sender, _mailbox(recipient), email_id, kind, reference))


def deliver_message(settings: Settings, message: EmailMessage) -> None:
    """Send one message over verified TLS with the configured SMTP transport."""
    context = ssl.create_default_context()
    password = settings.personal_task_smtp_password

    if settings.personal_task_smtp_security == "ssl":
        client_context = smtplib.SMTP_SSL(
            settings.personal_task_smtp_host,
            settings.personal_task_smtp_port,
            timeout=settings.personal_task_smtp_timeout_seconds,
            context=context,
        )
        with client_context as client:
            if settings.personal_task_smtp_username:
                client.login(settings.personal_task_smtp_username, password.get_secret_value())
            client.send_message(message)
        return

    with smtplib.SMTP(
        settings.personal_task_smtp_host,
        settings.personal_task_smtp_port,
        timeout=settings.personal_task_smtp_timeout_seconds,
    ) as client:
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
        if settings.personal_task_smtp_username:
            client.login(settings.personal_task_smtp_username, password.get_secret_value())
        client.send_message(message)
