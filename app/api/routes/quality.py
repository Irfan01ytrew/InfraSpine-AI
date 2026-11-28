"""Module 12 API. Authentication, MFA, CSRF and audit use the existing platform controls."""
import uuid
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from app.api.deps import AdminUser, DbSession, audit_context
from app.qa import service

router = APIRouter(prefix="/api/admin/quality", tags=["quality"])

class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite_id: str = Field(max_length=80)
    rerun_of: uuid.UUID | None = None

class SignoffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

@router.get("")
async def overview(db: DbSession, admin: AdminUser):
    return await service.overview(db, service.tenant_id(), admin.id)

@router.get("/report")
async def report(db: DbSession, admin: AdminUser):
    return await service.report(db, service.tenant_id(), admin.id)

@router.post("/runs", status_code=201)
async def enqueue(body: RunInput, request: Request, db: DbSession, admin: AdminUser):
    return await service.enqueue(db, service.tenant_id(), admin.id, body.suite_id, audit_context(request, admin), body.rerun_of)

@router.get("/runs/{run_id}")
async def detail(run_id: uuid.UUID, db: DbSession, admin: AdminUser):
    return service.run_out(await service.find_run(db, service.tenant_id(), admin.id, run_id))

@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: uuid.UUID, request: Request, db: DbSession, admin: AdminUser):
    return await service.cancel(db, service.tenant_id(), admin.id, run_id, audit_context(request, admin))

@router.post("/signoff")
async def signoff(body: SignoffInput, request: Request, db: DbSession, admin: AdminUser):
    return await service.signoff(db, service.tenant_id(), admin.id, body.snapshot_sha256, audit_context(request, admin))
