"""Transactional incident coordination. Route layer commits successful operations."""
import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import func, select, text

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import User
from app.recovery import evidence
from app.recovery.access import get_scoped, scope_for, scoped
from app.recovery.adapters import Context
from app.recovery.models import Command, Event, Incident, Member, Playbook, Target, Tenant
from app.recovery.policy import ACTIVE_COMMANDS, RISK, TERMINAL, TRANSITIONS, action_digest, bounded_evidence, check_version, digest, now, redact
from app.services import audit


def public(row):
    omitted = {"ciphertext", "nonce", "key_id"}
    return {c.name: (str(v) if isinstance(v, uuid.UUID) else v.isoformat() if hasattr(v, "isoformat") else v)
            for c in row.__table__.columns if c.name not in omitted for v in [getattr(row, c.name)]}


async def create_tenant(db, user, name, ctx):
    if user.role != "admin":
        raise PermissionDeniedError("Only an administrator can provision a recovery workspace.")
    row = Tenant(name=redact(name))
    db.add(row)
    await db.flush()
    db.add(Member(tenant_id=row.id, user_id=user.id, role="owner"))
    audit.record(db, ctx, "recovery.workspace_created", target=str(row.id))
    return public(row)


async def list_tenants(db, user):
    rows = (await db.execute(select(Tenant, Member.role).join(Member).where(Member.user_id == user.id, Member.active.is_(True)).order_by(Tenant.name))).all()
    return [{**public(t), "role": role} for t, role in rows]


async def member_set(db, scope, data, ctx):
    scope.require_owner()
    if data.user_id == scope.user_id:
        raise ConflictError("Self role changes are prohibited.")
    user = await db.get(User, data.user_id)
    if user is None or not user.is_active:
        raise NotFoundError("Active user not found.")
    row = await db.get(Member, (scope.tenant_id, data.user_id))
    if row is None:
        row = Member(tenant_id=scope.tenant_id, **data.model_dump())
        db.add(row)
    else:
        row.role, row.active = data.role, data.active
    audit.record(db, ctx, "recovery.membership_changed", target=str(scope.tenant_id), details={"user": str(data.user_id), "role": data.role, "active": data.active})


async def create_target(db, scope, data, registry, ctx):
    scope.require_owner()
    await scope_for(db, scope.tenant_id, data.owner_id)
    adapter = registry.get(data.adapter)
    await asyncio.wait_for(adapter.authorize(Context(scope.tenant_id, data.owner_id, data.resource)), timeout=10)
    row = Target(tenant_id=scope.tenant_id, **data.model_dump())
    row.name = redact(row.name)
    db.add(row)
    await db.flush()
    audit.record(db, ctx, "recovery.target_created", target=str(row.id), details={"tenant": str(scope.tenant_id)})
    return public(row)


async def create_playbook(db, scope, data, registry, ctx):
    scope.require_owner()
    adapter = registry.get(data.adapter)
    if data.action not in adapter.actions:
        raise InvalidInputError("This action is not implemented by the owning module.")
    risk = RISK[data.action]
    if data.preapproved and (risk != "low" or not adapter.idempotent):
        raise InvalidInputError("Only low-risk, idempotent owner actions can be preapproved.")
    if not adapter.idempotent and data.max_attempts != 1:
        raise InvalidInputError("Non-idempotent adapters require exactly one execution attempt.")
    row = Playbook(tenant_id=scope.tenant_id, created_by=scope.user_id, risk=risk, **data.model_dump())
    row.name = redact(row.name)
    db.add(row)
    await db.flush()
    audit.record(db, ctx, "recovery.playbook_created", target=str(row.id), details={"risk": risk, "preapproved": row.preapproved})
    return public(row)


