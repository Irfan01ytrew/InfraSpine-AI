"""Step AF slice 4: alert dispatch worker.

One pass over a workflow scope:

1. :func:`collect_snapshot` reads the live signals (workflow runs, finance
   exceptions/payments, ingestion workers, LLM quota) into the slice-1
   :class:`~app.control_room.alerts.MetricsSnapshot`. Signals with no cheap
   live source stay ``None`` and their rules are skipped — never invented.
2. The slice-1 evaluator decides which rules fire, applying the scope's
   configured thresholds, cooldowns and quiet hours (see
   :mod:`app.control_room.config`).
3. :func:`queue_alerts` writes one ``pending`` delivery row per fired alert
   × recipient channel into ``control_room_alert_deliveries``. The log is
   the durable memory that makes cooldown/dedupe survive a worker restart:
   a row that is pending, sending, sent or recently failed inside the rule's
   cooldown window blocks a second queue of the same alert.
4. :func:`send_pending` claims pending rows with ``FOR UPDATE ... SKIP
   LOCKED`` (the same pattern as the approval-reminder worker), sends each
   one through the channel sender, and audits ``control_room.alert_sent``
   per send.

Channel order is WhatsApp first, email second, in-app as the always-on
fallback (plan §1). A channel with no configured recipient is skipped at
queue time; when no configured channel can be reached at all, the alert is
queued as ``in_app`` so it is still visible on the dashboard and in the
delivery history. Alerts are read-only notifications — dispatch never
executes an action, approves anything, or changes a payee or an invoice.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.control_room.alerts import (
    DEFAULT_QUIET_END,
    DEFAULT_QUIET_START,
    Alert,
    MetricsSnapshot,
    evaluate_alerts,
    format_alert_message,
)
from app.control_room.config import (
    config_to_dict,
    effective_rules,
    rule_channels,
)
from app.db.control_room_models import ControlRoomAlertConfig, ControlRoomAlertDelivery
from app.db.finance_models import FinInvoice, FinPayment
from app.db.models import Document
from app.db.workflow_engine_models import Workflow
from app.services.audit import AuditContext, record as audit_record

logger = logging.getLogger(__name__)

_ONE_HOUR = timedelta(hours=1)
_SEND_BATCH = 50
_ALERT_AUDIT_ACTION = "control_room.alert_sent"


class ChannelUnavailableError(Exception):
    """A configured channel cannot be reached right now (no connection / no SMTP)."""


class ChannelSender:
    """Sends one alert message on one channel. Returns a provider id or None."""

    async def send(self, channel: str, recipient: str | None, message: str) -> str | None:
        raise NotImplementedError


async def collect_snapshot(db: AsyncSession, scope_id: UUID, settings, *, now: datetime) -> MetricsSnapshot:
    """Read the live signals for one scope into a metrics snapshot.

    Finance signals are scoped to the department (``group_id == scope_id``,
    the same identity the charts endpoint uses). Ingestion signals are
    organisation-wide — documents carry no department — and are labelled as
    such in the docstring, not silently scoped. ``last_tick_age_seconds``
    stays ``None``: there is no persisted workflow-engine heartbeat yet
    (the same honest gap as ``charts.py``), so the ``worker_down`` rule
    fires only when the engine is off.
    """
    from app.control_room.charts import _health, _runs_summary

    runs = await _runs_summary(db, scope_id, now=now)
    health = await _health(db, settings, runs, now=now)
    window_start = now - _ONE_HOUR
    failed_payments = int(
        await db.scalar(
            select(func.count()).where(
                FinPayment.group_id == scope_id,
                FinPayment.status == "failed",
                FinPayment.updated_at >= window_start,
            )
        )
        or 0
    )
    oldest_awaiting = await db.scalar(
        select(func.min(FinPayment.created_at)).where(
            FinPayment.group_id == scope_id,
            FinPayment.status == "awaiting_approval",
        )
    )
    oldest_age = (now - oldest_awaiting).total_seconds() / 3600 if oldest_awaiting else None
    exceptions = int(
        await db.scalar(
            select(func.count()).where(
                FinInvoice.group_id == scope_id,
                FinInvoice.status == "exception",
            )
        )
        or 0
    )
    failed_ingestions = int(
        await db.scalar(
            select(func.count()).where(
                Document.status == "failed",
                Document.updated_at >= window_start,
            )
        )
        or 0
    )
    return MetricsSnapshot(
        engine_on=health["worker_enabled"],
        last_tick_age_seconds=None,
        queued_runs=runs["by_status"].get("queued", 0),
        running_runs=runs["by_status"].get("running", 0),
        failed_runs_last_hour=runs["failed_last_hour"],
        failed_payments_last_hour=failed_payments,
        exceptions_count=exceptions,
        oldest_approval_age_hours=oldest_age,
        ingestion_workers_online=health["ingestion_workers_online"],
        failed_ingestions_last_hour=failed_ingestions,
        llm_quota_percent=health["llm_quota_max_percent"],
    )


async def last_sent_map(db: AsyncSession, scope_id: UUID) -> dict[str, datetime]:
    """Rule key -> when that rule was last successfully sent for the scope."""
    rows = (
        await db.execute(
            select(
                ControlRoomAlertDelivery.rule_key,
                func.max(ControlRoomAlertDelivery.sent_at),
            )
            .where(
                ControlRoomAlertDelivery.scope_id == scope_id,
                ControlRoomAlertDelivery.status == "sent",
            )
            .group_by(ControlRoomAlertDelivery.rule_key)
        )
    ).all()
    return {key: sent for key, sent in rows if sent is not None}


async def _load_config(db: AsyncSession, scope_id: UUID) -> dict:
    row = await db.get(ControlRoomAlertConfig, scope_id)
    return config_to_dict(row)


def _recipients_for(config: dict, channel: str) -> list[str]:
    return [
        str(item["address"]).strip()
        for item in (config.get("recipients") or [])
        if isinstance(item, dict) and item.get("channel") == channel and str(item.get("address") or "").strip()
    ]


async def queue_alerts(
    db: AsyncSession,
    scope_id: UUID,
    alerts: list[Alert],
    config: dict,
    *,
    now: datetime,
    available_channels: set[str] | None = None,
) -> list[ControlRoomAlertDelivery]:
    """Write one ``pending`` delivery row per fired alert × reachable channel.

    A (rule, channel, recipient) that already has a pending, sending, sent
    or recently failed row inside the rule's cooldown window is not queued
    again — this is the crash-safe dedupe that survives worker restarts.
    When no configured channel is reachable for an alert, one ``in_app``
    row is queued instead so the alert is never silently dropped. A
    cooldown-blocked alert is NOT re-queued as ``in_app`` either: the
    block means the alert was already delivered or attempted recently,
    and the fallback must not resurrect it.
    """
    rules = {rule.key: rule for rule in effective_rules(config)}
    available = available_channels if available_channels is not None else {"whatsapp", "email", "in_app"}
    existing = (
        await db.execute(
            select(ControlRoomAlertDelivery).where(
                ControlRoomAlertDelivery.scope_id == scope_id,
                ControlRoomAlertDelivery.status.in_(("pending", "sending", "sent", "failed")),
            )
        )
    ).scalars().all()

    def _blocked(rule_key: str, channel: str, recipient: str | None) -> bool:
        rule = rules.get(rule_key)
        if rule is None:
            return True
        cutoff = now - timedelta(seconds=rule.cooldown_seconds)
        # Compare against evaluated_at, not created_at: evaluated_at is the
        # dispatch clock (the same clock the evaluator's cooldown uses),
        # while created_at is the database clock. In production the two
        # agree to the second; when they disagree, the dispatch clock is
        # the one the cooldown is defined on.
        return any(
            row.rule_key == rule_key
            and row.channel == channel
            and row.recipient == recipient
            and row.evaluated_at is not None
            and row.evaluated_at >= cutoff
            for row in existing
        )

    queued: list[ControlRoomAlertDelivery] = []
    for alert in alerts:
        placed = False
        blocked = False
        for channel in rule_channels(config, alert.rule_key):
            if channel == "in_app":
                continue  # in-app is the fallback below, not a first-class send
            if channel not in available:
                continue
            for recipient in _recipients_for(config, channel):
                if _blocked(alert.rule_key, channel, recipient):
                    blocked = True
                    continue
                row = ControlRoomAlertDelivery(
                    scope_id=scope_id,
                    rule_key=alert.rule_key,
                    severity=alert.severity,
                    title=alert.title,
                    detail=alert.detail,
                    channel=channel,
                    recipient=recipient,
                    status="pending",
                    evaluated_at=alert.evaluated_at,
                )
                db.add(row)
                queued.append(row)
                placed = True
        if not placed and not blocked and not _blocked(alert.rule_key, "in_app", None):
            row = ControlRoomAlertDelivery(
                scope_id=scope_id,
                rule_key=alert.rule_key,
                severity=alert.severity,
                title=alert.title,
                detail=alert.detail,
                channel="in_app",
                recipient=None,
                status="pending",
                evaluated_at=alert.evaluated_at,
            )
            db.add(row)
            queued.append(row)
    await db.flush()
    return queued


async def send_pending(
    db: AsyncSession,
    *,
    sender: ChannelSender,
    now: datetime,
    limit: int = _SEND_BATCH,
) -> dict[str, int]:
    """Claim pending rows (SKIP LOCKED) and send them; audit every outcome."""
    claimed = (
        await db.execute(
            select(ControlRoomAlertDelivery)
            .where(ControlRoomAlertDelivery.status == "pending")
            .order_by(ControlRoomAlertDelivery.created_at)
            .limit(limit)
            .with_for_update(of=ControlRoomAlertDelivery, skip_locked=True)
        )
    ).scalars().all()
    for row in claimed:
        row.status = "sending"
    if claimed:
        await db.commit()

    sent = failed = 0
    for row in claimed:
        message = format_alert_message(
            Alert(
                rule_key=row.rule_key,
                severity=row.severity,
                title=row.title,
                detail=row.detail,
                evaluated_at=row.evaluated_at,
            )
        )
        if row.channel == "in_app":
            # In-app delivery is the row itself: no transport is ever
            # invoked, the alert simply becomes visible on the dashboard.
            provider_id = None
        else:
            try:
                provider_id = await sender.send(row.channel, row.recipient, message)
            except Exception as exc:  # transport failure: keep the row for retry after cooldown
                logger.warning("Control room alert send failed (%s %s): %s", row.rule_key, row.channel, type(exc).__name__)
                row.status = "failed"
                row.error = str(exc)[:500]
                await db.commit()
                failed += 1
                continue
        row.status = "sent"
        row.sent_at = now
        audit_record(
            db,
            AuditContext(None, None, None, None),
            _ALERT_AUDIT_ACTION,
            target=f"control_room:{row.scope_id}:{row.rule_key}",
            details={
                "rule_key": row.rule_key,
                "severity": row.severity,
                "channel": row.channel,
                "recipient": row.recipient,
                "provider_id": provider_id,
            },
        )
        await db.commit()
        sent += 1
    return {"claimed": len(claimed), "sent": sent, "failed": failed}


class DefaultAlertSender(ChannelSender):
    """WhatsApp first, email second, in-app as the always-on fallback.

    WhatsApp reuses the tested Step F Cloud send path
    (``app.integrations.whatsapp.send_text``) against the first enabled
    connection; with no connection configured it raises
    :class:`ChannelUnavailableError` so the dispatcher falls back. Email
    uses the configured SMTP transport; with no SMTP host configured it
    raises the same. ``in_app`` never sends anything — the delivery row
    itself is the record, visible on the dashboard.
    """

    def __init__(self, db: AsyncSession, settings, cipher):
        self._db = db
        self._settings = settings
        self._cipher = cipher
        self._connection = None

    async def _whatsapp_connection(self):
        if self._connection is None:
            from app.db.agent_studio_whatsapp_models import AgentStudioWhatsAppConnection

            self._connection = await self._db.scalar(
                select(AgentStudioWhatsAppConnection)
                .where(AgentStudioWhatsAppConnection.enabled.is_(True))
                .order_by(AgentStudioWhatsAppConnection.id)
                .limit(1)
            )
        return self._connection

    async def available_channels(self) -> set[str]:
        channels = {"in_app"}
        if await self._whatsapp_connection() is not None:
            channels.add("whatsapp")
        if bool(getattr(self._settings, "personal_task_smtp_host", "") or "").strip() and bool(
            getattr(self._settings, "personal_task_email_from", "") or ""
        ).strip():
            channels.add("email")
        return channels

    async def send(self, channel: str, recipient: str | None, message: str) -> str | None:
        if channel == "in_app":
            return None
        if channel == "whatsapp":
            from app.integrations.whatsapp import send_text

            connection = await self._whatsapp_connection()
            if connection is None:
                raise ChannelUnavailableError("no_whatsapp_connection")
            if not recipient:
                raise ChannelUnavailableError("no_whatsapp_recipient")
            return await send_text(connection, SimpleNamespace(from_wa_id=recipient), self._cipher, message)
        if channel == "email":
            from app.services.personal_task_email import deliver_message

            if not recipient:
                raise ChannelUnavailableError("no_email_recipient")
            email = EmailMessage()
            email["Subject"] = f"[InfraSpine alert] {message.split(' — ', 1)[0]}"
            email["From"] = self._settings.personal_task_email_from
            email["To"] = recipient
            email.set_content(message)
            try:
                await asyncio.to_thread(deliver_message, self._settings, email)
            except Exception as exc:
                raise ChannelUnavailableError(f"email_failed: {type(exc).__name__}") from exc
            return None
        raise ChannelUnavailableError(f"unknown_channel:{channel}")


async def dispatch_alerts(
    db: AsyncSession,
    scope_id: UUID,
    settings,
    *,
    now: datetime | None = None,
    sender: ChannelSender | None = None,
    config: dict | None = None,
    cipher=None,
) -> dict:
    """Evaluate + queue + send alerts for one scope. Returns a summary.

    A disabled scope config short-circuits (nothing evaluated, nothing
    queued). The sender defaults to :class:`DefaultAlertSender`; tests pass
    a recording sender instead.
    """
    now = now or datetime.now(UTC)
    if config is None:
        config = await _load_config(db, scope_id)
    if not config.get("enabled", True):
        return {"scope_id": str(scope_id), "evaluated": 0, "queued": 0, "sent": 0, "failed": 0, "disabled": True}
    sender = sender or DefaultAlertSender(db, settings, cipher)
    available = await sender.available_channels() if isinstance(sender, DefaultAlertSender) else {"whatsapp", "email", "in_app"}
    snapshot = await collect_snapshot(db, scope_id, settings, now=now)
    rules = effective_rules(config)
    sent_at = await last_sent_map(db, scope_id)
    quiet_start = config.get("quiet_start") or DEFAULT_QUIET_START
    quiet_end = config.get("quiet_end") or DEFAULT_QUIET_END
    if isinstance(quiet_start, str):
        quiet_start = datetime.strptime(quiet_start, "%H:%M").time()
    if isinstance(quiet_end, str):
        quiet_end = datetime.strptime(quiet_end, "%H:%M").time()
    alerts = evaluate_alerts(snapshot, now=now, rules=rules, quiet_start=quiet_start, quiet_end=quiet_end, sent_at=sent_at)
    queued = await queue_alerts(db, scope_id, alerts, config, now=now, available_channels=available)
    await db.commit()
    result = await send_pending(db, sender=sender, now=now)
    return {
        "scope_id": str(scope_id),
        "evaluated": len(alerts),
        "queued": len(queued),
        "sent": result["sent"],
        "failed": result["failed"],
        "disabled": False,
    }


async def discover_scopes(db: AsyncSession) -> list[UUID]:
    """Scopes that can produce alerts: every defined workflow scope + every scope with a saved config."""
    defined = set((await db.execute(select(Workflow.scope_id).distinct())).scalars().all())
    configured = set((await db.execute(select(ControlRoomAlertConfig.scope_id).distinct())).scalars().all())
    return sorted(defined | configured)


async def process_control_room_alerts(sessionmaker: async_sessionmaker[AsyncSession], settings) -> dict:
    """One dispatch pass over every discoverable scope. Never raises."""
    from app.core.crypto import CredentialCipher

    cipher = CredentialCipher(settings.master_key_bytes, settings.master_encryption_key_id)
    totals = {"scopes": 0, "evaluated": 0, "queued": 0, "sent": 0, "failed": 0}
    try:
        async with sessionmaker() as db:
            scope_ids = await discover_scopes(db)
        for scope_id in scope_ids:
            try:
                async with sessionmaker() as db:
                    summary = await dispatch_alerts(db, scope_id, settings, cipher=cipher)
            except Exception as exc:
                logger.warning("Control room alert dispatch failed for scope %s (%s)", scope_id, type(exc).__name__)
                totals["failed"] += 1
                continue
            totals["scopes"] += 1
            totals["evaluated"] += summary["evaluated"]
            totals["queued"] += summary["queued"]
            totals["sent"] += summary["sent"]
            totals["failed"] += summary["failed"]
    except Exception as exc:
        logger.warning("Control room alert dispatch failed (%s)", type(exc).__name__)
    return totals


async def run_alert_dispatch(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings,
    stop: asyncio.Event,
    *,
    interval_seconds: float = 300,
) -> None:
    """Background scheduler; retries on the next tick and logs only exception types."""
    while not stop.is_set():
        try:
            await process_control_room_alerts(sessionmaker, settings)
        except Exception as exc:
            logger.warning("Control room alert scheduler failed (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
