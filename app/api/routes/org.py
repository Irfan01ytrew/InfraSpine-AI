"""Positions (job titles), reporting lines and the organization chart.

Administrators manage positions and who holds them; every signed-in person can read the chart (names,
positions, managers and departments only). None of this changes what anyone may access.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.deps import AdminUser, CurrentUser, DbSession, audit_context, require_admin
from app.schemas.org import (
    BudgetListResponse,
    BudgetOut,
    CreateDelegationRequest,
    DelegationListResponse,
    DelegationOut,
    CreateBudgetRequest,
    UpdateBudgetRequest,
    AddApprovalChainStepRequest,
    ApprovalChainResponse,
    ApprovalChainStepOut,
    MoveApprovalChainStepRequest,
    UpdateApprovalChainStepRequest,
    ApprovalLimitListResponse,
    ApprovalLimitOut,
    CreateApprovalLimitRequest,
    UpdateApprovalLimitRequest,
    ApplyLibraryRequest,
    ApplyLibraryResponse,
    CreatePositionRequest,
    LibraryPositionOut,
    OrgAssignmentRequest,
    OrgChartNode,
    OrgChartResponse,
    OrgHistoryEntry,
    OrgHistoryResponse,
    PositionLibraryResponse,
    PositionListResponse,
    PositionOut,
    UpdatePositionRequest,
)
from app.services.approval_authority import ApprovalLimitService
from app.services.approval_chains import ApprovalChainService
from app.services.budgets import BudgetService
from app.services.approval_delegation import DelegationService
from app.services.org_structure import OrgStructureService, org_chart
from app.services.position_library import POSITION_LIBRARY
from app.platform.timezone import org_today

router = APIRouter(prefix="/api/admin", tags=["organization"], dependencies=[Depends(require_admin)])
chart_router = APIRouter(prefix="/api/org", tags=["organization"])


def _service(db: DbSession, admin: AdminUser, request: Request) -> OrgStructureService:
    return OrgStructureService(db, admin, audit_context(request, admin))


Service = Annotated[OrgStructureService, Depends(_service)]


def _out(position, holders: int = 0) -> PositionOut:
    return PositionOut(
        id=position.id, title=position.title, job_family=position.job_family, level=position.level,
        description=position.description, is_active=position.is_active, holders=holders,
        created_at=position.created_at, updated_at=position.updated_at,
    )


@router.get("/positions", response_model=PositionListResponse)
async def list_positions(service: Service) -> PositionListResponse:
    return PositionListResponse(positions=[_out(p, n) for p, n in await service.list_positions()])


@router.post("/positions", response_model=PositionOut, status_code=201)
async def create_position(payload: CreatePositionRequest, service: Service) -> PositionOut:
    position = await service.create_position(payload.title, payload.job_family, payload.level, payload.description)
    return _out(position)


@router.patch("/positions/{position_id}", response_model=PositionOut)
async def update_position(position_id: uuid.UUID, payload: UpdatePositionRequest, service: Service) -> PositionOut:
    position = await service.update_position(
        position_id, title=payload.title, job_family=payload.job_family, level=payload.level,
        description=payload.description, is_active=payload.is_active, clear_level=payload.clear_level,
    )
    holders = next((n for p, n in await service.list_positions() if p.id == position.id), 0)
    return _out(position, holders)


@router.get("/positions/library", response_model=PositionLibraryResponse)
async def position_library(service: Service) -> PositionLibraryResponse:
    existing = {p.title.lower() for p, _ in await service.list_positions(include_archived=False)}
    return PositionLibraryResponse(positions=[
        LibraryPositionOut(title=p.title, job_family=p.job_family, level=p.level, description=p.description, added=p.title.lower() in existing)
        for p in POSITION_LIBRARY
    ])


@router.post("/positions/library/apply", response_model=ApplyLibraryResponse)
async def apply_position_library(payload: ApplyLibraryRequest, service: Service) -> ApplyLibraryResponse:
    created, skipped = await service.apply_library(payload.titles)
    return ApplyLibraryResponse(created=created, skipped=skipped)


@router.put("/users/{user_id}/org", response_model=OrgChartNode)
async def assign_position(user_id: uuid.UUID, payload: OrgAssignmentRequest, service: Service, db: DbSession) -> OrgChartNode:
    await service.assign(user_id, payload.position_id, payload.reports_to_id)
    node = next(n for n in await org_chart(db) if n.user_id == user_id)  # assign() accepts active accounts only
    return OrgChartNode(**node.__dict__)


@router.get("/users/{user_id}/org-history", response_model=OrgHistoryResponse)
async def org_history(user_id: uuid.UUID, service: Service) -> OrgHistoryResponse:
    return OrgHistoryResponse(entries=[
        OrgHistoryEntry(position_title=h.position_title, reports_to_email=h.reports_to_email, started_at=h.started_at, ended_at=h.ended_at)
        for h in await service.history(user_id)
    ])


@chart_router.get("/chart", response_model=OrgChartResponse)
async def chart(db: DbSession, user: CurrentUser) -> OrgChartResponse:
    return OrgChartResponse(people=[OrgChartNode(**node.__dict__) for node in await org_chart(db)])


def _limits(db: DbSession, admin: AdminUser, request: Request) -> ApprovalLimitService:
    return ApprovalLimitService(db, admin, audit_context(request, admin))


Limits = Annotated[ApprovalLimitService, Depends(_limits)]


def _limit_out(limit, title: str | None, email: str | None) -> ApprovalLimitOut:
    return ApprovalLimitOut(
        id=limit.id, category=limit.category, position_id=limit.position_id, position_title=title,
        user_id=limit.user_id, user_email=email, max_amount=limit.max_amount, currency=limit.currency,
    )


@router.get("/approval-limits", response_model=ApprovalLimitListResponse)
async def list_approval_limits(service: Limits) -> ApprovalLimitListResponse:
    return ApprovalLimitListResponse(limits=[_limit_out(*row) for row in await service.list()])


@router.post("/approval-limits", response_model=ApprovalLimitOut, status_code=201)
async def create_approval_limit(payload: CreateApprovalLimitRequest, service: Limits) -> ApprovalLimitOut:
    limit = await service.create(
        category=payload.category, position_id=payload.position_id, user_id=payload.user_id,
        max_amount=payload.max_amount, currency=payload.currency,
    )
    return next(_limit_out(*row) for row in await service.list() if row[0].id == limit.id)


@router.patch("/approval-limits/{limit_id}", response_model=ApprovalLimitListResponse)
async def update_approval_limit(limit_id: uuid.UUID, payload: UpdateApprovalLimitRequest, service: Limits) -> ApprovalLimitListResponse:
    await service.update(limit_id, max_amount=payload.max_amount, currency=payload.currency, is_active=payload.is_active)
    return ApprovalLimitListResponse(limits=[_limit_out(*row) for row in await service.list()])



def _chain(db: DbSession, admin: AdminUser, request: Request) -> ApprovalChainService:
    return ApprovalChainService(db, admin, audit_context(request, admin))


Chain = Annotated[ApprovalChainService, Depends(_chain)]


async def _chain_out(service: ApprovalChainService) -> ApprovalChainResponse:
    return ApprovalChainResponse(steps=[
        ApprovalChainStepOut(
            id=step.id, category=step.category, step_order=step.step_order, label=step.label, approver_kind=step.approver_kind,
            position_id=step.position_id, position_title=title, user_id=step.user_id, user_email=email, min_amount=step.min_amount,
            parallel_with_previous=step.parallel_with_previous, stage_mode=step.stage_mode,
        )
        for step, title, email in await service.list()
    ])


@router.get("/approval-chain", response_model=ApprovalChainResponse)
async def approval_chain(service: Chain) -> ApprovalChainResponse:
    return await _chain_out(service)


@router.post("/approval-chain", response_model=ApprovalChainResponse, status_code=201)
async def add_approval_chain_step(payload: AddApprovalChainStepRequest, service: Chain) -> ApprovalChainResponse:
    await service.add(
        category=payload.category, label=payload.label, approver_kind=payload.approver_kind,
        position_id=payload.position_id, user_id=payload.user_id, min_amount=payload.min_amount,
        parallel_with_previous=payload.parallel_with_previous, stage_mode=payload.stage_mode,
    )
    return await _chain_out(service)


@router.patch("/approval-chain/{step_id}", response_model=ApprovalChainResponse)
async def update_approval_chain_step(step_id: uuid.UUID, payload: UpdateApprovalChainStepRequest, service: Chain) -> ApprovalChainResponse:
    """Run a step alongside the previous one (or on its own), or set its stage to need all or any approvals."""
    await service.set_stage(step_id, parallel_with_previous=payload.parallel_with_previous, stage_mode=payload.stage_mode)
    return await _chain_out(service)


@router.delete("/approval-chain/{step_id}", response_model=ApprovalChainResponse)
async def remove_approval_chain_step(step_id: uuid.UUID, service: Chain) -> ApprovalChainResponse:
    await service.remove(step_id)
    return await _chain_out(service)


@router.post("/approval-chain/{step_id}/move", response_model=ApprovalChainResponse)
async def move_approval_chain_step(step_id: uuid.UUID, payload: MoveApprovalChainStepRequest, service: Chain) -> ApprovalChainResponse:
    await service.move(step_id, payload.direction)
    return await _chain_out(service)



def _budget_service(db: DbSession, user: CurrentUser, request: Request) -> BudgetService:
    return BudgetService(db, user, audit_context(request, user))


Budgets = Annotated[BudgetService, Depends(_budget_service)]


async def _budgets_out(service: BudgetService) -> BudgetListResponse:
    return BudgetListResponse(budgets=[
        BudgetOut(
            id=use.budget.id, name=use.budget.name, department_group_id=use.budget.department_group_id, department_name=use.department_name,
            category=use.budget.category, period_start=use.budget.period_start, period_end=use.budget.period_end, amount=use.budget.amount,
            currency=use.budget.currency, enforcement=use.budget.enforcement, approved=use.approved, committed=use.committed, remaining=use.remaining,
        )
        for use in await service.readable()
    ])


@chart_router.get("/budgets", response_model=BudgetListResponse)
async def list_budgets(service: Budgets) -> BudgetListResponse:
    """Administrators see every budget; department managers see their own departments' budgets."""
    return await _budgets_out(service)