async def ingest(db, scope, data, cipher, ctx):
    scope.require_write()
    target = await get_scoped(db, Target, data.target_id, scope, lock=True)
    if not target.enabled:
        raise ConflictError("Target is disabled.")
    stamp = now()
    if data.occurred_at > stamp + timedelta(minutes=5) or data.occurred_at < stamp - timedelta(days=30):
        raise InvalidInputError("Event time must be within the last 30 days and no more than five minutes ahead.")
    clean = bounded_evidence(data.evidence)
    existing = await db.scalar(select(Event).where(Event.tenant_id == scope.tenant_id, Event.target_id == target.id, Event.event_key == data.event_key))
    if existing:
        return {"event_id": str(existing.id), "incident_id": str(existing.incident_id) if existing.incident_id else None, "disposition": existing.disposition, "duplicate": True}
    fp = digest({"source": data.source, "category": data.category, "code": data.error_code})
    row = Event(tenant_id=scope.tenant_id, target_id=target.id, event_key=data.event_key, fingerprint=fp,
                disposition="suppressed" if data.condition == "expected_rejection" else "pending_threshold", occurred_at=data.occurred_at, received_at=stamp)
    db.add(row)
    await db.flush()
    if data.condition == "expected_rejection":
        return {"event_id": str(row.id), "incident_id": None, "disposition": row.disposition, "duplicate": False}
    count = await db.scalar(select(func.count()).select_from(Event).where(Event.tenant_id == scope.tenant_id,
        Event.target_id == target.id, Event.fingerprint == fp, Event.disposition != "suppressed", Event.received_at >= stamp - timedelta(seconds=target.window_seconds)))
    threshold = max(target.threshold, 2 if data.condition == "transient" else 1)
    if count < threshold:
        return {"event_id": str(row.id), "incident_id": None, "disposition": row.disposition, "duplicate": False}
    incident = await db.scalar(select(Incident).where(Incident.tenant_id == scope.tenant_id,
        Incident.target_id == target.id, Incident.fingerprint == fp).with_for_update().execution_options(populate_existing=True))
    kind = "detected"
    if incident:
        incident.occurrences += 1
        incident.last_seen = stamp
        if incident.status in TERMINAL:
            incident.status, kind = "reopened", "reopened"
            incident.generation += 1
            incident.resolved_at = incident.verified_at = None
            incident.sla_due = stamp + timedelta(seconds=target.sla_seconds)
            incident.opened_at, incident.archived = stamp, False
        else:
            kind = "correlated"
        # A new failure during verification invalidates prior successful samples.
        if incident.status == "verifying":
            commands = list(await db.scalars(select(Command).where(Command.incident_id == incident.id, Command.status == "verifying").with_for_update()))
            for cmd in commands:
                cmd.successes = 0
    else:
        incident = Incident(tenant_id=scope.tenant_id, target_id=target.id, owner_id=target.owner_id,
            fingerprint=fp, title=redact(data.title), source=data.source, category=data.category, severity=data.severity,
            correlation_key=data.correlation_key, opened_at=stamp, detected_at=stamp, last_seen=stamp,
            sla_due=stamp + timedelta(seconds=target.sla_seconds))
        db.add(incident)
        await db.flush()
    row.incident_id, row.disposition = incident.id, "incident"
    await evidence.append(db, cipher, ctx, incident, kind, {"event_id": str(row.id), "code": data.error_code,
        "condition": data.condition, "source": data.source, "evidence": clean, "occurred_at": data.occurred_at.isoformat(),
        "detection_latency_seconds": max(0, (stamp - data.occurred_at).total_seconds()), "window_count": count})
    return {"event_id": str(row.id), "incident_id": str(incident.id), "disposition": "incident", "duplicate": False}


async def detail(db, scope, incident_id, cipher):
    # A row lock ensures timeline/head consistency across concurrent appends.
    incident = await get_scoped(db, Incident, incident_id, scope, lock=True)
    timeline = await evidence.read(db, cipher, incident)
    commands = list(await db.scalars(select(Command).where(Command.tenant_id == scope.tenant_id, Command.incident_id == incident.id).order_by(Command.created_at)))
    related = []
    if incident.correlation_key:
        related = [public(i) for i in await db.scalars(select(Incident).where(*scoped(Incident, scope),
            Incident.owner_id == incident.owner_id, Incident.correlation_key == incident.correlation_key,
            Incident.id != incident.id).order_by(Incident.last_seen.desc()).limit(50))]
    return {"incident": public(incident), "timeline": timeline, "commands": [public(c) for c in commands], "related": related, "audit_valid": True}


async def transition(db, scope, incident_id, data, cipher, ctx):
    scope.require_write()
    incident = await get_scoped(db, Incident, incident_id, scope, lock=True)
    check_version(incident, data.version)
    if data.state not in TRANSITIONS.get(incident.status, set()):
        raise ConflictError("This incident state does not allow that transition.")
    if data.state in TERMINAL:
        scope.require_owner()
        if data.disposition is None:
            raise InvalidInputError("Closure requires verified recovery or an explicit authorized disposition.")
    if data.assignee_id is not None:
        assignee = await scope_for(db, scope.tenant_id, data.assignee_id)
        if assignee.role == "observer" or (assignee.role != "owner" and data.assignee_id != incident.owner_id):
            raise InvalidInputError("Assignee must be a responder with access to this incident.")
        incident.assignee_id = data.assignee_id
    active = list(await db.scalars(select(Command).where(Command.incident_id == incident.id, Command.status.in_(ACTIVE_COMMANDS)).with_for_update()))
    if any(c.status in {"executing", "verifying"} for c in active):
        raise ConflictError("A recovery is in flight. Wait for its bounded verification before changing state.")
    for cmd in active:
        cmd.status, cmd.finished_at = "cancelled", now()
    old = incident.status
    incident.status = data.state
    if data.state in TERMINAL:
        incident.resolved_at = now()
    await evidence.append(db, cipher, ctx, incident, "transition", {"from": old, "to": data.state,
        "reason": data.reason, "disposition": data.disposition, "assignee": str(incident.assignee_id)})
    return public(incident)


