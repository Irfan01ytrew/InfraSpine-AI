"""Stable v1 graph contracts shared by Company Map, Knowledge, Agents and Workflows."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, audit_context
from app.company_graph import discovery, intelligence, service
from app.company_graph.access import get_record, scope_for
from app.company_graph.models import GraphMember, GraphSource
from app.company_graph.schemas import Decision, ExtractionInput, GraphQuestion, MemberInput, RecordProposal, Simulation, SourceInput, SyncInput, TenantInput
from app.core.errors import InvalidInputError
from app.db.models import User
from app.services import audit

router = APIRouter(prefix="/api/company-graph", tags=["company-graph"])


@router.get("/tenants")
async def tenants(db: DbSession, user: CurrentUser):
    return {"items": await service.list_tenants(db, user)}


@router.post("/tenants", status_code=201)
async def create_tenant(payload: TenantInput, request: Request, db: DbSession, user: CurrentUser):
    result = await service.create_tenant(db, user, payload.name, audit_context(request, user))
    await db.commit()
    return result


@router.get("/{tenant_id}/catalog")
async def catalog(tenant_id: UUID, db: DbSession, user: CurrentUser):
    scope = await scope_for(db, tenant_id, user)
    return {"contract_version": 1, "entity_types": service.ENTITY_TYPES, "relationship_types": service.RELATION_TYPES,
            "custom_types_allowed": True, "role": scope.role, "approval_expiry_hours": 24}


@router.get("/{tenant_id}/map")
async def graph_map(tenant_id: UUID, response: Response, db: DbSession, user: CurrentUser,
    search: Annotated[str, Query(max_length=240)] = "", kind: Annotated[str, Query(max_length=64)] = "",
    department_id: UUID | None = None, after: UUID | None = None, limit: Annotated[int, Query(ge=1, le=1000)] = 500):
    scope = await scope_for(db, tenant_id, user)
    result = await service.projection(db, scope, search=search, kind=kind, department_id=department_id, after=after, limit=limit)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["ETag"] = '"' + result["fingerprint"] + '"'
    return result


@router.get("/{tenant_id}/records/{record_id}")
async def record(tenant_id: UUID, record_id: UUID, db: DbSession, user: CurrentUser):
    return service.public_record(await get_record(db, await scope_for(db, tenant_id, user), record_id))


@router.get("/{tenant_id}/neighbors/{record_id}")
async def neighbors(tenant_id: UUID, record_id: UUID, db: DbSession, user: CurrentUser, depth: Annotated[int, Query(ge=1, le=6)] = 1):
    return await service.neighborhood(db, await scope_for(db, tenant_id, user), record_id, depth)


@router.post("/{tenant_id}/changes", status_code=201)
async def propose(tenant_id: UUID, payload: RecordProposal, request: Request, db: DbSession, user: CurrentUser):
    row = await service.propose_record(db, await scope_for(db, tenant_id, user), payload, audit_context(request, user))
    result = service.public_change(row)
    await db.commit()
    return result


@router.get("/{tenant_id}/changes")
async def changes(tenant_id: UUID, db: DbSession, user: CurrentUser, after: UUID | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 50):
    return await service.list_changes(db, await scope_for(db, tenant_id, user), after=after, limit=limit)


@router.post("/{tenant_id}/changes/{change_id}/decision")
async def decision(tenant_id: UUID, change_id: UUID, payload: Decision, request: Request, db: DbSession, user: CurrentUser):
    result = await service.decide(db, await scope_for(db, tenant_id, user), change_id, payload.approve, payload.reason, audit_context(request, user))
    await db.commit()
    return result


@router.get("/{tenant_id}/members")
async def members(tenant_id: UUID, db: DbSession, user: CurrentUser):
    scope = await scope_for(db, tenant_id, user)
    scope.require("owner")
    rows = (await db.execute(select(GraphMember, User.email).join(User).where(GraphMember.tenant_id == tenant_id))).all()
    return {"items": [{"user_id": str(m.user_id), "email": email, "role": m.role, "departments": m.departments,
                       "all_departments": m.all_departments, "active": m.active, "version": m.version} for m, email in rows]}


@router.post("/{tenant_id}/members/changes", status_code=201)
async def propose_member(tenant_id: UUID, payload: MemberInput, request: Request, db: DbSession, user: CurrentUser):
    row = await service.propose_member(db, await scope_for(db, tenant_id, user), payload, audit_context(request, user))
    result = service.public_change(row)
    await db.commit()
    return result


@router.get("/{tenant_id}/events")
async def events(tenant_id: UUID, db: DbSession, user: CurrentUser, after: UUID | None = None, subject_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100):
    return await service.events(db, await scope_for(db, tenant_id, user), after=after, subject_id=subject_id, limit=limit)


@router.post("/{tenant_id}/simulate")
async def simulate(tenant_id: UUID, payload: Simulation, request: Request, db: DbSession, user: CurrentUser):
    scope = await scope_for(db, tenant_id, user)
    moment = payload.as_of or datetime.now(UTC)
    # Traverse each root so a large tenant's first page cannot drop a requested root.
    nodes, edges, truncated = {}, {}, False
    for root in payload.failed_ids:
        graph = await service.neighborhood(db, scope, root, payload.depth, moment)
        nodes.update({n["id"]: n for n in graph["entities"]})
        edges.update({e["id"]: e for e in graph["relationships"]})
        truncated |= graph["truncated"]
        if len(nodes) > 2000 or len(edges) > 10000:
            raise InvalidInputError("Simulation budget exceeded. Use fewer starting entities or a smaller depth.")
    result = intelligence.impact({"entities": list(nodes.values()), "relationships": list(edges.values()), "truncated": truncated, "as_of": moment.isoformat()},
                                 [str(v) for v in payload.failed_ids], payload.depth, payload.scenario)
    audit.record(db, audit_context(request, user), "company_graph.simulated", target=str(tenant_id), details={"scenario": payload.scenario, "roots": len(payload.failed_ids)})
    await db.commit()
    return result


@router.get("/{tenant_id}/quality")
async def quality(tenant_id: UUID, db: DbSession, user: CurrentUser, stale_days: Annotated[int, Query(ge=1, le=3650)] = 90):
    graph = await service.projection(db, await scope_for(db, tenant_id, user), limit=1000)
    return intelligence.quality(graph, stale_days=stale_days)


@router.post("/{tenant_id}/query")
async def graph_query(tenant_id: UUID, payload: GraphQuestion, db: DbSession, user: CurrentUser):
    return intelligence.answer_query(await service.projection(db, await scope_for(db, tenant_id, user), limit=1000), payload.question)


@router.get("/{tenant_id}/export")
async def export(tenant_id: UUID, request: Request, response: Response, db: DbSession, user: CurrentUser, after: UUID | None = None):
    graph = await service.projection(db, await scope_for(db, tenant_id, user), limit=1000, after=after)
    response.headers["Cache-Control"] = "private, no-store"
    audit.record(db, audit_context(request, user), "company_graph.exported", target=str(tenant_id))
    await db.commit()
    return {"contract_version": 1, **graph}


@router.get("/{tenant_id}/consumers/{consumer}")
async def consumer_events(tenant_id: UUID, consumer: Literal["knowledge", "agents", "workflow", "governance", "continuity"],
    db: DbSession, user: CurrentUser, after: UUID | None = None):
    return {"consumer": consumer, **await service.events(db, await scope_for(db, tenant_id, user), after=after)}


@router.get("/{tenant_id}/sources")
async def sources(tenant_id: UUID, db: DbSession, user: CurrentUser):
    scope = await scope_for(db, tenant_id, user)
    scope.require("owner")
    rows = (await db.scalars(select(GraphSource).where(GraphSource.tenant_id == tenant_id).order_by(GraphSource.name))).all()
    return {"items": [discovery.source_public(s) for s in rows]}


@router.post("/{tenant_id}/sources", status_code=201)
async def create_source(tenant_id: UUID, payload: SourceInput, request: Request, db: DbSession, user: CurrentUser):
    scope = await scope_for(db, tenant_id, user)
    scope.require("owner")
    await service.lock_tenant(db, scope)
    row = GraphSource(tenant_id=tenant_id, created_by=user.id, **payload.model_dump())
    db.add(row)
    await db.flush()
    audit.record(db, audit_context(request, user), "company_graph.source_created", target=str(row.id))
    result = discovery.source_public(row)
    await db.commit()
    return result


@router.post("/{tenant_id}/sources/{source_id}/sync")
async def synchronize(tenant_id: UUID, source_id: UUID, payload: SyncInput, request: Request, db: DbSession, user: CurrentUser):
    result = await discovery.synchronize(db, await scope_for(db, tenant_id, user), source_id, payload, audit_context(request, user))
    await db.commit()
    return result


@router.post("/{tenant_id}/sources/{source_id}/extract")
async def extract(tenant_id: UUID, source_id: UUID, payload: ExtractionInput, request: Request, db: DbSession, user: CurrentUser):
    scope = await scope_for(db, tenant_id, user)
    await discovery.source_for(db, scope, source_id)
    client = await request.app.state.llm_runtime.get_client(db)
    result = await discovery.extract_candidates(client, payload.text)
    audit.record(db, audit_context(request, user), "company_graph.extracted", target=str(source_id), details={"candidates": len(result)})
    await db.commit()
    return {"items": result, "reference": payload.reference, "approval_required": True, "saved": False}
