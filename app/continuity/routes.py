"""Authenticated Module 08 API. All mutations commit their journal atomically."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, DbSession, audit_context
from app.continuity import dashboard as dashboard_service
from app.continuity import drills, graph_merge, handover, intelligence, journal, schemas, service
from app.continuity.access import nodes_for, scope_for
from app.continuity.models import GrantRequest, Member, Tenant
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.db.models import AutomationWorkflow
from app.recovery.access import scope_for as recovery_scope_for
from app.services import audit

router = APIRouter(prefix="/api/continuity", tags=["business-continuity"])


async def get_scope(tenant_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser):
    return await scope_for(db, tenant_id, user, write=request.method != "GET")


ScopeDep = Annotated[object, Depends(get_scope)]


async def save(db, operation):
    try:
        result = await operation
        await db.commit()
        return result
    except IntegrityError:
        await db.rollback()
        raise ConflictError("A conflicting record or reference exists. Reload and retry with a unique key.") from None


@router.get("/tenants")
async def tenants(db: DbSession, user: CurrentUser):
    return {"items": await service.list_tenants(db, user)}


@router.post("/tenants", status_code=201)
async def create_tenant(data: schemas.TenantInput, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.create_tenant(db, user, data, audit_context(request, user)))


@router.get("/access-requests")
async def access_requests(db: DbSession, user: CurrentUser):
    if user.role != "admin":
        raise PermissionDeniedError()
    rows = await db.scalars(select(GrantRequest).where(GrantRequest.status == "pending").order_by(GrantRequest.expires_at).limit(100))
    return {"items": [service.public(row) for row in rows]}


@router.post("/access-requests/{grant_id}/decision")
async def decide_grant(grant_id: uuid.UUID, data: schemas.Decision, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.decide_grant(db, user, grant_id, data, audit_context(request, user)))


@router.get("/tenants/{tenant_id}/dashboard")
async def dashboard(scope: ScopeDep, db: DbSession, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    return await service.dashboard(db, scope)


@router.get("/scopes/{scope_id}/dashboard")
async def continuity_dashboard(scope_id: uuid.UUID, db: DbSession, user: CurrentUser, response: Response):
    """Step AJ business continuity dashboard (read-only).

    Non-members and unknown scopes get 404 (the scope is not
    revealed); members whose role may not open the dashboard get 403.
    Recovery incidents come from the viewer's recovery workspace for
    the same tenant id when one exists; otherwise that section is
    empty — recovery workspaces are a separate tenant namespace.
    """
    response.headers["Cache-Control"] = "private, no-store"
    scope = await scope_for(db, scope_id, user, write=False)
    if scope.role not in dashboard_service.DASHBOARD_ROLES:
        raise PermissionDeniedError("The continuity dashboard is available to owner, approver and planner roles.")
    recovery_scope = None
    try:
        recovery_scope = await recovery_scope_for(db, scope_id, user.id)
    except NotFoundError:
        recovery_scope = None
    return await dashboard_service.continuity_dashboard_summary(db, scope, recovery_scope=recovery_scope)


@router.get("/tenants/{tenant_id}/members")
async def members(scope: ScopeDep, db: DbSession):
    scope.require("owner")
    return {"items": [service.public(row) for row in await db.scalars(select(Member).where(Member.tenant_id == scope.tenant_id))]}


@router.post("/tenants/{tenant_id}/members", status_code=202)
async def member_request(data: schemas.MemberInput, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.propose_grant(db, scope, data, audit_context(request, user)))


@router.post("/tenants/{tenant_id}/nodes", status_code=201)
async def node(data: schemas.NodeInput, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.create_node(db, scope, data, audit_context(request, user)))


@router.post("/tenants/{tenant_id}/links", status_code=201)
async def link(data: schemas.LinkInput, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.create_link(db, scope, data, audit_context(request, user)))


@router.post("/tenants/{tenant_id}/bia", status_code=201)
async def bia(data: schemas.BIAInput, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.create_bia(db, scope, data, audit_context(request, user)))


@router.post("/tenants/{tenant_id}/scenarios", status_code=201)
async def scenario(data: schemas.ScenarioInput, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.create_scenario(db, scope, data, audit_context(request, user)))


@router.get("/tenants/{tenant_id}/scenarios/{scenario_id}/handover")
async def handover_draft_endpoint(scenario_id: uuid.UUID, scope: ScopeDep, db: DbSession):
    scenario = await service.scenario_for(db, scope, scenario_id)
    return intelligence.handover_draft(scenario, await nodes_for(db, scope))


@router.get("/tenants/{tenant_id}/scenarios/{scenario_id}/handover-check")
async def handover_check(scenario_id: uuid.UUID, scope: ScopeDep, db: DbSession):
    return await handover.check_handover(db, scope, scenario_id)


@router.get("/tenants/{tenant_id}/handover-grants")
async def handover_grants(scope: ScopeDep, db: DbSession):
    return {"items": await handover.list_handover_grants(db, scope)}


@router.post("/tenants/{tenant_id}/handover-grants", status_code=201)
async def propose_handover_grant(data: schemas.HandoverGrantInput, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, handover.propose_handover_grant(db, scope, data, audit_context(request, user)))


@router.post("/tenants/{tenant_id}/handover-grants/{grant_id}/decision")
async def decide_handover_grant(grant_id: uuid.UUID, data: schemas.Decision, scope: ScopeDep, request: Request,
                                db: DbSession, user: CurrentUser):
    return await save(db, handover.decide_handover_grant(db, scope, grant_id, data, audit_context(request, user), user))


@router.post("/tenants/{tenant_id}/handover-grants/{grant_id}/revoke")
async def revoke_handover_grant(grant_id: uuid.UUID, data: schemas.Reason, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, handover.revoke_handover_grant(db, scope, grant_id, data, audit_context(request, user)))


@router.post("/tenants/{tenant_id}/drills/seed")
async def seed_drills(scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, drills.seed_drills(db, scope, audit_context(request, user)))


@router.get("/tenants/{tenant_id}/runs/{run_id}/drill-comparison")
async def drill_comparison(run_id: uuid.UUID, scope: ScopeDep, db: DbSession):
    return await drills.drill_comparison(db, scope, run_id)


@router.post("/tenants/{tenant_id}/graph-merge/seed")
async def seed_graph_merge(data: schemas.GraphMergeSeedInput, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, graph_merge.seed_graph_merge(db, scope, data.company_graph_tenant_id, user, audit_context(request, user)))


@router.get("/tenants/{tenant_id}/graph-merge/status")
async def graph_merge_status(company_graph_tenant_id: uuid.UUID, scope: ScopeDep, db: DbSession, user: CurrentUser):
    return await graph_merge.merge_status(db, scope, company_graph_tenant_id, user)


@router.get("/tenants/{tenant_id}/workflows")
async def workflows(scope: ScopeDep, db: DbSession, user: CurrentUser):
    from app.services.automation import list_workflows
    nodes = await nodes_for(db, scope)
    groups = {n.access_group_id for n in nodes if n.kind == "department" and n.access_group_id}
    employees = {str(n.user_id) for n in nodes if n.kind == "employee" and n.user_id}
    rows = await list_workflows(db, user)
    return {"items": [{"id": str(w.id), "name": w.name, "version": w.version, "department_group_id": str(w.department_group_id),
                       "assignee_user_id": w.action_config.get("assignee_user_id")}
                      for w in rows if w.enabled and w.action_type == "create_task" and w.department_group_id in groups and w.action_config.get("assignee_user_id") in employees]}


@router.post("/tenants/{tenant_id}/plans", status_code=201)
async def plan(data: schemas.PlanInput, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.create_plan(db, scope, data, audit_context(request, user)))


@router.post("/tenants/{tenant_id}/plans/{plan_id}/decision")
async def decide_plan(plan_id: uuid.UUID, data: schemas.Decision, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.decide_plan(db, scope, plan_id, data, audit_context(request, user)))


@router.post("/tenants/{tenant_id}/runs", status_code=202)
async def activate(data: schemas.Activation, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.activate(db, scope, data, audit_context(request, user)))


@router.post("/tenants/{tenant_id}/runs/{run_id}/decision")
async def decide_run(run_id: uuid.UUID, data: schemas.Decision, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.decide_activation(db, scope, run_id, data, audit_context(request, user),
                                                    cipher=request.app.state.cipher))


@router.get("/tenants/{tenant_id}/runs/{run_id}")
async def report(run_id: uuid.UUID, scope: ScopeDep, db: DbSession, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    return await service.report(db, scope, run_id)


@router.post("/tenants/{tenant_id}/tasks/{task_id}/dispatch")
async def dispatch(task_id: uuid.UUID, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.dispatch(db, scope, task_id, audit_context(request, user)))


@router.post("/tenants/{tenant_id}/tasks/{task_id}/evidence")
async def evidence(task_id: uuid.UUID, data: schemas.TaskEvidence, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.submit_evidence(db, scope, task_id, data, audit_context(request, user)))


@router.post("/tenants/{tenant_id}/tasks/{task_id}/verify")
async def verify(task_id: uuid.UUID, data: schemas.Reason, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.verify_task(db, scope, task_id, data, audit_context(request, user)))


@router.post("/tenants/{tenant_id}/runs/{run_id}/suspend")
async def suspend(run_id: uuid.UUID, data: schemas.Reason, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.transition(db, scope, run_id, "suspend", data, audit_context(request, user)))


@router.post("/tenants/{tenant_id}/runs/{run_id}/resume")
async def resume(run_id: uuid.UUID, data: schemas.Reason, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.transition(db, scope, run_id, "resume", data, audit_context(request, user)))


@router.post("/tenants/{tenant_id}/runs/{run_id}/close")
async def close(run_id: uuid.UUID, data: schemas.Review, scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    return await save(db, service.close_run(db, scope, run_id, data, audit_context(request, user)))


@router.get("/tenants/{tenant_id}/audit/verify")
async def verify_audit(scope: ScopeDep, db: DbSession):
    scope.require("owner", "approver")
    if not scope.all_departments:
        raise PermissionDeniedError("Workspace-wide audit verification requires all-department scope.")
    return await journal.verify(db, scope.tenant_id)


@router.get("/tenants/{tenant_id}/export")
async def export(scope: ScopeDep, request: Request, db: DbSession, user: CurrentUser):
    # Export re-evaluates exactly the same current scope as the UI; JSON prevents spreadsheet formula execution.
    result = await service.dashboard(db, scope)
    audit.record(db, audit_context(request, user), "continuity.export", target=str(scope.tenant_id))
    await db.commit()
    return JSONResponse(result, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                                        "Content-Disposition": 'attachment; filename="continuity-' + str(scope.tenant_id) + '.json"'})
