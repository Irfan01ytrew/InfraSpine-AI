"""Independent recovery worker: leased claims, fencing, bounded retries and probes."""
import asyncio
import logging
import random
import uuid
from datetime import timedelta

from sqlalchemy import or_, select

from app.core.errors import AppError
from app.recovery import evidence, service
from app.recovery.access import scope_for
from app.recovery.adapters import Context, Observation
from app.recovery.models import Command, Incident, Playbook, Target
from app.recovery.policy import TERMINAL, action_digest, now
from app.recovery.schemas import EventInput
from app.services.audit import AuditContext

logger = logging.getLogger(__name__)
SYSTEM = AuditContext(None, None, None, "recovery-worker")


async def locked_bundle(db, command_id, *, skip=False):
    identity = (await db.execute(select(Command.target_id, Command.incident_id).where(Command.id == command_id))).first()
    if identity is None:
        return None
    target = await db.scalar(select(Target).where(Target.id == identity.target_id).with_for_update(skip_locked=skip).execution_options(populate_existing=True))
    if target is None:
        return None
    incident = await db.scalar(select(Incident).where(Incident.id == identity.incident_id, Incident.tenant_id == target.tenant_id,
        Incident.target_id == target.id, Incident.owner_id == target.owner_id).with_for_update(skip_locked=skip).execution_options(populate_existing=True))
    if incident is None:
        return None
    cmd = await db.scalar(select(Command).where(Command.id == command_id, Command.tenant_id == target.tenant_id,
        Command.incident_id == incident.id).with_for_update(skip_locked=skip).execution_options(populate_existing=True))
    if cmd is None:
        return None
    playbook = await db.scalar(select(Playbook).where(Playbook.id == cmd.playbook_id, Playbook.tenant_id == target.tenant_id))
    return target, incident, cmd, playbook


async def authorization(db, target, incident, cmd, playbook, registry):
    if not playbook or not target.enabled or not playbook.enabled or incident.generation != cmd.generation or incident.status in TERMINAL:
        return False
    if playbook.adapter != target.adapter or action_digest(cmd, target, playbook) != cmd.action_digest:
        return False
    try:
        requester = await scope_for(db, cmd.tenant_id, cmd.requester_id)
        requester.require_write()
        owner = await scope_for(db, cmd.tenant_id, target.owner_id)
        if requester.role != "owner" and requester.user_id != target.owner_id:
            return False
        adapter = registry.get(target.adapter)
        if playbook.action not in adapter.actions:
            return False
        if playbook.preapproved and playbook.risk == "low" and adapter.idempotent:
            return True
        approver = await scope_for(db, cmd.tenant_id, cmd.approver_id)
        return (approver.role == "owner" and cmd.approver_id != cmd.requester_id
                and cmd.approval_expires is not None and cmd.approval_expires > now()
                and cmd.approved_digest == cmd.action_digest)
    except AppError:
        return False


async def terminal(db, cipher, incident, cmd, code):
    cmd.status, cmd.last_error, cmd.finished_at = "dead_letter", code, now()
    cmd.lease_token = cmd.lease_until = None
    incident.status = "escalated"
    await evidence.append(db, cipher, SYSTEM, incident, "escalated", {"command_id": str(cmd.id), "code": code, "attempts": cmd.attempts})


async def claim(sessionmaker, registry, cipher):
    stamp = now()
    async with sessionmaker() as db:
        candidates = list(await db.scalars(select(Command.id).where(Command.status.in_({"queued", "executing", "verifying"}),
            Command.available_at <= stamp, or_(Command.lease_until.is_(None), Command.lease_until < stamp)).order_by(Command.available_at).limit(50)))
        for command_id in candidates:
            bundle = await locked_bundle(db, command_id, skip=True)
            if bundle is None:
                continue
            target, incident, cmd, playbook = bundle
            if cmd.status not in {"queued", "executing", "verifying"} or cmd.available_at > stamp or (cmd.lease_until and cmd.lease_until >= stamp):
                continue
            if not await authorization(db, *bundle, registry):
                await terminal(db, cipher, incident, cmd, "authorization_invalid")
                await db.commit()
                return {"handled": True}
            other = await db.scalar(select(Command.id).where(Command.target_id == target.id, Command.id != cmd.id,
                Command.status.in_({"executing", "verifying"})).limit(1))
            if other:
                continue
            phase = "verify" if cmd.status in {"executing", "verifying"} else "execute"
            if phase == "execute" and target.circuit_until and target.circuit_until > stamp:
                cmd.available_at = target.circuit_until
                continue
            if phase == "execute" and cmd.attempts >= playbook.max_attempts:
                await terminal(db, cipher, incident, cmd, "retry_budget_exhausted")
                await db.commit()
                return {"handled": True}
            if phase == "execute":
                cmd.attempts += 1
                cmd.status, incident.status = "executing", "recovering"
            else:
                cmd.status, incident.status = "verifying", "verifying"
            cmd.lease_token = uuid.uuid4()
            cmd.lease_until = stamp + timedelta(seconds=playbook.timeout_seconds + 15)
            await evidence.append(db, cipher, SYSTEM, incident, "execution_started" if phase == "execute" else "verification_started",
                {"command_id": str(cmd.id), "attempt": cmd.attempts, "phase": phase})
            result = {"handled": False, "id": cmd.id, "token": cmd.lease_token, "phase": phase,
                "adapter": target.adapter, "context": Context(cmd.tenant_id, target.owner_id, target.resource, cmd.id, cmd.generation),
                "action": playbook.action, "timeout": playbook.timeout_seconds, "last_seen": incident.last_seen}
            await db.commit()
            return result
        await db.commit()
    return None


