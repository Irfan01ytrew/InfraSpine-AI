"""Foundry API (Step AM slice 6): registry reads, build recording, deploy
requests and feedback.

Every route authorises on the platform scope in the URL: only scope
members see an app (unknown scope / non-member → 404, member without the
permission → 403). Reads need `foundry.read`; writes need
`foundry.manage`. A deploy request only *requests* the platform approval
for the exact build — the deploy itself needs a different person to
approve it (slice 4).
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AppSettings, CurrentUser, DbSession
from app.foundry.registry import BuildRecord
from app.foundry.service import (
    get_app_detail as service_get_app_detail,
)
from app.foundry.service import (
    list_apps as service_list_apps,
)
from app.foundry.service import (
    list_scopes as service_list_scopes,
)
from app.foundry.service import (
    record_build as service_record_build,
)
from app.foundry.service import (
    request_deploy as service_request_deploy,
)
from app.foundry.service import (
    submit_feedback as service_submit_feedback,
)
from app.platform.access import principal_for

router = APIRouter(prefix="/api/foundry", tags=["foundry"])


class BuildIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_name: str = Field(min_length=3, max_length=64)
    spec_version: int = Field(gt=0)
    spec_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    build_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    gate: Literal["blocked", "preview_ready"]
    preview_token: str = Field(default="", max_length=64)
    preview_expires_in_seconds: int = Field(default=0, ge=0, le=3600)
    health: Literal["pending", "passed", "failed"] = "pending"
    tests_passed: int = Field(default=0, ge=0)
    tests_failed: int = Field(default=0, ge=0)
    spec: dict[str, Any] = Field(default_factory=dict)


class DeployRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    build_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    target: str = Field(min_length=1, max_length=64)


class FeedbackChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    name: str | None = None
    path: str | None = None
    method: str | None = None
    table: str | None = None
    columns: list[list[str]] = Field(default_factory=list)
    endpoints: list[str] = Field(default_factory=list)
    new_name: str | None = None
    purpose: str | None = None


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    build_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    kind: Literal["bug", "change_request", "praise", "question"]
    note: str = Field(default="", max_length=5000)
    changes: list[FeedbackChangeIn] = Field(default_factory=list)


@router.get("/scopes")
async def list_foundry_scopes(db: DbSession, user: CurrentUser):
    p = principal_for(user)
    return {"scopes": await service_list_scopes(db, p)}


@router.get("/scopes/{scope_id}/apps")
async def list_foundry_apps(scope_id: UUID, db: DbSession, user: CurrentUser):
    p = principal_for(user)
    apps = await service_list_apps(db, p, scope_id)
    return {"apps": apps}


@router.get("/scopes/{scope_id}/apps/{slug}")
async def get_foundry_app(scope_id: UUID, slug: str, db: DbSession, user: CurrentUser):
    p = principal_for(user)
    return await service_get_app_detail(db, p, scope_id, slug)


@router.post("/scopes/{scope_id}/apps/{slug}/builds")
async def record_foundry_build(scope_id: UUID, slug: str, body: BuildIn,
                               db: DbSession, user: CurrentUser):
    p = principal_for(user)
    build = BuildRecord(
        slug=slug, app_name=body.app_name, spec_version=body.spec_version,
        spec_fingerprint=body.spec_fingerprint,
        build_fingerprint=body.build_fingerprint, gate=body.gate,
        preview_token=body.preview_token,
        preview_expires_in_seconds=body.preview_expires_in_seconds,
        health=body.health, tests_passed=body.tests_passed,
        tests_failed=body.tests_failed)
    return await service_record_build(db, p, scope_id, slug, body.app_name,
                                     build, body.spec)


@router.post("/scopes/{scope_id}/apps/{slug}/deploy-request")
async def request_foundry_deploy(scope_id: UUID, slug: str, body: DeployRequestIn,
                                 db: DbSession, user: CurrentUser, settings: AppSettings):
    p = principal_for(user)
    allowed_targets = tuple(
        t.strip() for t in (settings.foundry_deploy_targets or "").split(",") if t.strip())
    return await service_request_deploy(db, p, scope_id, slug,
                                       body.build_fingerprint, body.target,
                                       allowed_targets=allowed_targets)


@router.post("/scopes/{scope_id}/apps/{slug}/feedback")
async def submit_foundry_feedback(scope_id: UUID, slug: str, body: FeedbackIn,
                                  db: DbSession, user: CurrentUser):
    p = principal_for(user)
    return await service_submit_feedback(
        db, p, scope_id, slug, body.build_fingerprint, body.kind, body.note,
        [change.model_dump() for change in body.changes])
