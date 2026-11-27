"""Authenticated connector lifecycle, immutable approvals, and signed event intake."""
import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.integrations.adapters import validate_operation
from app.integrations.catalog import CATALOG, READ_OPERATIONS
from app.integrations.models import Connector, IntegrationEvent, IntegrationJob, IntegrationRecord
from app.integrations.schemas import EventInput
from app.integrations.security import authorize, canonical, digest, scope, seal, unseal
from app.providers.endpoint_security import EndpointPolicy, UnsafeEndpointError, check_endpoint, normalize_base_url
from app.services import audit
from app.services.audit import AuditContext

MAX_INPUT = 3_000_000
MAX_WEBHOOK = 1_000_000


def public_connector(row):
    return {name: getattr(row, name) for name in ("id", "tenant_id", "owner_id", "name", "provider", "base_url", "config", "enabled", "version", "health", "last_error", "verified_at", "last_sync_at", "next_sync_at", "circuit_until", "created_at")} | {"credential_configured": bool(row.credentials), "webhook_url": f"/api/integrations/webhooks/{row.id}"}


def public_job(row):
    return {name: getattr(row, name) for name in ("id", "connector_id", "operation", "status", "attempts", "digest", "connector_version", "result", "last_error", "duration_ms", "created_at", "available_at", "completed_at", "approval_expires_at", "approved_at")}


