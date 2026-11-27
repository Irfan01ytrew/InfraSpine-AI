"""Durable bounded worker. Ambiguous writes require reconciliation, never blind replay."""
import asyncio
import logging
import random
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select

from app.core.errors import AppError
from app.integrations import adapters
from app.integrations.catalog import READ_OPERATIONS
from app.integrations.models import Connector, IntegrationJob, IntegrationRecord
from app.integrations.oauth import usable_secret
from app.integrations.security import authorize, digest, seal, unseal
from app.integrations.transport import TransportError
from app.services import audit
from app.services.audit import AuditContext

logger = logging.getLogger(__name__)
LEASE_SECONDS = 600
MAX_ATTEMPTS = 5


def evidence(db, job, event, **details):
    audit.record(db, AuditContext(job.owner_id, None, None, None), "integration." + event, target=str(job.id),
                 details={"tenant_id": str(job.tenant_id), "connector_id": str(job.connector_id), **details})


async def schedule(sessionmaker, cipher):
    async with sessionmaker() as db:
        now = datetime.now(UTC)
        rows = (await db.scalars(select(Connector).where(Connector.enabled.is_(True), Connector.next_sync_at <= now)
                                .with_for_update(skip_locked=True).limit(20))).all()
        for row in rows:
            interval = row.config.get("interval_seconds", 0)
            row.next_sync_at = now + timedelta(seconds=max(60, interval)) if interval else None
            try:
                await authorize(db, row.tenant_id, row.owner_id, write=True)
                adapters.validate_operation(row, "sync", {})
            except AppError:
                row.health = "permission_required"; row.next_sync_at = None
                continue
            active = await db.scalar(select(IntegrationJob.id).where(IntegrationJob.connector_id == row.id,
                IntegrationJob.operation == "sync", IntegrationJob.status.in_(("queued", "processing"))).limit(1))
            if active: continue
            job = IntegrationJob(id=uuid.uuid4(), tenant_id=row.tenant_id, owner_id=row.owner_id, connector_id=row.id,
                connector_version=row.version, operation="sync", idempotency_key=f"schedule:{now.isoformat()}",
                digest=digest({"operation": "sync", "payload": {}, "version": row.version}), status="queued", attempts=0)
            job.payload = seal(cipher, "job", job, {})
            db.add(job); evidence(db, job, "sync_scheduled")
        await db.commit()


async def claim(sessionmaker):
    async with sessionmaker() as db:
        now = datetime.now(UTC)
        expired = (await db.scalars(select(IntegrationJob).where(IntegrationJob.status.in_(("processing", "dispatching")),
                    IntegrationJob.lease_until <= now).with_for_update(skip_locked=True).limit(50))).all()
        for job in expired:
            if job.status == "dispatching":
                job.status, job.last_error = "uncertain", "dispatch_interrupted"
            else:
                job.status = "dead_letter" if job.attempts >= MAX_ATTEMPTS else "queued"
                job.last_error = "lease_expired"
            job.claim_token = None; job.lease_until = None
            evidence(db, job, "lease_recovered", status=job.status)
        job = await db.scalar(select(IntegrationJob).join(Connector, Connector.id == IntegrationJob.connector_id).where(
            IntegrationJob.status == "queued", IntegrationJob.available_at <= now, IntegrationJob.attempts < MAX_ATTEMPTS,
            or_(Connector.circuit_until.is_(None), Connector.circuit_until <= now),
        ).order_by(IntegrationJob.created_at).with_for_update(of=IntegrationJob, skip_locked=True).limit(1))
        if job is None:
            await db.commit(); return None
        job.status = "processing"; job.attempts += 1; job.claim_token = uuid.uuid4()
        job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        result = (job.id, job.claim_token)
        await db.commit()
        return result


async def locked_job(db, identity):
    job_id, token = identity
    job = await db.scalar(select(IntegrationJob).where(IntegrationJob.id == job_id).with_for_update().execution_options(populate_existing=True))
    if not job or job.claim_token != token or job.status not in ("processing", "dispatching") or job.lease_until <= datetime.now(UTC):
        return None
    return job


async def authorized_connector(db, job):
    await authorize(db, job.tenant_id, job.owner_id, write=True, lock=True)
    row = await db.scalar(select(Connector).where(Connector.id == job.connector_id, Connector.tenant_id == job.tenant_id,
        Connector.owner_id == job.owner_id).with_for_update().execution_options(populate_existing=True))
    if row is None or not row.enabled or not row.credentials: raise TransportError("connector_unavailable")
    if row.version != job.connector_version: raise TransportError("configuration_changed")
    return row


def check_approval(job):
    if job.operation not in READ_OPERATIONS and (not job.approved_at or job.approved_by != job.owner_id or not job.approval_expires_at or job.approval_expires_at <= datetime.now(UTC)):
        raise TransportError("approval_required")


