"""Versioned Module 04 API. No workflow execution or policy mutation endpoints."""
import csv
import io
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy import delete, exists, or_, select

from app.api.deps import AdminUser, CurrentUser, DbSession, audit_context
from app.core.errors import FileTooLargeError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import Document, User
from app.rag.access import readable_by
from app.services import audit
from app.process_intelligence import access, ai, service
from app.process_intelligence.mining import simulate
from app.process_intelligence.models import Baseline, Event, Job, Membership, Model, Process, Source, SourceGrant, Tenant
from app.process_intelligence.schemas import AnalysisQuestion, BaselineInput, BatchInput, ExtractionInput, GrantInput, JobOut, MemberInput, ModelReviewInput, ProcessInput, RetentionInput, ReviewInput, SimulationInput, SourceInput, TenantInput


class BoundedRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def bounded(request: Request):
            if request.method in {"POST", "PUT", "PATCH"}:
                chunks, size = [], 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 4 * 1024 * 1024:
                        raise FileTooLargeError("Process Intelligence requests are limited to 4 MiB.")
                    chunks.append(chunk)
                request._body = b"".join(chunks)
            response = await handler(request)
            response.headers["Cache-Control"] = "no-store"
            return response
        return bounded


router = APIRouter(prefix="/api/process-intelligence/v1", tags=["process-intelligence"], route_class=BoundedRoute)


def source_out(s, user):
    return {"id": str(s.id), "name": s.name, "kind": s.kind, "document_id": str(s.document_id) if s.document_id else None,
            "retention_days": s.retention_days, "cursor": s.cursor, "synced_at": s.synced_at, "can_sync": s.owner_id == user.id}


def baseline_out(b):
    return {"id": str(b.id), "source_id": str(b.source_id), "status": b.status, "origin": b.origin, "data": b.data,
            "created_by": str(b.created_by), "reviewed_by": str(b.reviewed_by) if b.reviewed_by else None, "created_at": b.created_at}


@router.get("/organizations")
async def organizations(db: DbSession, user: CurrentUser):
    rows = (await db.execute(select(Tenant, Membership.role).join(Membership, Membership.tenant_id == Tenant.id).where(Membership.user_id == user.id).order_by(Tenant.created_at).limit(200))).all()
    return {"items": [{"id": str(t.id), "name": t.name, "role": role} for t, role in rows]}


@router.post("/organizations", status_code=201)
async def create_organization(payload: TenantInput, request: Request, db: DbSession, user: AdminUser):
    row = Tenant(name=payload.name, created_by=user.id)
    db.add(row)
    await db.flush()
    db.add(Membership(tenant_id=row.id, user_id=user.id, role="steward"))
    audit.record(db, audit_context(request, user), "process.organization_created", target=str(row.id))
    await db.commit()
    return {"id": str(row.id), "name": row.name, "role": "steward"}


@router.put("/{tenant_id}/members")
async def set_member(tenant_id: UUID, payload: MemberInput, request: Request, db: DbSession, user: CurrentUser):
    await access.membership(db, user, tenant_id, "steward")
    if user.role != "admin":
        raise PermissionDeniedError("Organization membership changes require a deployment administrator who is also a steward.")
    if payload.user_id == user.id:
        raise InvalidInputError("Self-service role changes are not allowed.")
    target = await db.get(User, payload.user_id)
    if target is None or not target.is_active:
        raise NotFoundError()
    member = await db.get(Membership, (tenant_id, payload.user_id))
    if member:
        member.role = payload.role
    else:
        db.add(Membership(tenant_id=tenant_id, user_id=payload.user_id, role=payload.role))
    audit.record(db, audit_context(request, user), "process.membership_changed", target=str(tenant_id), details={"user_id": str(payload.user_id), "role": payload.role})
    await db.commit()
    return {"user_id": str(payload.user_id), "role": payload.role}


@router.get("/{tenant_id}/processes")
async def processes(tenant_id: UUID, db: DbSession, user: CurrentUser, q: str = Query(default="", max_length=160)):
    await access.membership(db, user, tenant_id)
    visible = exists().where(Source.process_id == Process.id, *access.source_scope(user, tenant_id))
    rows = (await db.scalars(select(Process).where(Process.tenant_id == tenant_id, or_(Process.owner_id == user.id, visible), Process.name.icontains(q, autoescape=True)).order_by(Process.created_at).limit(200))).all()
    return {"items": [{"id": str(p.id), "name": p.name, "description": p.description, "revision": p.revision} for p in rows]}


@router.post("/{tenant_id}/processes", status_code=201)
async def create_process(tenant_id: UUID, payload: ProcessInput, request: Request, db: DbSession, user: CurrentUser):
    await access.membership(db, user, tenant_id, "analyst")
    row = Process(tenant_id=tenant_id, owner_id=user.id, **payload.model_dump())
    db.add(row)
    await db.flush()
    audit.record(db, audit_context(request, user), "process.created", target=str(row.id), details={"tenant_id": str(tenant_id)})
    await db.commit()
    return {"id": str(row.id), "name": row.name, "description": row.description, "revision": row.revision}