class IntegrationService:
    def __init__(self, db, user, cipher, ctx):
        self.db, self.user, self.cipher, self.ctx = db, user, cipher, ctx

    async def authorize(self, tenant_id, *, write=False):
        return await authorize(self.db, tenant_id, self.user.id, write=write, lock=write)

    def record(self, action, row, **details):
        audit.record(self.db, self.ctx, "integration." + action, target=str(row.id), details={"tenant_id": str(row.tenant_id), **details})

    async def connector(self, tenant_id, connector_id, *, write=False):
        await self.authorize(tenant_id, write=write)
        query = select(Connector).where(*scope(Connector, tenant_id, self.user.id), Connector.id == connector_id)
        if write: query = query.with_for_update()
        row = await self.db.scalar(query.execution_options(populate_existing=True))
        if row is None: raise NotFoundError("Connector not found.")
        return row

    async def create(self, tenant_id, data):
        await self.authorize(tenant_id, write=True)
        try:
            endpoint = normalize_base_url(data.base_url)
            check_endpoint(endpoint, EndpointPolicy())
        except UnsafeEndpointError:
            raise InvalidInputError("Use a public HTTPS endpoint without credentials, query parameters or redirects.") from None
        definition = CATALOG[data.provider]
        if definition["base_url"] and endpoint.base_url != definition["base_url"]:
            raise InvalidInputError("Use the official base URL for this provider.")
        if data.provider not in ("rest", "graphql", "soap") and not set(data.config.scopes) <= set(definition["scopes"]):
            raise InvalidInputError("Unsupported provider scope.")
        row = Connector(id=uuid.uuid4(), tenant_id=tenant_id, owner_id=self.user.id, name=data.name.strip(),
                        provider=data.provider, base_url=endpoint.base_url, config=data.config.model_dump(mode="json"),
                        enabled=True, version=1, health="unverified", failures=0)
        credential = data.credentials.plaintext()
        credential["webhook_secret"] = credential["webhook_secret"] or secrets.token_urlsafe(32)
        row.credentials = seal(self.cipher, "credentials", row, credential)
        if data.config.interval_seconds:
            row.next_sync_at = datetime.now(UTC) + timedelta(seconds=data.config.interval_seconds)
        self.db.add(row)
        self.record("connector_created", row, provider=row.provider)
        await self.db.commit(); await self.db.refresh(row)
        return public_connector(row), credential["webhook_secret"]

    async def patch(self, tenant_id, connector_id, data):
        row = await self.connector(tenant_id, connector_id, write=True)
        if row.version != data.version: raise ConflictError()
        if data.name is not None: row.name = data.name.strip()
        if data.enabled is not None: row.enabled = data.enabled
        if data.config is not None:
            if row.provider not in ("rest", "graphql", "soap") and not set(data.config.scopes) <= set(CATALOG[row.provider]["scopes"]):
                raise InvalidInputError("Unsupported provider scope.")
            row.config = data.config.model_dump(mode="json")
            row.cursor = None
        if data.credentials is not None:
            current = unseal(self.cipher, "credentials", row, row.credentials)
            current.update({k: v for k, v in data.credentials.plaintext().items() if v is not None and v != ""})
            row.credentials = seal(self.cipher, "credentials", row, current)
        row.version += 1; row.health = "unverified" if row.enabled else "disabled"
        row.verified_at = None; row.circuit_until = None; row.failures = 0
        interval = row.config["interval_seconds"]
        row.next_sync_at = datetime.now(UTC) + timedelta(seconds=interval) if row.enabled and interval else None
        self.record("connector_changed", row, version=row.version, enabled=row.enabled)
        await self.db.commit(); await self.db.refresh(row)
        return public_connector(row)

    async def revoke(self, tenant_id, connector_id):
        row = await self.connector(tenant_id, connector_id, write=True)
        row.credentials = {}; row.enabled = False; row.version += 1; row.health = "revoked"; row.next_sync_at = None
        self.record("credentials_revoked_locally", row)
        await self.db.commit()
        return {"revoked": True, "provider_revocation_verified": False}

    async def enqueue(self, tenant_id, connector_id, data):
        row = await self.connector(tenant_id, connector_id, write=True)
        if not row.enabled or not row.credentials: raise InvalidInputError("Enable and configure this connector first.")
        payload = validate_operation(row, data.operation, data.payload)
        if len(canonical(payload).encode()) > MAX_INPUT: raise InvalidInputError("Action exceeds the payload limit.")
        fingerprint = digest({"operation": data.operation, "payload": payload, "version": row.version})
        existing = await self.db.scalar(select(IntegrationJob).where(IntegrationJob.connector_id == row.id, IntegrationJob.idempotency_key == data.idempotency_key))
        if existing:
            if existing.digest != fingerprint: raise ConflictError("This idempotency key belongs to a different action.")
            return public_job(existing)
        pending = data.operation not in READ_OPERATIONS
        job = IntegrationJob(id=uuid.uuid4(), tenant_id=row.tenant_id, owner_id=row.owner_id, connector_id=row.id,
                             connector_version=row.version, operation=data.operation, idempotency_key=data.idempotency_key,
                             digest=fingerprint, status="pending_approval" if pending else "queued", attempts=0,
                             approval_expires_at=datetime.now(UTC) + timedelta(hours=1) if pending else None)
        job.payload = seal(self.cipher, "job", job, payload)
        self.db.add(job); self.record("action_requested", job, operation=job.operation, digest=job.digest)
        await self.db.commit(); await self.db.refresh(job)
        return public_job(job)

    async def job(self, tenant_id, job_id, *, write=False):
        await self.authorize(tenant_id, write=write)
        query = select(IntegrationJob).where(*scope(IntegrationJob, tenant_id, self.user.id), IntegrationJob.id == job_id)
        if write: query = query.with_for_update()
        job = await self.db.scalar(query.execution_options(populate_existing=True))
        if job is None: raise NotFoundError("Integration action not found.")
        return job

    async def decision(self, tenant_id, job_id, data):
        job = await self.job(tenant_id, job_id, write=True)
        if job.status != "pending_approval": raise ConflictError("This action is no longer awaiting approval.")
        if not hmac.compare_digest(job.digest, data.digest): raise ConflictError("Review the saved action before deciding.")
        if job.approval_expires_at <= datetime.now(UTC): raise ConflictError("Approval expired. Create a new action.")
        job.status = "queued" if data.approve else "cancelled"
        if data.approve:
            job.approved_at = datetime.now(UTC); job.approved_by = self.user.id
        self.record("action_approved" if data.approve else "action_rejected", job, digest=job.digest)
        await self.db.commit(); await self.db.refresh(job)
        return public_job(job)

    async def retry(self, tenant_id, job_id):
        job = await self.job(tenant_id, job_id, write=True)
        if job.status != "dead_letter" or (job.operation not in READ_OPERATIONS and job.last_error != "rate_limited"):
            raise ConflictError("Only definitely unsubmitted or read-only failures can be retried.")
        job.attempts = 0; job.claim_token = None; job.lease_until = None; job.available_at = datetime.now(UTC)
        job.status = "queued"
        if job.operation not in READ_OPERATIONS:
            job.status = "pending_approval"; job.approved_at = None; job.approved_by = None
            job.approval_expires_at = datetime.now(UTC) + timedelta(hours=1)
        self.record("retry_requested", job)
        await self.db.commit(); await self.db.refresh(job)
        return public_job(job)

    async def cancel(self, tenant_id, job_id):
        job = await self.job(tenant_id, job_id, write=True)
        if job.status not in ("queued", "pending_approval"): raise ConflictError("Only waiting actions can be cancelled.")
        job.status = "cancelled"; self.record("action_cancelled", job)
        await self.db.commit()
        return public_job(job)

    async def overview(self, tenant_id):
        member = await self.authorize(tenant_id)
        connectors = (await self.db.scalars(select(Connector).where(*scope(Connector, tenant_id, self.user.id)).order_by(Connector.created_at.desc()).limit(200))).all()
        jobs = (await self.db.scalars(select(IntegrationJob).where(*scope(IntegrationJob, tenant_id, self.user.id)).order_by(IntegrationJob.created_at.desc()).limit(100))).all()
        events = (await self.db.scalars(select(IntegrationEvent).where(*scope(IntegrationEvent, tenant_id, self.user.id)).order_by(IntegrationEvent.created_at.desc()).limit(100))).all()
        counts = dict((await self.db.execute(select(IntegrationJob.status, func.count()).where(*scope(IntegrationJob, tenant_id, self.user.id)).group_by(IntegrationJob.status))).all())
        latency = await self.db.scalar(select(func.percentile_cont(.95).within_group(IntegrationJob.duration_ms)).where(*scope(IntegrationJob, tenant_id, self.user.id), IntegrationJob.duration_ms.is_not(None)))
        records = await self.db.scalar(select(func.count()).select_from(IntegrationRecord).where(*scope(IntegrationRecord, tenant_id, self.user.id)))
        return {"role": member.role, "connectors": [public_connector(x) for x in connectors], "jobs": [public_job(x) for x in jobs],
                "events": [{k: getattr(x, k) for k in ("id", "connector_id", "event_type", "target_module", "status", "created_at", "consumer_reference")} for x in events],
                "metrics": {"jobs_by_status": counts, "records": records, "latency_p95_ms": latency, "window": "all_time"},
                "list_limits": {"connectors": 200, "jobs": 100, "events": 100}}


