"""Durable PostgreSQL queue; every execution rechecks the submitting user's permissions."""
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select

from app.db.models import User
from app.services.audit import AuditContext
from .models import Job
from .service import analyze

log = logging.getLogger(__name__)


async def claim_job(sessionmaker):
    async with sessionmaker() as db:
        now = datetime.now(UTC)
        row = await db.scalar(select(Job).where(or_(Job.status == "queued", (Job.status == "processing") & (Job.lease_until < now))).order_by(Job.created_at, Job.id).with_for_update(skip_locked=True).limit(1))
        if row is None:
            return None
        if row.attempts >= 3:
            row.status, row.error_code = "failed", "attempt_limit"
            row.claim, row.lease_until = None, None
            await db.commit()
            return None
        row.status, row.claim = "processing", uuid4()
        row.attempts += 1
        row.lease_until = now + timedelta(minutes=5)
        await db.commit()
        return row.id, row.claim


async def execute_claim(sessionmaker, job_id, claim):
    try:
        async with sessionmaker() as db:
            row = await db.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if not row or row.status != "processing" or row.claim != claim or row.lease_until <= datetime.now(UTC):
                return False
            user = await db.get(User, row.owner_id)
            if user is None or not user.is_active:
                row.status, row.error_code = "failed", "access_revoked"
            else:
                ctx = AuditContext(user.id, None, None, f"pi-job-{row.id}")
                model = await analyze(db, user, row.tenant_id, row.process_id, ctx)
                if row.lease_until <= datetime.now(UTC):
                    await db.rollback()
                    return False
                row.status, row.model_id, row.error_code = "completed", model.id, None
            row.claim, row.lease_until = None, None
            await db.commit()
            return True
    except Exception as exc:
        # Never persist exception text: DB parameters and provider messages can contain source data.
        async with sessionmaker() as db:
            row = await db.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if row and row.status == "processing" and row.claim == claim and row.lease_until > datetime.now(UTC):
                denied = getattr(exc, "status_code", None) in (403, 404)
                row.status = "failed" if row.attempts >= 3 or denied else "queued"
                row.error_code = "access_revoked" if denied else "analysis_failed"
                row.claim, row.lease_until = None, None
                await db.commit()
        log.warning("Process analysis failed (%s)", type(exc).__name__)
        return False


async def run_once(sessionmaker):
    claim = await claim_job(sessionmaker)
    return await execute_claim(sessionmaker, *claim) if claim else False


async def run_loop(sessionmaker, stop):
    while not stop.is_set():
        try:
            await run_once(sessionmaker)
        except Exception as exc:
            log.warning("Process queue unavailable (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            pass
