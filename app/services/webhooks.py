"""Outgoing webhooks for request and approval events.

Events are queued in the same transaction as the change that caused them, then delivered by the worker as a
JSON POST signed with HMAC-SHA256 over "<timestamp>.<body>" (header `X-InfraSpine-Signature: t=<ts>,v1=<hex>`).
Destinations go through the same outbound guard as custom AI providers: HTTPS only, no private, internal or
metadata addresses (unless the deployment lists the origin in WEBHOOK_TRUSTED_PRIVATE_ORIGINS), no redirects,
DNS checked at connect time. Failed deliveries are retried with backoff up to 8 times; after 20 failures in a
row the webhook is paused. HR requests never produce webhook events, and payloads carry no request text other
than the subject.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.crypto import EncryptedValue, RotatingCredentialCipher, rotating_cipher_from_settings
from app.core.errors import InvalidInputError, NotFoundError
from app.db.models import AccessGroup, ServiceRequest, User
from app.db.webhook_models import WEBHOOK_EVENTS, Webhook, WebhookDelivery
from app.providers.endpoint_security import (
    EndpointPolicy, UnsafeEndpointError, blocked_reason, guarded_http_client, normalize_base_url, parse_trusted_origins,
    validate_endpoint,
)
from app.services import audit
from app.services.audit import AuditContext

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 8
PAUSE_AFTER_FAILURES = 20
TIMEOUT_SECONDS = 10.0
BATCH_SIZE = 25
CLAIM_SECONDS = 120
POLL_SECONDS = 15
USER_AGENT = "InfraSpine-Webhooks/1"


def policy(settings: Settings | None = None) -> EndpointPolicy:
    return EndpointPolicy(parse_trusted_origins((settings or get_settings()).webhook_trusted_private_origins))


def _cipher(settings: Settings | None = None) -> RotatingCredentialCipher:
    settings = settings or get_settings()
    return rotating_cipher_from_settings(settings)


def _aad(webhook_id: uuid.UUID) -> bytes:
    return f"infraspine:webhook-secret:{webhook_id}".encode()


def _secret(webhook: Webhook) -> str:
    return _cipher().decrypt(EncryptedValue(webhook.secret_ciphertext, webhook.secret_nonce, webhook.secret_key_id), associated_data=_aad(webhook.id))


def sign(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------------------------------- queueing
async def enqueue(db: AsyncSession, event: str, data: dict, *, only: uuid.UUID | None = None) -> int:
    """Queue the event for every active webhook subscribed to it (or just `only`)."""
    query = select(Webhook).where(Webhook.is_active.is_(True), Webhook.is_archived.is_(False))
    if only is not None:
        query = query.where(Webhook.id == only)
    hooks = [h for h in (await db.scalars(query)).all() if only is not None or event in h.events]
    now = datetime.now(UTC)
    for hook in hooks:
        delivery_id = uuid.uuid4()
        db.add(WebhookDelivery(id=delivery_id, webhook_id=hook.id, event=event, created_at=now, payload={
            "id": str(delivery_id), "event": event, "created_at": now.isoformat(), "data": data,
        }))
    return len(hooks)


async def request_event(db: AsyncSession, request: ServiceRequest, event: str, **extra) -> int:
    """Queue a request event. HR requests are private and never leave the system this way."""
    if request.category == "hr":
        return 0
    department = await db.get(AccessGroup, request.department_group_id) if request.department_group_id else None
    data = {
        "request_id": str(request.id), "category": request.category, "status": request.status, "subject": request.subject,
        "priority": request.priority, "department": department.name if department else None,
        "amount": str(request.amount) if request.amount is not None else None, "currency": request.currency,
        "form": request.form_name, "created_at": request.created_at.isoformat() if request.created_at else None, **extra,
    }
    return await enqueue(db, event, data)


# ------------------------------------------------------------------------------------------- admin
class WebhookService:
    def __init__(self, db: AsyncSession, admin: User, ctx: AuditContext) -> None:
        self.db = db
        self.admin = admin
        self.ctx = ctx

    async def list(self) -> list[tuple[Webhook, WebhookDelivery | None]]:
        hooks = (await self.db.scalars(select(Webhook).where(Webhook.is_archived.is_(False)).order_by(Webhook.created_at))).all()
        out = []
        for hook in hooks:
            last = await self.db.scalar(select(WebhookDelivery).where(WebhookDelivery.webhook_id == hook.id)
                                        .order_by(WebhookDelivery.created_at.desc()).limit(1))
            out.append((hook, last))
        return out

    async def _get(self, webhook_id: uuid.UUID) -> Webhook:
        hook = await self.db.get(Webhook, webhook_id, with_for_update=True)
        if hook is None or hook.is_archived:
            raise NotFoundError("Webhook not found.")
        return hook

    @staticmethod
    def _events(events: list[str]) -> list[str]:
        clean = list(dict.fromkeys(events))
        if not clean or any(e not in WEBHOOK_EVENTS for e in clean):
            raise InvalidInputError("Choose at least one supported event.")
        return clean

    async def _url(self, raw: str) -> str:
        try:
            return (await validate_endpoint(raw, policy())).base_url
        except UnsafeEndpointError as exc:
            raise InvalidInputError(exc.message.replace("API URL", "webhook URL").replace("API base URL", "webhook URL")) from None

    def _store_secret(self, hook: Webhook) -> str:
        secret = "whsec_" + secrets.token_urlsafe(32)
        encrypted = _cipher().encrypt(secret, associated_data=_aad(hook.id))
        hook.secret_ciphertext, hook.secret_nonce, hook.secret_key_id = encrypted.ciphertext, encrypted.nonce, encrypted.key_id
        return secret

    async def create(self, *, name: str, url: str, events: list[str]) -> tuple[Webhook, str]:
        hook = Webhook(id=uuid.uuid4(), name=" ".join(name.split()), url=await self._url(url), events=self._events(events), created_by=self.admin.id)
        secret = self._store_secret(hook)
        self.db.add(hook)
        audit.record(self.db, self.ctx, "webhook.created", target=hook.name, details={"url": hook.url, "events": hook.events})
        await self.db.commit()
        return hook, secret

    async def update(self, webhook_id: uuid.UUID, *, name: str | None, url: str | None, events: list[str] | None, is_active: bool | None) -> Webhook:
        hook = await self._get(webhook_id)
        if name is not None:
            hook.name = " ".join(name.split())
        if url is not None:
            hook.url = await self._url(url)
        if events is not None:
            hook.events = self._events(events)
        if is_active is not None:
            hook.is_active = is_active
            if is_active:
                hook.consecutive_failures, hook.disabled_reason = 0, None
        hook.updated_at = datetime.now(UTC)
        audit.record(self.db, self.ctx, "webhook.changed", target=hook.name, details={"url": hook.url, "events": hook.events, "active": hook.is_active})
        await self.db.commit()
        return hook

    async def rotate_secret(self, webhook_id: uuid.UUID) -> str:
        hook = await self._get(webhook_id)
        secret = self._store_secret(hook)
        hook.updated_at = datetime.now(UTC)
        audit.record(self.db, self.ctx, "webhook.secret_rotated", target=hook.name)
        await self.db.commit()
        return secret

    async def archive(self, webhook_id: uuid.UUID) -> None:
        hook = await self._get(webhook_id)
        hook.is_archived, hook.is_active = True, False
        await self.db.execute(WebhookDelivery.__table__.update().where(
            WebhookDelivery.webhook_id == hook.id, WebhookDelivery.status.in_(("pending", "retry"))).values(status="cancelled"))
        audit.record(self.db, self.ctx, "webhook.deleted", target=hook.name)
        await self.db.commit()

    async def send_test(self, webhook_id: uuid.UUID) -> None:
        hook = await self._get(webhook_id)
        await enqueue(self.db, "webhook.test", {"message": "Test delivery from InfraSpine AI.", "webhook": hook.name}, only=hook.id)
        audit.record(self.db, self.ctx, "webhook.test_queued", target=hook.name)
        await self.db.commit()

    async def deliveries(self, webhook_id: uuid.UUID, limit: int = 50) -> list[WebhookDelivery]:
        await self._get(webhook_id)
        return list((await self.db.scalars(select(WebhookDelivery).where(WebhookDelivery.webhook_id == webhook_id)
                                           .order_by(WebhookDelivery.created_at.desc()).limit(limit))).all())

    async def redeliver(self, delivery_id: uuid.UUID) -> None:
        row = await self.db.get(WebhookDelivery, delivery_id, with_for_update=True)
        if row is None:
            raise NotFoundError("Delivery not found.")
        hook = await self._get(row.webhook_id)
        if row.status in ("pending", "sending", "retry"):
            raise InvalidInputError("This delivery is still being attempted.")
        await enqueue(self.db, row.event, row.payload["data"], only=hook.id)
        audit.record(self.db, self.ctx, "webhook.redelivered", target=hook.name, details={"delivery": str(row.id)})
        await self.db.commit()


# ------------------------------------------------------------------------------------------- delivery
async def post(url: str, headers: dict[str, str], body: bytes, settings: Settings | None = None) -> int:
    """POST through the outbound guard; returns the HTTP status. Tests replace this function."""
    endpoint = normalize_base_url(url)
    async with guarded_http_client(endpoint, policy(settings)) as client:
        response = await client.post(url, content=body, headers=headers, timeout=TIMEOUT_SECONDS)
        return response.status_code


async def deliver_due(sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime | None = None, settings: Settings | None = None) -> int:
    """Claim and send a batch of due deliveries; returns how many were sent successfully."""
    current = now or datetime.now(UTC)
    due = or_(
        and_(WebhookDelivery.status.in_(("pending", "retry")), or_(WebhookDelivery.next_attempt_at.is_(None), WebhookDelivery.next_attempt_at <= current)),
        and_(WebhookDelivery.status == "sending", WebhookDelivery.claimed_until <= current),
    )
    async with sessionmaker() as db:
        rows = (await db.execute(
            select(WebhookDelivery, Webhook).join(Webhook, Webhook.id == WebhookDelivery.webhook_id).where(due)
            .order_by(WebhookDelivery.created_at).limit(BATCH_SIZE).with_for_update(of=WebhookDelivery, skip_locked=True)
        )).all()
        claimed = []
        for delivery, hook in rows:
            if not hook.is_active or hook.is_archived:
                delivery.status, delivery.claimed_until = "cancelled", None
                continue
            delivery.status, delivery.attempts = "sending", delivery.attempts + 1
            delivery.claimed_until = current + timedelta(seconds=CLAIM_SECONDS)
            claimed.append((delivery.id, hook.id, hook.url, _secret(hook), delivery.event, delivery.payload, delivery.attempts))
        await db.commit()

    sent = 0
    for delivery_id, hook_id, url, secret, event, payload, attempt in claimed:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        timestamp = int(current.timestamp())
        headers = {
            "Content-Type": "application/json", "User-Agent": USER_AGENT, "X-InfraSpine-Event": event,
            "X-InfraSpine-Delivery": str(delivery_id), "X-InfraSpine-Signature": f"t={timestamp},v1={sign(secret, timestamp, body)}",
        }
        started = time.monotonic()
        status, error = None, None
        try:
            status = await post(url, headers, body, settings)
            if not 200 <= status < 300:
                error = f"Receiver answered HTTP {status}."
        except Exception as exc:  # network, TLS, timeout or a blocked destination
            blocked = blocked_reason(exc)
            error = (blocked.message.replace("API URL", "webhook URL") if blocked else f"{type(exc).__name__}")[:200]
        elapsed = int((time.monotonic() - started) * 1000)
        async with sessionmaker() as db:
            delivery = await db.get(WebhookDelivery, delivery_id, with_for_update=True)
            hook = await db.get(Webhook, hook_id, with_for_update=True)
            if delivery is None or delivery.status != "sending" or hook is None:
                continue
            delivery.response_status, delivery.duration_ms, delivery.claimed_until, delivery.last_error = status, elapsed, None, error
            if error is None:
                delivery.status, delivery.sent_at, delivery.next_attempt_at = "sent", current, None
                hook.consecutive_failures = 0
                sent += 1
            else:
                retry = attempt < MAX_ATTEMPTS
                delivery.status = "retry" if retry else "failed"
                delivery.next_attempt_at = current + timedelta(seconds=min(6 * 3600, 30 * 2 ** (attempt - 1))) if retry else None
                hook.consecutive_failures += 1
                if hook.consecutive_failures >= PAUSE_AFTER_FAILURES and hook.is_active:
                    hook.is_active, hook.disabled_reason = False, f"Paused after {PAUSE_AFTER_FAILURES} failed deliveries in a row."
            await db.commit()
    return sent


async def run_webhook_worker(sessionmaker: async_sessionmaker[AsyncSession], stop: asyncio.Event, *, interval_seconds: float = POLL_SECONDS) -> None:
    while not stop.is_set():
        try:
            await deliver_due(sessionmaker)
        except Exception as exc:
            logger.warning("Webhook delivery pass failed (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