async def diagnose(db, scope, incident_id, registry, cipher, ctx):
    scope.require_write()
    incident = await get_scoped(db, Incident, incident_id, scope, lock=True)
    if incident.status in TERMINAL or incident.status in {"recovering", "verifying", "awaiting_approval", "recovery_proposed"}:
        raise ConflictError("Diagnosis is unavailable in this incident state.")
    target = await get_scoped(db, Target, incident.target_id, scope)
    adapter = registry.get(target.adapter)
    context = Context(scope.tenant_id, target.owner_id, target.resource)
    await asyncio.wait_for(adapter.authorize(context), 10)
    try:
        observation = await asyncio.wait_for(adapter.observe(context), 10)
        impact = await asyncio.wait_for(adapter.impact(context), 10)
        observed = {"healthy": observation.healthy, "code": observation.code, "evidence": observation.evidence}
    except (TimeoutError, OSError):
        observed, impact = {"healthy": None, "code": "diagnostic_unavailable"}, {"status": "unknown"}
    fixes = list(await db.scalars(select(Command).join(Incident, Command.incident_id == Incident.id).where(
        Command.tenant_id == scope.tenant_id, Incident.owner_id == incident.owner_id, Incident.target_id == target.id,
        Command.status == "succeeded").order_by(Command.finished_at.desc()).limit(5)))
    incident.diagnosis = bounded_evidence({"confirmed": False, "hypothesis": f"{incident.source} {incident.category} requires causal investigation",
        "confidence": 0.4 if observed["healthy"] is False else 0.2, "basis": "event classification and current owner-adapter observations",
        "observation": observed, "impact": impact, "known_fixes": [{"playbook_id": str(c.playbook_id), "command_id": str(c.id)} for c in fixes],
        "generated_at": now().isoformat(), "ai_generated": False})
    incident.status = "investigating"
    await evidence.append(db, cipher, ctx, incident, "diagnosis", incident.diagnosis)
    return public(incident)


async def propose(db, scope, incident_id, data, registry, cipher, ctx):
    scope.require_write()
    incident = await get_scoped(db, Incident, incident_id, scope, lock=True)
    existing = await db.scalar(select(Command).where(Command.tenant_id == scope.tenant_id, Command.incident_id == incident.id, Command.request_key == data.request_key))
    if existing:
        if existing.playbook_id != data.playbook_id:
            raise ConflictError("Idempotency key is bound to another playbook.")
        return public(existing)
    check_version(incident, data.version)
    if incident.status not in {"investigating", "failed", "escalated"}:
        raise ConflictError("Investigate this incident before proposing recovery.")
    if incident.recovery_count >= 10:
        raise ConflictError("Lifetime incident recovery budget exhausted. Human remediation is required.")
    active = await db.scalar(select(Command.id).where(Command.incident_id == incident.id, Command.status.in_(ACTIVE_COMMANDS)).limit(1))
    if active:
        raise ConflictError("This incident already has an active recovery.")
    target = await get_scoped(db, Target, incident.target_id, scope)
    playbook = await get_scoped(db, Playbook, data.playbook_id, scope)
    adapter = registry.get(target.adapter)
    if not target.enabled or not playbook.enabled or playbook.adapter != target.adapter or playbook.action not in adapter.actions:
        raise InvalidInputError("Enabled playbook and target adapter must support the same action.")
    cmd = Command(tenant_id=scope.tenant_id, incident_id=incident.id, playbook_id=playbook.id, target_id=target.id,
        generation=incident.generation, requester_id=scope.user_id, request_key=data.request_key,
        status="proposed", action_digest="")
    cmd.action_digest = action_digest(cmd, target, playbook)
    db.add(cmd)
    await db.flush()
    incident.recovery_count += 1
    incident.status = "recovery_proposed"
    await evidence.append(db, cipher, ctx, incident, "recovery_proposed", {"command_id": str(cmd.id), "digest": cmd.action_digest, "action": playbook.action, "risk": playbook.risk})
    # Preapproval is a persisted tenant-owner policy for explicitly low-risk actions.
    if playbook.preapproved and playbook.risk == "low" and adapter.idempotent:
        cmd.status = "queued"
        incident.status = "recovering"
    else:
        cmd.status = "awaiting_approval"
        cmd.approval_expires = now() + timedelta(hours=1)
        incident.status = "awaiting_approval"
    await evidence.append(db, cipher, ctx, incident, "authorization_requested" if cmd.status == "awaiting_approval" else "policy_authorized",
        {"command_id": str(cmd.id), "digest": cmd.action_digest})
    return public(cmd)