async def finish(sessionmaker, registry, cipher, claimed, result, error):
    async with sessionmaker() as db:
        bundle = await locked_bundle(db, claimed["id"])
        if bundle is None:
            return False
        target, incident, cmd, playbook = bundle
        stamp = now()
        if (cmd.lease_token != claimed["token"] or cmd.lease_until is None or cmd.lease_until <= stamp
                or cmd.generation != incident.generation or cmd.status not in {"executing", "verifying"}):
            return False  # superseded worker must never record a result
        cmd.lease_token = cmd.lease_until = None
        if not await authorization(db, *bundle, registry):
            await terminal(db, cipher, incident, cmd, "authorization_revoked")
            await db.commit()
            return True
        cmd.last_error = error
        if claimed["phase"] == "execute":
            # Includes ambiguous failure/timeout: always observe before another dispatch.
            cmd.status, incident.status = "verifying", "verifying"
            cmd.checks = cmd.successes = 0
            cmd.available_at = stamp + timedelta(seconds=playbook.backoff_seconds)
            await evidence.append(db, cipher, SYSTEM, incident, "execution_returned", {"command_id": str(cmd.id), "error": error, "result": result or {}})
        else:
            cmd.checks += 1
            healthy = isinstance(result, Observation) and result.healthy and error is None and incident.last_seen <= claimed["last_seen"]
            cmd.successes = cmd.successes + 1 if healthy else 0
            await evidence.append(db, cipher, SYSTEM, incident, "health_observed", {"command_id": str(cmd.id), "healthy": healthy,
                "code": result.code if isinstance(result, Observation) else error, "evidence": result.evidence if isinstance(result, Observation) else {}, "samples": cmd.successes})
            if cmd.successes >= playbook.verify_samples:
                cmd.status, cmd.finished_at = "succeeded", stamp
                incident.status, incident.verified_at, incident.resolved_at = "resolved", stamp, stamp
                target.failures, target.circuit_until = 0, None
                target.healthy, target.last_checked = True, stamp
                await evidence.append(db, cipher, SYSTEM, incident, "resolved", {"command_id": str(cmd.id), "health_samples": cmd.successes, "verified": True})
            elif cmd.checks >= playbook.verify_samples + 2:
                target.failures += 1
                if target.failures >= 3:
                    target.circuit_until = stamp + timedelta(seconds=300)
                adapter = registry.get(target.adapter)
                if cmd.attempts < playbook.max_attempts and adapter.idempotent:
                    cmd.status, cmd.checks, cmd.successes = "queued", 0, 0
                    delay = min(3600, playbook.backoff_seconds * 2 ** max(0, cmd.attempts - 1))
                    cmd.available_at = stamp + timedelta(seconds=delay + random.uniform(0, delay * .25))
                    await evidence.append(db, cipher, SYSTEM, incident, "retry_scheduled", {"command_id": str(cmd.id), "available_at": cmd.available_at.isoformat()})
                else:
                    await terminal(db, cipher, incident, cmd, "verification_failed")
            else:
                cmd.available_at = stamp + timedelta(seconds=playbook.backoff_seconds)
        await db.commit()
        return True


async def _execute_once(sessionmaker, claimed, dispatch):
    """Dispatch a recovery command through the platform ledger so a retry never re-runs a completed action."""
    from app.db.models import User
    from app.platform.access import principal_for
    from app.platform.approvals import ActionSpec
    from app.platform.execution import VerifiedResult
    from app.platform.side_effects import dispatch_once, module_scope_id

    held = {}

    async def _dispatch():
        held["result"] = await asyncio.wait_for(dispatch(), timeout=claimed["timeout"])
        return str(claimed["id"])

    async def _verify(ref):
        return VerifiedResult(ok=True, details={"external_ref": ref, "replayed": "result" not in held})

    async with sessionmaker() as db:
        owner = await db.get(User, claimed["context"].owner_id)
        if owner is None or not owner.is_active:
            raise RuntimeError("authorization_rejected")
        scope_id = await module_scope_id(db, "recovery", claimed["context"].tenant_id)
        if scope_id is None:
            raise RuntimeError("authorization_rejected")
        execution = await dispatch_once(
            db, principal_for(owner, channel="worker"), f"recovery:{claimed['id']}",
            ActionSpec("recovery.command", scope_id, {"command_id": str(claimed["id"]), "action": claimed["action"]},
                       subject=f"recovery:{claimed['id']}"),
            duty="recovery.access", dispatch=_dispatch, verify=_verify,
        )
        if execution.replayed and "result" not in held:
            return execution.verified_result
        if execution.status == "failed" and "result" not in held:
            raise RuntimeError("adapter_failed")
        return held.get("result")