@router.get("/{tenant_id}/processes/{process_id}/sources")
async def list_sources(tenant_id: UUID, process_id: UUID, db: DbSession, user: CurrentUser):
    await access.process(db, user, tenant_id, process_id)
    return {"items": [source_out(s, user) for s in await access.sources(db, user, tenant_id, process_id)]}


@router.post("/{tenant_id}/processes/{process_id}/sources", status_code=201)
async def create_source(tenant_id: UUID, process_id: UUID, payload: SourceInput, request: Request, db: DbSession, user: CurrentUser):
    p = await access.process(db, user, tenant_id, process_id, write=True, lock=True)
    if p.owner_id != user.id:
        raise PermissionDeniedError("Only the process owner may attach sources.")
    if payload.document_id and not await db.scalar(select(Document.id).where(Document.id == payload.document_id, readable_by(user))):
        raise NotFoundError()
    row = Source(tenant_id=tenant_id, process_id=process_id, owner_id=user.id, **payload.model_dump())
    db.add(row)
    p.revision += 1
    await db.flush()
    audit.record(db, audit_context(request, user), "process.source_created", target=str(row.id), details={"tenant_id": str(tenant_id)})
    await db.commit()
    return source_out(row, user)


@router.put("/{tenant_id}/processes/{process_id}/sources/{source_id}/access")
async def source_access(tenant_id: UUID, process_id: UUID, source_id: UUID, payload: GrantInput, request: Request, db: DbSession, user: CurrentUser):
    await access.process(db, user, tenant_id, process_id, write=True, lock=True)
    await access.source(db, user, tenant_id, process_id, source_id, write=True)
    if await db.get(Membership, (tenant_id, payload.user_id)) is None:
        raise NotFoundError()
    grant = await db.get(SourceGrant, (tenant_id, source_id, payload.user_id))
    if payload.allowed and not grant:
        db.add(SourceGrant(tenant_id=tenant_id, source_id=source_id, user_id=payload.user_id))
    elif not payload.allowed and grant:
        await db.delete(grant)
    audit.record(db, audit_context(request, user), "process.source_access_changed", target=str(source_id), details={"tenant_id": str(tenant_id), "user_id": str(payload.user_id), "allowed": payload.allowed})
    await db.commit()
    return {"allowed": payload.allowed}


@router.post("/{tenant_id}/processes/{process_id}/sources/{source_id}/events")
async def ingest_events(tenant_id: UUID, process_id: UUID, source_id: UUID, payload: BatchInput, request: Request, db: DbSession, user: CurrentUser):
    return await service.ingest(db, user, tenant_id, process_id, source_id, payload, audit_context(request, user))


@router.post("/{tenant_id}/processes/{process_id}/analyze")
async def analyze(tenant_id: UUID, process_id: UUID, request: Request, db: DbSession, user: CurrentUser):
    model = await service.analyze(db, user, tenant_id, process_id, audit_context(request, user))
    await db.commit()
    return service.model_out(model)


@router.post("/{tenant_id}/processes/{process_id}/jobs", response_model=JobOut, status_code=202)
async def enqueue(tenant_id: UUID, process_id: UUID, db: DbSession, user: CurrentUser):
    job = await service.enqueue(db, user, tenant_id, process_id)
    await db.commit()
    return job


@router.get("/{tenant_id}/processes/{process_id}/jobs", response_model=list[JobOut])
async def list_jobs(tenant_id: UUID, process_id: UUID, db: DbSession, user: CurrentUser):
    await access.process(db, user, tenant_id, process_id)
    return list((await db.scalars(select(Job).where(Job.tenant_id == tenant_id, Job.process_id == process_id, Job.owner_id == user.id).order_by(Job.created_at.desc()).limit(25))).all())


@router.get("/{tenant_id}/processes/{process_id}/models")
async def models(tenant_id: UUID, process_id: UUID, db: DbSession, user: CurrentUser):
    await access.process(db, user, tenant_id, process_id)
    source_ids = [str(s.id) for s in await access.sources(db, user, tenant_id, process_id)]
    rows = (await db.scalars(select(Model).where(Model.tenant_id == tenant_id, Model.process_id == process_id, Model.source_ids.contained_by(source_ids)).order_by(Model.created_at.desc(), Model.id.desc()).limit(50))).all()
    return {"items": [service.model_out(m, summary=True) for m in rows]}


@router.get("/{tenant_id}/processes/{process_id}/models/{model_id}")
async def model(tenant_id: UUID, process_id: UUID, model_id: UUID, db: DbSession, user: CurrentUser):
    return service.model_out(await service.get_model(db, user, tenant_id, process_id, model_id))


@router.post("/{tenant_id}/processes/{process_id}/models/{model_id}/review")
async def review_model(tenant_id: UUID, process_id: UUID, model_id: UUID, payload: ModelReviewInput, request: Request, db: DbSession, user: CurrentUser):
    await access.membership(db, user, tenant_id, "steward")
    row = await service.get_model(db, user, tenant_id, process_id, model_id)
    row.review_status = payload.decision
    audit.record(db, audit_context(request, user), "process.model_reviewed", target=str(model_id), details={"tenant_id": str(tenant_id), "decision": payload.decision})
    await db.commit()
    return service.model_out(row)