async def accept_event(db, cipher, connector_id, raw, signature):
    if len(raw) > MAX_WEBHOOK: raise InvalidInputError("Webhook exceeds the payload limit.")
    # One connector lock serializes duplicate delivery without exposing unique-constraint errors.
    row = await db.scalar(select(Connector).where(Connector.id == connector_id).with_for_update())
    if row is None or not row.enabled or not row.credentials: raise NotFoundError()
    await authorize(db, row.tenant_id, row.owner_id, write=True, lock=True)
    try:
        timestamp, supplied = signature.split(",v1=")
        if not timestamp.startswith("t="): raise ValueError()
        timestamp = timestamp[2:]
        if len(timestamp) > 12 or abs(time.time() - int(timestamp)) > 300: raise ValueError()
        secret = unseal(cipher, "credentials", row, row.credentials)["webhook_secret"]
        expected = hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied): raise ValueError()
    except (ValueError, KeyError):
        raise InvalidInputError("Webhook signature is invalid or expired.") from None
    try:
        event = EventInput.model_validate_json(raw)
    except ValueError:
        raise InvalidInputError("Invalid event envelope.") from None
    if event.type not in row.config["event_types"]: raise InvalidInputError("Event type is not subscribed.")
    fingerprint = digest(event.model_dump(mode="json"))
    prior = await db.scalar(select(IntegrationEvent).where(IntegrationEvent.connector_id == row.id, IntegrationEvent.external_id == event.id))
    if prior:
        if prior.digest != fingerprint: raise ConflictError("An event with this identifier has different content.")
        return {"accepted": True, "duplicate": True, "event_id": str(prior.id)}
    stored = IntegrationEvent(id=uuid.uuid4(), tenant_id=row.tenant_id, owner_id=row.owner_id, connector_id=row.id,
                              external_id=event.id, event_type=event.type, digest=fingerprint,
                              target_module=row.config["target_module"], status="received")
    stored.payload = seal(cipher, "event", stored, event.model_dump(mode="json"))
    db.add(stored)
    audit.record(db, AuditContext(row.owner_id, None, None, None), "integration.webhook_received", target=str(stored.id),
                 details={"tenant_id": str(row.tenant_id), "connector_id": str(row.id), "digest": fingerprint})
    await db.commit()
    return {"accepted": True, "duplicate": False, "event_id": str(stored.id)}
