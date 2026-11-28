"""Step AA slice 4: delivery channels — WhatsApp via the Step F service and
email via the shared SMTP transport, with delivery log + dedupe proof.

Every Meta/SMTP call is a fake injected sender; no network is touched. The
exactly-once proof is behavioural: the same delivery sent twice (a retry
after a crash) reaches the fake sender exactly once, because
``dispatch_once`` replays the stored provider id.
"""

import asyncio
import base64
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import platform
from app.core.config import Settings
from app.core.crypto import CredentialCipher
from app.db.agent_studio_models import AgentStudioAgent, AgentStudioThread
from app.db.agent_studio_whatsapp_models import (
    AgentStudioWhatsAppConnection,
    AgentStudioWhatsAppContact,
    AgentStudioWhatsAppInbound,
)
from app.db.finance_models import FinInvoice, FinInvoiceBatch, FinPayment, FinVendor
from app.db.models import AccessGroup, User
from app.db.report_schedule_models import ReportDelivery, ReportScheduleRow
from app.db.session import test_engine_options as _test_engine_options
from app.platform.models import PlatformActionExecution
from app.platform.sync import group_scope_sync
from app.services.scheduled_report_delivery import (
    EmailNotConfigured,
    WhatsAppNotAllowed,
    WhatsAppNotConfigured,
    WhatsAppNumberUnknown,
    WhatsAppWindowExpired,
    build_report_email,
    deliver_report,
    make_report_deliver,
)
from app.services.scheduled_report_worker import process_scheduled_reports
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, USER_EMAIL

pytestmark = pytest.mark.usefixtures("clean_tables")

PKT = ZoneInfo("Asia/Karachi")
WA_ID = "15551234567"


def run_async(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _settings(**overrides):
    values = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://synthetic:synthetic@127.0.0.1:1/synthetic",
        "master_encryption_key": base64.b64encode(b"k" * 32).decode(),
        "app_env": "testing",
        "scheduled_report_email_enabled": True,
        "personal_task_smtp_host": "mail.synthetic.invalid",
        "personal_task_smtp_port": 587,
        "personal_task_smtp_security": "starttls",
        "personal_task_smtp_username": "synthetic-user",
        "personal_task_smtp_password": SecretStr("synthetic-password"),
        "personal_task_email_from": "reports@synthetic.invalid",
    }
    values.update(overrides)
    return Settings(**values)


def _cipher():
    return CredentialCipher(b"k" * 32, "test-key")


async def _must_not_send(*args):
    raise AssertionError("must not send")


def _at(hour, minute=0, day=28):
    return datetime(2026, 11, day, hour, minute, tzinfo=PKT)


async def _setup(sm, *, channels=("whatsapp",), with_connection=True,
                 allowed=True, with_contact=True, inbound_age_hours=1):
    """Finance rows for the body + a schedule + optional WhatsApp fixtures.

    The connection belongs to the admin (the schedule creator); the contact
    links the recipient user to the connection, as if they had messaged the
    business number (``inbound_age_hours`` ago).
    """
    async with sm() as db:
        admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
        recipient = await db.scalar(select(User).where(User.email == USER_EMAIL))
        group = AccessGroup(name="Finance", kind="department", source="local")
        db.add(group)
        await db.flush()
        scope_id = await db.run_sync(lambda s: group_scope_sync(s.connection(), group.id))
        await platform.ensure_role(db, "finance.viewer", {"finance.read"})
        await platform.grant_membership(
            db, platform.principal_for(admin), user_id=recipient.id,
            scope_id=scope_id, role_key="finance.viewer")
        batch = FinInvoiceBatch(group_id=group.id, name="B1", created_by=admin.id,
                                currency="PKR", status="open")
        db.add(batch)
        await db.flush()
        today = date(2026, 11, 28)
        db.add(FinInvoice(group_id=group.id, batch_id=batch.id, seq=1,
                          invoice_number="INV-0001", status="pending",
                          amount=Decimal("100000"), currency="PKR",
                          invoice_date=today - timedelta(days=10),
                          source_kind="json", source_digest="digest-1"))
        vendor = FinVendor(group_id=group.id, name="Alpha Supplies", name_key="alpha supplies",
                           bank_account="PK00ALPHA0001", currency="PKR")
        db.add(vendor)
        await db.flush()
        db.add(FinPayment(group_id=group.id, batch_id=batch.id, vendor_id=vendor.id,
                          amount=Decimal("30000"), currency="PKR", payee_account="PK00ALPHA0001",
                          invoice_ids=[], status="verified",
                          created_at=datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)))
        schedule = ReportScheduleRow(
            template="daily_cash_summary", cadence="daily", time_of_day=_at(8, 0).time(),
            timezone="Asia/Karachi", weekday=0, channels=list(channels),
            recipients=[str(recipient.id)], quiet_start=_at(22, 0).time(),
            quiet_end=_at(7, 0).time(), overdue_days=7, enabled=True,
            created_by=admin.id)
        db.add(schedule)
        await db.flush()
        connection = None
        if with_connection:
            agent = AgentStudioAgent(owner_id=admin.id, name="Reports agent",
                                     instructions="Send scheduled reports.")
            db.add(agent)
            await db.flush()
            thread = AgentStudioThread(agent_id=agent.id, owner_id=admin.id,
                                       history_ciphertext=b"x", history_nonce=b"y",
                                       history_key_id="test-key")
            db.add(thread)
            await db.flush()
            connection = AgentStudioWhatsAppConnection(
                owner_id=admin.id, agent_id=agent.id, phone_number_id="123456789",
                business_account_id="987654321",
                allowed_senders=[WA_ID] if allowed else [],
                access_token_ciphertext=b"x", access_token_nonce=b"y",
                access_token_key_id="test-key",
                app_secret_ciphertext=b"x", app_secret_nonce=b"y",
                app_secret_key_id="test-key",
                verify_token_ciphertext=b"x", verify_token_nonce=b"y",
                verify_token_key_id="test-key", enabled=True)
            db.add(connection)
            await db.flush()
            if with_contact:
                db.add(AgentStudioWhatsAppContact(
                    connection_id=connection.id, owner_id=recipient.id, wa_id=WA_ID,
                    thread_id=thread.id))
                await db.flush()
                db.add(AgentStudioWhatsAppInbound(
                    connection_id=connection.id, owner_id=admin.id,
                    external_message_id=f"wamid.{uuid4().hex[:16]}",
                    from_wa_id=WA_ID, message_type="text",
                    text_ciphertext=b"x", text_nonce=b"y", text_key_id="test-key",
                    status="completed",
                    created_at=datetime.now(timezone.utc) - timedelta(hours=inbound_age_hours)))
        await db.commit()
        return {"schedule_id": schedule.id, "recipient_id": recipient.id,
                "connection_id": connection.id if connection else None}


