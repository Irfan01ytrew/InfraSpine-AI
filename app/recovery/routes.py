"""Authenticated Module 07 API. Uses InfraSpine sessions, MFA and CSRF protection."""
import uuid

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, audit_context
from app.core.errors import ConflictError
from app.recovery import evidence, service
from app.recovery.access import get_scoped, scope_for, scoped
from app.recovery.models import Incident, Member, Playbook, Target
from app.recovery.policy import TERMINAL, now
from app.recovery.schemas import DecisionInput, EventInput, MemberInput, NoteInput, PlaybookInput, ProposalInput, TargetInput, TenantInput, ToggleInput, TransitionInput
from app.services import audit

router = APIRouter(prefix="/api/recovery", tags=["Exception & Recovery"])


@router.get("/workspaces")
async def workspaces(db: DbSession, user: CurrentUser):
    return {"workspaces": await service.list_tenants(db, user)}


@router.post("/workspaces", status_code=201)
async def create_workspace(body: TenantInput, request: Request, db: DbSession, user: CurrentUser):
    result = await service.create_tenant(db, user, body.name, audit_context(request, user))
    await db.commit()
    return result


@router.get("/{tenant_id}/configuration")
async def configuration(tenant_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser):
    scope = await scope_for(db, tenant_id, user.id)
    books = [service.public(p) for p in await db.scalars(select(Playbook).where(*scoped(Playbook, scope)).order_by(Playbook.created_at.desc()))]
    members = [service.public(m) for m in await db.scalars(select(Member).where(Member.tenant_id == tenant_id))] if scope.role == "owner" else []
    return {"role": scope.role, "user_id": str(user.id), "adapters": request.app.state.recovery_registry.catalog(), "playbooks": books, "members": members}


@router.put("/{tenant_id}/members")
async def set_member(tenant_id: uuid.UUID, body: MemberInput, request: Request, db: DbSession, user: CurrentUser):
    await service.member_set(db, await scope_for(db, tenant_id, user.id), body, audit_context(request, user))
    await db.commit()
    return {"updated": True}


@router.post("/{tenant_id}/targets", status_code=201)
async def create_target(tenant_id: uuid.UUID, body: TargetInput, request: Request, db: DbSession, user: CurrentUser):
    result = await service.create_target(db, await scope_for(db, tenant_id, user.id), body, request.app.state.recovery_registry, audit_context(request, user))
    await db.commit()
    return result


@router.post("/{tenant_id}/playbooks", status_code=201)
async def create_playbook(tenant_id: uuid.UUID, body: PlaybookInput, request: Request, db: DbSession, user: CurrentUser):
    result = await service.create_playbook(db, await scope_for(db, tenant_id, user.id), body, request.app.state.recovery_registry, audit_context(request, user))
    await db.commit()
    return result


@router.patch("/{tenant_id}/playbooks/{resource_id}")
async def toggle_playbook(tenant_id: uuid.UUID, resource_id: uuid.UUID, body: ToggleInput, request: Request, db: DbSession, user: CurrentUser):
    scope = await scope_for(db, tenant_id, user.id)
    scope.require_owner()
    row = await get_scoped(db, Playbook, resource_id, scope, lock=True)
    row.enabled = body.enabled
    audit.record(db, audit_context(request, user), "recovery.playbook_toggled", target=str(row.id), details={"enabled": row.enabled})
    await db.commit()
    return service.public(row)


@router.patch("/{tenant_id}/targets/{resource_id}")
async def toggle_target(tenant_id: uuid.UUID, resource_id: uuid.UUID, body: ToggleInput, request: Request, db: DbSession, user: CurrentUser):
    scope = await scope_for(db, tenant_id, user.id)
    scope.require_owner()
    row = await get_scoped(db, Target, resource_id, scope, lock=True)
    row.enabled = body.enabled
    audit.record(db, audit_context(request, user), "recovery.target_toggled", target=str(row.id), details={"enabled": row.enabled})
    await db.commit()
    return service.public(row)


@router.post("/{tenant_id}/events", status_code=202)
async def event(tenant_id: uuid.UUID, body: EventInput, request: Request, db: DbSession, user: CurrentUser):
    result = await service.ingest(db, await scope_for(db, tenant_id, user.id), body, request.app.state.cipher, audit_context(request, user))
    await db.commit()
    return result


