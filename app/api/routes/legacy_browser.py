"""Approval-gated, bounded, read-only legacy-browser workflows."""

import uuid

from fastapi import APIRouter, Depends, Request

from app.api.deps import AdminUser, AppSettings, CurrentUser, DbSession, audit_context, get_current_user
from app.db.models import ROLE_ADMIN
from app.schemas.legacy_browser import (
    LegacyBrowserApprovalOut,
    LegacyBrowserRequest,
    LegacyBrowserRunListOut,
    LegacyBrowserRunOut,
    LegacyBrowserStatusOut,
)
from app.services import legacy_browser_runs

router = APIRouter(
    prefix="/api/legacy-browser",
    tags=["legacy-browser"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/status", response_model=LegacyBrowserStatusOut)
async def browser_status(settings: AppSettings) -> LegacyBrowserStatusOut:
    return legacy_browser_runs.status(settings)


@router.get("/runs", response_model=LegacyBrowserRunListOut)
async def list_runs(db: DbSession, user: CurrentUser) -> LegacyBrowserRunListOut:
    return LegacyBrowserRunListOut(items=await legacy_browser_runs.list_runs(db, user))


@router.post("/runs", response_model=LegacyBrowserRunOut, status_code=201)
async def create_run(
    payload: LegacyBrowserRequest, db: DbSession, user: CurrentUser, request: Request, settings: AppSettings
):
    return await legacy_browser_runs.create_run(db, user, audit_context(request, user), payload, settings)


@router.post("/runs/{run_id}/approve", response_model=LegacyBrowserApprovalOut)
async def approve_run(
    run_id: uuid.UUID, db: DbSession, admin: AdminUser, request: Request, settings: AppSettings
) -> LegacyBrowserApprovalOut:
    return await legacy_browser_runs.approve_run(db, admin, audit_context(request, admin), run_id, settings)


@router.post("/runs/{run_id}/reject", response_model=LegacyBrowserRunOut)
async def reject_run(run_id: uuid.UUID, db: DbSession, admin: AdminUser, request: Request):
    return await legacy_browser_runs.reject_run(db, admin, audit_context(request, admin), run_id)


@router.post("/runs/{run_id}/cancel", response_model=LegacyBrowserRunOut)
async def cancel_run(run_id: uuid.UUID, db: DbSession, user: CurrentUser, request: Request):
    return await legacy_browser_runs.cancel_run(db, user, audit_context(request, user), run_id)