@router.get("/{tenant_id}/processes/{process_id}/evidence/{event_id}")
async def evidence(tenant_id: UUID, process_id: UUID, event_id: UUID, db: DbSession, user: CurrentUser):
    await access.process(db, user, tenant_id, process_id)
    row = await db.scalar(select(Event).where(Event.tenant_id == tenant_id, Event.process_id == process_id, Event.id == event_id))
    if not row:
        raise NotFoundError()
    await access.source(db, user, tenant_id, process_id, row.source_id)
    return service.event_dict(row)


@router.get("/{tenant_id}/processes/{process_id}/baselines")
async def baselines(tenant_id: UUID, process_id: UUID, db: DbSession, user: CurrentUser):
    await access.process(db, user, tenant_id, process_id)
    source_ids = [s.id for s in await access.sources(db, user, tenant_id, process_id)]
    rows = (await db.scalars(select(Baseline).where(Baseline.tenant_id == tenant_id, Baseline.process_id == process_id, Baseline.source_id.in_(source_ids)).order_by(Baseline.created_at.desc()).limit(100))).all()
    return {"items": [baseline_out(b) for b in rows]}


@router.post("/{tenant_id}/processes/{process_id}/baselines", status_code=201)
async def propose_baseline(tenant_id: UUID, process_id: UUID, payload: BaselineInput, request: Request, db: DbSession, user: CurrentUser):
    row = await service.propose_baseline(db, user, tenant_id, process_id, payload, audit_context(request, user))
    await db.commit()
    return baseline_out(row)


@router.post("/{tenant_id}/processes/{process_id}/baselines/{baseline_id}/review")
async def review_baseline(tenant_id: UUID, process_id: UUID, baseline_id: UUID, payload: ReviewInput, request: Request, db: DbSession, user: CurrentUser):
    return baseline_out(await service.review_baseline(db, user, tenant_id, process_id, baseline_id, payload.decision, audit_context(request, user)))


@router.post("/{tenant_id}/processes/{process_id}/extract")
async def extract(tenant_id: UUID, process_id: UUID, payload: ExtractionInput, request: Request, db: DbSession, user: CurrentUser):
    return baseline_out(await ai.extract(db, user, tenant_id, process_id, payload.source_id, request.app.state.llm_runtime, audit_context(request, user)))


@router.post("/{tenant_id}/processes/{process_id}/ask")
async def ask(tenant_id: UUID, process_id: UUID, payload: AnalysisQuestion, request: Request, db: DbSession, user: CurrentUser):
    return await ai.answer(db, user, tenant_id, process_id, payload, request.app.state.llm_runtime, audit_context(request, user))


@router.post("/{tenant_id}/processes/{process_id}/simulate")
async def simulation(tenant_id: UUID, process_id: UUID, payload: SimulationInput, request: Request, db: DbSession, user: CurrentUser):
    row = await service.get_model(db, user, tenant_id, process_id, payload.model_id)
    if payload.actor not in {owner["name"] for owner in row.data["owners"]}:
        raise InvalidInputError("Choose an actor observed in this model.")
    result = simulate(row.data, payload.actor, payload.absence_hours, payload.replacement_capacity)
    audit.record(db, audit_context(request, user), "process.simulation", target=str(row.id), details={"tenant_id": str(tenant_id)})
    await db.commit()
    return result


@router.get("/{tenant_id}/processes/{process_id}/models/{model_id}/export")
async def export(tenant_id: UUID, process_id: UUID, model_id: UUID, request: Request, db: DbSession, user: CurrentUser, format: str = Query(default="json", pattern="^(json|csv)$")):
    row = await service.get_model(db, user, tenant_id, process_id, model_id)
    audit.record(db, audit_context(request, user), "process.model_exported", target=str(model_id), details={"tenant_id": str(tenant_id), "format": format})
    await db.commit()
    if format == "json":
        return service.model_out(row)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["source_id", "case_id", "sequence", "completed", "cycle_hours", "sla_breached"])
    def safe(value):
        value = str(value) if value is not None else ""
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value
    for case in row.data["cases"]:
        writer.writerow([safe(case["source_id"]), safe(case["case_id"]), safe(" â†’ ".join(case["sequence"])), case["completed"], case["cycle_hours"], case["sla_breached"]])
    return Response(buffer.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="process-{model_id}.csv"', "Cache-Control": "no-store"})


@router.post("/{tenant_id}/processes/{process_id}/sources/{source_id}/retention")
async def retention(tenant_id: UUID, process_id: UUID, source_id: UUID, payload: RetentionInput, request: Request, db: DbSession, user: CurrentUser):
    if payload.confirm_source_id != source_id:
        raise InvalidInputError("Confirm the exact source whose expired cases should be removed.")
    return await service.purge_expired(db, user, tenant_id, process_id, source_id, audit_context(request, user))