@router.get("/{tenant_id}/dashboard")
async def dashboard(tenant_id: uuid.UUID, db: DbSession, user: CurrentUser, search: str = Query(default="", max_length=120),
                    status: str | None = Query(default=None, max_length=24), severity: str | None = Query(default=None, max_length=12), offset: int = Query(default=0, ge=0)):
    return await service.dashboard(db, await scope_for(db, tenant_id, user.id), search=search, status=status, severity=severity, offset=offset)


@router.get("/{tenant_id}/incidents/{incident_id}")
async def incident_detail(tenant_id: uuid.UUID, incident_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser):
    return await service.detail(db, await scope_for(db, tenant_id, user.id), incident_id, request.app.state.cipher)


@router.post("/{tenant_id}/incidents/{incident_id}/transition")
async def transition(tenant_id: uuid.UUID, incident_id: uuid.UUID, body: TransitionInput, request: Request, db: DbSession, user: CurrentUser):
    result = await service.transition(db, await scope_for(db, tenant_id, user.id), incident_id, body, request.app.state.cipher, audit_context(request, user))
    await db.commit()
    return result


@router.post("/{tenant_id}/incidents/{incident_id}/diagnose")
async def diagnose(tenant_id: uuid.UUID, incident_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser):
    result = await service.diagnose(db, await scope_for(db, tenant_id, user.id), incident_id,
        request.app.state.recovery_registry, request.app.state.cipher, audit_context(request, user))
    await db.commit()
    return result


@router.post("/{tenant_id}/incidents/{incident_id}/recoveries", status_code=202)
async def propose(tenant_id: uuid.UUID, incident_id: uuid.UUID, body: ProposalInput, request: Request, db: DbSession, user: CurrentUser):
    result = await service.propose(db, await scope_for(db, tenant_id, user.id), incident_id, body,
        request.app.state.recovery_registry, request.app.state.cipher, audit_context(request, user))
    await db.commit()
    return result


@router.post("/{tenant_id}/commands/{command_id}/decision")
async def decide(tenant_id: uuid.UUID, command_id: uuid.UUID, body: DecisionInput, request: Request, db: DbSession, user: CurrentUser):
    result = await service.decide(db, await scope_for(db, tenant_id, user.id), command_id, body, request.app.state.cipher, audit_context(request, user))
    await db.commit()
    return result


@router.post("/{tenant_id}/incidents/{incident_id}/notes")
async def note(tenant_id: uuid.UUID, incident_id: uuid.UUID, body: NoteInput, request: Request, db: DbSession, user: CurrentUser):
    result = await service.add_note(db, await scope_for(db, tenant_id, user.id), incident_id, body, request.app.state.cipher, audit_context(request, user))
    await db.commit()
    return result


@router.post("/{tenant_id}/incidents/{incident_id}/archive")
async def archive(tenant_id: uuid.UUID, incident_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser):
    scope = await scope_for(db, tenant_id, user.id)
    scope.require_owner()
    row = await get_scoped(db, Incident, incident_id, scope, lock=True)
    if row.status not in TERMINAL or row.resolved_at is None or (now() - row.resolved_at).days < 30:
        raise ConflictError("Only incidents closed for at least 30 days can be archived.")
    row.archived = True
    await evidence.append(db, request.app.state.cipher, audit_context(request, user), row, "archived", {"retention": "evidence retained"})
    await db.commit()
    return service.public(row)


@router.get("/{tenant_id}/incidents/{incident_id}/report")
async def report(tenant_id: uuid.UUID, incident_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser):
    result = await service.detail(db, await scope_for(db, tenant_id, user.id), incident_id, request.app.state.cipher)
    result["report"] = {"generated_at": now().isoformat(), "type": "evidence_backed_incident_report",
        "root_cause_confirmed": result["incident"]["diagnosis"].get("confirmed", False),
        "preventive_actions": [e for e in result["timeline"] if e["kind"] == "preventive_action"],
        "postmortem": [e for e in result["timeline"] if e["kind"] == "postmortem"]}
    return JSONResponse(result, headers={"Content-Disposition": f'attachment; filename="incident-{incident_id}.json"', "Cache-Control": "no-store"})