async def decide(db, scope, command_id, data, cipher, ctx):
    scope.require_owner()
    # Lock incident before command everywhere to avoid inversion with ingestion/worker.
    cmd = await get_scoped(db, Command, command_id, scope)
    incident = await get_scoped(db, Incident, cmd.incident_id, scope, lock=True)
    cmd = await get_scoped(db, Command, command_id, scope, lock=True)
    if cmd.status != "awaiting_approval" or incident.status != "awaiting_approval" or cmd.generation != incident.generation:
        raise ConflictError("This request is no longer awaiting approval.")
    if cmd.requester_id == scope.user_id:
        raise PermissionDeniedError("A different tenant owner must approve sensitive recovery.")
    if data.digest != cmd.action_digest or cmd.approval_expires <= now():
        raise ConflictError("Approval expired or does not match the exact action.")
    cmd.approver_id = scope.user_id
    cmd.approved_digest = data.digest if data.approve else None
    cmd.status = "queued" if data.approve else "rejected"
    incident.status = "recovering" if data.approve else "escalated"
    if not data.approve:
        cmd.finished_at = now()
    await evidence.append(db, cipher, ctx, incident, "approval_decided", {"command_id": str(cmd.id), "approved": data.approve, "digest": data.digest, "reason": data.reason})
    return public(cmd)


async def add_note(db, scope, incident_id, data, cipher, ctx):
    scope.require_write()
    incident = await get_scoped(db, Incident, incident_id, scope, lock=True)
    await evidence.append(db, cipher, ctx, incident, data.kind, {"note": data.note, "confirmed": False, "source": "human_annotation"})
    return public(incident)


async def dashboard(db, scope, *, search="", status=None, severity=None, offset=0):
    conditions = scoped(Incident, scope) + [Incident.archived.is_(False)]
    if search:
        conditions.append(Incident.title.ilike("%" + search.replace("%", "\\%").replace("_", "\\_") + "%"))
    if status:
        conditions.append(Incident.status == status)
    if severity:
        conditions.append(Incident.severity == severity)
    items = list(await db.scalars(select(Incident).where(*conditions).order_by(Incident.last_seen.desc()).offset(offset).limit(50)))
    total = await db.scalar(select(func.count()).select_from(Incident).where(*conditions))
    base = scoped(Incident, scope)
    counts = dict((await db.execute(select(Incident.status, func.count()).where(*base, Incident.archived.is_(False)).group_by(Incident.status))).all())
    mttr = await db.scalar(select(func.avg(func.extract("epoch", Incident.resolved_at - Incident.opened_at))).where(*base,
        Incident.status == "resolved", Incident.verified_at.is_not(None)))
    recurring = await db.scalar(select(func.count()).select_from(Incident).where(*base, Incident.generation > 1))
    overdue = await db.scalar(select(func.count()).select_from(Incident).where(*base, Incident.status.not_in(TERMINAL), Incident.sla_due < now()))
    commands = (await db.execute(select(Command.status, func.count()).join(Incident, Command.incident_id == Incident.id).where(*base).group_by(Command.status))).all()
    command_counts = dict(commands)
    finished = command_counts.get("succeeded", 0) + command_counts.get("dead_letter", 0)
    targets = [public(t) for t in await db.scalars(select(Target).where(*scoped(Target, scope)).order_by(Target.name))]
    return {"incidents": [public(i) for i in items], "total": total, "offset": offset, "page_size": 50, "targets": targets,
        "metrics": {"active": sum(v for k, v in counts.items() if k not in TERMINAL), "by_state": counts,
            "overdue": overdue, "recurring": recurring, "mttr_seconds": float(mttr) if mttr is not None else None,
            "recovery_success_percent": round(100 * command_counts.get("succeeded", 0) / finished, 1) if finished else None,
            "commands": command_counts}, "as_of": now().isoformat()}