async def execute(sessionmaker, cipher, identity):
    start = time.monotonic()
    error = None
    try:
        async with sessionmaker() as db:
            job = await locked_job(db, identity)
            if job is None: return
            row = await authorized_connector(db, job)
            check_approval(job)
            payload = unseal(cipher, "job", job, job.payload)
            if digest({"operation": job.operation, "payload": payload, "version": row.version}) != job.digest:
                raise TransportError("action_integrity_failed")
            adapters.validate_operation(row, job.operation, payload)
            secret = await usable_secret(db, cipher, row)
            if job.operation == "verify":
                result = await adapters.verify(row, secret)
                row.verified_at = datetime.now(UTC)
            elif job.operation == "sync":
                records, cursor, pages = await adapters.fetch_records(row, secret)
                inserted = changed = unchanged = 0
                for key, data in records.items():
                    previous = await db.scalar(select(IntegrationRecord).where(IntegrationRecord.connector_id == row.id, IntegrationRecord.external_id == key))
                    fingerprint = digest(data)
                    if previous and previous.digest == fingerprint and previous.schema_version == row.config["schema_version"]:
                        unchanged += 1; continue
                    if previous and row.config["conflict_policy"] == "reject_changes": raise TransportError("record_conflict")
                    if previous is None:
                        previous = IntegrationRecord(id=uuid.uuid4(), tenant_id=row.tenant_id, owner_id=row.owner_id,
                            connector_id=row.id, external_id=key, version=1)
                        db.add(previous); inserted += 1
                    else:
                        previous.version += 1; changed += 1
                    previous.payload = seal(cipher, "record", previous, data)
                    previous.digest = fingerprint; previous.schema_version = row.config["schema_version"]; previous.updated_at = datetime.now(UTC)
                row.cursor = cursor; row.last_sync_at = datetime.now(UTC)
                result = {"records_seen": len(records), "inserted": inserted, "changed": changed, "unchanged": unchanged, "pages": pages}
            else:
                # Save before submitting. A crash after this commit must never resend blindly.
                job.status = "dispatching"; evidence(db, job, "dispatch_started", digest=job.digest)
                await db.commit()
                job = await locked_job(db, identity)
                if job is None: return
                row = await authorized_connector(db, job); check_approval(job)
                from app.db.models import User
                from app.platform.access import principal_for
                from app.platform.approvals import ActionSpec
                from app.platform.side_effects import dispatch_once, module_scope_id
                actor = await db.get(User, job.owner_id)
                if actor is None or not actor.is_active:
                    raise TransportError("authorization_rejected")
                scope_id = await module_scope_id(db, "company_graph", job.tenant_id)
                if scope_id is None:
                    raise TransportError("authorization_rejected")
                held = {"result": None}

                async def dispatch():
                    held["result"] = await adapters.execute_write(row, secret, job, payload)
                    return str(job.id)

                async def verify(ref):
                    from app.platform.execution import VerifiedResult
                    return VerifiedResult(ok=held["result"] is not None or bool(ref), details=held["result"] or {"ref": ref})

                execution = await dispatch_once(
                    db, principal_for(actor, channel="worker"), f"connector:{job.id}",
                    ActionSpec("connector.write", scope_id, {"operation": job.operation, "digest": job.digest},
                               subject=f"connector:{job.connector_id}:{job.id}"),
                    duty="connector.write", dispatch=dispatch, verify=verify,
                )
                if execution.status == "failed":
                    raise TransportError("worker_error")
                result = held["result"] if held["result"] is not None else (execution.verified_result or {})
            job.result = result; job.status = "completed"; job.completed_at = datetime.now(UTC)
            job.duration_ms = int((time.monotonic() - start) * 1000); job.last_error = None
            job.claim_token = None; job.lease_until = None
            row.health = "healthy"; row.last_error = None; row.failures = 0; row.circuit_until = None
            evidence(db, job, "action_completed", operation=job.operation, result=result)
            await db.commit()
    except TransportError as exc:
        error = exc
    except AppError:
        error = TransportError("authorization_rejected")
    except Exception:
        # Conservatively reconcile a dispatch after an unknown exception, including commit failure.
        error = TransportError("worker_error")
    if error:
        await fail(sessionmaker, identity, error, int((time.monotonic() - start) * 1000))


async def fail(sessionmaker, identity, error, duration):
    async with sessionmaker() as db:
        job = await locked_job(db, identity)
        if job is None: return
        ambiguous = error.ambiguous or (job.status == "dispatching" and error.code == "worker_error")
        job.status = "uncertain" if ambiguous else ("queued" if error.retryable and job.attempts < MAX_ATTEMPTS else "dead_letter")
        job.available_at = datetime.now(UTC) + timedelta(seconds=max(error.retry_after, min(300, 2 ** job.attempts * 5)) + random.uniform(0, 3))
        job.last_error = error.code; job.duration_ms = duration; job.claim_token = None; job.lease_until = None
        row = await db.scalar(select(Connector).where(Connector.id == job.connector_id).with_for_update())
        row.failures += 1; row.last_error = error.code
        row.health = "credentials_required" if error.code in ("token_expired", "credentials_or_scope_rejected") else "degraded"
        if row.failures >= 3: row.circuit_until = datetime.now(UTC) + timedelta(seconds=max(60, error.retry_after))
        evidence(db, job, "action_failed", error=error.code, status=job.status, attempt=job.attempts)
        await db.commit()


async def run_worker(sessionmaker, cipher, stop):
    while not stop.is_set():
        try:
            await schedule(sessionmaker, cipher)
            identity = await claim(sessionmaker)
            if identity:
                await execute(sessionmaker, cipher, identity)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Integration worker poll failed; no payload logged")
        try: await asyncio.wait_for(stop.wait(), timeout=2)
        except asyncio.TimeoutError: pass