async def _deliveries(sm, **filters):
    async with sm() as db:
        query = select(ReportDelivery)
        for key, value in filters.items():
            query = query.where(getattr(ReportDelivery, key) == value)
        return list((await db.scalars(query)).all())


def test_whatsapp_send_marks_sent_with_provider_ref():
    sent = []

    async def fake_sender(connection, cipher, wa_id, body):
        sent.append((connection.phone_number_id, wa_id, body))
        return "wamid.abc123"

    async def go(sm):
        s = await _setup(sm)
        deliver = make_report_deliver(sm, _settings(), _cipher(), whatsapp_sender=fake_sender)
        assert await process_scheduled_reports(sm, now=_at(8, 5), deliver=deliver) == 1
        assert len(sent) == 1
        assert sent[0][0] == "123456789" and sent[0][1] == WA_ID
        assert "Total invoiced: 100000.00 PKR" in sent[0][2]
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        assert len(rows) == 1
        assert rows[0].status == "sent" and rows[0].provider_ref == "wamid.abc123"
        assert rows[0].sent_at is not None

    run_async(go)


def test_whatsapp_send_is_exactly_once_across_retry():
    """A retry after a crash replays the stored provider id; the sender runs once."""
    sent = []

    async def fake_sender(connection, cipher, wa_id, body):
        sent.append(body)
        return "wamid.once-only"

    async def go(sm):
        s = await _setup(sm)
        async with sm() as db:
            schedule = await db.get(ReportScheduleRow, s["schedule_id"])
            recipient = await db.get(User, s["recipient_id"])
            delivery = ReportDelivery(
                schedule_id=schedule.id, period_start=_at(8, 0),
                channel="whatsapp", recipient_user_id=recipient.id, status="sending")
            db.add(delivery)
            await db.commit()
            delivery_id = delivery.id
        settings, cipher = _settings(), _cipher()
        ref1 = await deliver_report(delivery, schedule, recipient, "subject", "body",
                                    sessionmaker=sm, settings=settings, cipher=cipher,
                                    whatsapp_sender=fake_sender)
        ref2 = await deliver_report(delivery, schedule, recipient, "subject", "body",
                                    sessionmaker=sm, settings=settings, cipher=cipher,
                                    whatsapp_sender=fake_sender)
        assert ref1 == ref2 == "wamid.once-only"
        assert len(sent) == 1
        async with sm() as db:
            executions = list((await db.scalars(select(PlatformActionExecution).where(
                PlatformActionExecution.idempotency_key == f"scheduled-report:{delivery_id}"))).all())
        assert len(executions) == 1
        assert executions[0].external_ref == "wamid.once-only"

    run_async(go)


def test_whatsapp_without_connection_records_retry():
    async def go(sm):
        s = await _setup(sm, with_connection=False)
        deliver = make_report_deliver(sm, _settings(), _cipher(),
                                      whatsapp_sender=_must_not_send)
        assert await process_scheduled_reports(sm, now=_at(8, 5), deliver=deliver) == 1
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        assert len(rows) == 1 and rows[0].status == "retry"
        assert rows[0].last_error_code == "WhatsAppNotConfigured"

    run_async(go)