async def process_once(sessionmaker, registry, cipher):
    claimed = await claim(sessionmaker, registry, cipher)
    if claimed is None:
        return False
    if claimed["handled"]:
        return True
    result, error = None, None
    try:
        adapter = registry.get(claimed["adapter"])
        async def dispatch():
            await adapter.authorize(claimed["context"])
            if claimed["phase"] == "verify":
                return await adapter.observe(claimed["context"])
            return await adapter.execute(claimed["context"], claimed["action"])
        if claimed["phase"] == "execute":
            result = await _execute_once(sessionmaker, claimed, dispatch)
        else:
            result = await asyncio.wait_for(dispatch(), timeout=claimed["timeout"])
    except TimeoutError:
        error = "adapter_timeout"
    except Exception:
        error = "adapter_failed"  # Never log exception strings from remote integrations.
    await finish(sessionmaker, registry, cipher, claimed, result, error)
    return True


async def monitor_once(sessionmaker, registry, cipher):
    stamp = now()
    async with sessionmaker() as db:
        target = await db.scalar(select(Target).where(Target.enabled.is_(True), Target.next_check <= stamp).order_by(Target.next_check).with_for_update(skip_locked=True).limit(1))
        if target is None:
            return False
        target.next_check = stamp + timedelta(seconds=target.check_interval)
        target_id, tenant_id, owner_id, adapter_id, resource = target.id, target.tenant_id, target.owner_id, target.adapter, target.resource
        await db.commit()
    observation = None
    try:
        async with sessionmaker() as db:
            actor_scope = await scope_for(db, tenant_id, owner_id)
            actor_scope.require_write()
        adapter = registry.get(adapter_id)
        context = Context(tenant_id, owner_id, resource)
        await asyncio.wait_for(adapter.authorize(context), 10)
        observation = await asyncio.wait_for(adapter.observe(context), 10)
    except Exception:
        observation = Observation(False, "monitor_unavailable")
    async with sessionmaker() as db:
        target = await db.scalar(select(Target).where(Target.id == target_id, Target.tenant_id == tenant_id).with_for_update())
        if not target.enabled or (target.last_checked and target.last_checked > stamp):
            return True
        try:
            actor_scope = await scope_for(db, tenant_id, owner_id)
            actor_scope.require_write()
        except AppError:
            return True
        target.healthy, target.last_checked = observation.healthy, stamp
        if not observation.healthy:
            await service.ingest(db, actor_scope, EventInput(target_id=target.id, event_key=f"health:{stamp.isoformat()}",
                source="workflow" if adapter_id == "automation" else "infrastructure", category="unavailable", error_code="health_check_failed",
                title=f"{target.name} health check failed", occurred_at=stamp, condition="transient", evidence={"code": observation.code}), cipher, SYSTEM)
        await db.commit()
    return True


async def escalate_due(sessionmaker, cipher):
    async with sessionmaker() as db:
        incident = await db.scalar(select(Incident).where(Incident.status.in_({"detected", "triaged", "investigating", "awaiting_approval", "reopened"}),
            Incident.sla_due < now()).order_by(Incident.sla_due).with_for_update(skip_locked=True).limit(1))
        if incident is None:
            return False
        for cmd in await db.scalars(select(Command).where(Command.incident_id == incident.id, Command.status == "awaiting_approval").with_for_update()):
            cmd.status, cmd.last_error, cmd.finished_at = "expired", "approval_sla_missed", now()
        incident.status = "escalated"
        await evidence.append(db, cipher, SYSTEM, incident, "sla_escalated", {"due": incident.sla_due.isoformat(), "reason": "response or approval SLA missed"})
        await db.commit()
    return True


async def run(sessionmaker, registry, cipher, stop, interval=2):
    while not stop.is_set():
        try:
            await process_once(sessionmaker, registry, cipher)
            await monitor_once(sessionmaker, registry, cipher)
            await escalate_due(sessionmaker, cipher)
        except Exception:
            logger.warning("recovery_worker_cycle_failed", extra={"event_code": "recovery_cycle_failed"})
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except TimeoutError:
            pass