@chart_router.post("/budgets", response_model=BudgetListResponse, status_code=201)
async def create_budget(payload: CreateBudgetRequest, service: Budgets) -> BudgetListResponse:
    await service.create(**payload.model_dump())
    return await _budgets_out(service)


@chart_router.patch("/budgets/{budget_id}", response_model=BudgetListResponse)
async def update_budget(budget_id: uuid.UUID, payload: UpdateBudgetRequest, service: Budgets) -> BudgetListResponse:
    await service.update(budget_id, amount=payload.amount, enforcement=payload.enforcement, is_active=payload.is_active)
    return await _budgets_out(service)



def _delegation_service(db: DbSession, user: CurrentUser, request: Request) -> DelegationService:
    return DelegationService(db, user, audit_context(request, user))


Delegations = Annotated[DelegationService, Depends(_delegation_service)]


async def _delegations_out(service: DelegationService) -> DelegationListResponse:
    today = await org_today(service.db)
    return DelegationListResponse(delegations=[
        DelegationOut(
            id=row.id, delegator_user_id=row.delegator_user_id, delegator_email=delegator, delegate_user_id=row.delegate_user_id,
            delegate_email=delegate, starts_on=row.starts_on, ends_on=row.ends_on, reason=row.reason, current=row.starts_on <= today,
        )
        for row, delegator, delegate in await service.list()
    ])


@chart_router.get("/approval-delegations", response_model=DelegationListResponse)
async def list_delegations(service: Delegations) -> DelegationListResponse:
    """Current and upcoming delegations you gave or received (administrators: everyone's)."""
    return await _delegations_out(service)


@chart_router.post("/approval-delegations", response_model=DelegationListResponse, status_code=201)
async def create_delegation(payload: CreateDelegationRequest, service: Delegations) -> DelegationListResponse:
    await service.create(**payload.model_dump())
    return await _delegations_out(service)


@chart_router.post("/approval-delegations/{delegation_id}/end", response_model=DelegationListResponse)
async def end_delegation(delegation_id: uuid.UUID, service: Delegations) -> DelegationListResponse:
    await service.end(delegation_id)
    return await _delegations_out(service)