def test_whatsapp_without_contact_records_retry():
    async def go(sm):
        s = await _setup(sm, with_contact=False)
        deliver = make_report_deliver(sm, _settings(), _cipher(),
                                      whatsapp_sender=_must_not_send)
        assert await process_scheduled_reports(sm, now=_at(8, 5), deliver=deliver) == 1
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        assert len(rows) == 1 and rows[0].status == "retry"
        assert rows[0].last_error_code == "WhatsAppNumberUnknown"

    run_async(go)


def test_whatsapp_not_on_allow_list_records_retry():
    async def go(sm):
        s = await _setup(sm, allowed=False)
        deliver = make_report_deliver(sm, _settings(), _cipher(),
                                      whatsapp_sender=_must_not_send)
        assert await process_scheduled_reports(sm, now=_at(8, 5), deliver=deliver) == 1
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        assert len(rows) == 1 and rows[0].status == "retry"
        assert rows[0].last_error_code == "WhatsAppNotAllowed"

    run_async(go)


def test_whatsapp_window_expired_records_retry():
    async def go(sm):
        s = await _setup(sm, inbound_age_hours=25)
        deliver = make_report_deliver(sm, _settings(), _cipher(),
                                      whatsapp_sender=_must_not_send)
        assert await process_scheduled_reports(sm, now=_at(8, 5), deliver=deliver) == 1
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        assert len(rows) == 1 and rows[0].status == "retry"
        assert rows[0].last_error_code == "WhatsAppWindowExpired"

    run_async(go)


def test_email_send_marks_sent_with_message_id():
    captured = {}

    def fake_email(settings, message):
        captured["message"] = message

    async def go(sm):
        s = await _setup(sm, channels=("email",))
        deliver = make_report_deliver(sm, _settings(), _cipher(), email_sender=fake_email)
        assert await process_scheduled_reports(sm, now=_at(8, 5), deliver=deliver) == 1
        assert len(captured) == 1
        message = captured["message"]
        assert message["To"] == USER_EMAIL
        assert message["From"] == "reports@synthetic.invalid"
        assert "Daily cash summary" in message["Subject"]
        assert "Total invoiced: 100000.00 PKR" in message.get_content()
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        assert len(rows) == 1
        assert rows[0].status == "sent"
        assert rows[0].provider_ref == str(message["Message-ID"])
        assert rows[0].provider_ref.startswith("<scheduled-report-")

    run_async(go)


def test_email_disabled_records_retry():
    async def go(sm):
        s = await _setup(sm, channels=("email",))
        deliver = make_report_deliver(
            sm, _settings(scheduled_report_email_enabled=False), _cipher(),
            email_sender=_must_not_send)
        assert await process_scheduled_reports(sm, now=_at(8, 5), deliver=deliver) == 1
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        assert len(rows) == 1 and rows[0].status == "retry"
        assert rows[0].last_error_code == "EmailNotConfigured"

    run_async(go)


def test_duplicate_delivery_row_is_rejected_by_the_database():
    async def go(sm):
        s = await _setup(sm, channels=("in_app",))
        assert await process_scheduled_reports(sm, now=_at(8, 5)) == 1
        rows = await _deliveries(sm, schedule_id=s["schedule_id"])
        assert len(rows) == 1
        existing = rows[0]
        async with sm() as db:
            db.add(ReportDelivery(
                schedule_id=existing.schedule_id, period_start=existing.period_start,
                channel=existing.channel, recipient_user_id=existing.recipient_user_id,
                status="pending"))
            with pytest.raises(IntegrityError):
                await db.commit()

    run_async(go)


def test_report_email_has_stable_message_id_and_no_task_content():
    async def go(sm):
        async with sm() as db:
            recipient = await db.scalar(select(User).where(User.email == USER_EMAIL))
            delivery = ReportDelivery(
                schedule_id=uuid4(), period_start=_at(8, 0), channel="email",
                recipient_user_id=recipient.id, status="pending")
            message = build_report_email(_settings(), recipient, delivery,
                                         "Daily cash summary — 2026-11-28", "Total invoiced: 1 PKR")
            assert message["Message-ID"] == f"<scheduled-report-{delivery.id}@infraspine.local>"
            assert "Total invoiced: 1 PKR" in message.get_content()
            assert "never moves money" in message.get_content()

    run_async(go)


def test_delivery_error_types_are_distinct():
    assert issubclass(WhatsAppNotConfigured, Exception)
    assert WhatsAppNumberUnknown is not WhatsAppNotAllowed
    assert WhatsAppWindowExpired is not WhatsAppNotConfigured
    assert EmailNotConfigured is not WhatsAppNotConfigured
