"""Admin-only AI provider management. Every route requires an authenticated admin."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from app.api.deps import get_provider_admin, require_admin
from app.schemas.providers import (
    AuditLogEntry,
    AuditLogResponse,
    CreateCustomProviderRequest,
    ImageSharingRequest,
    ProviderCatalogResponse,
    ProviderListResponse,
    ProviderMetadata,
    ProviderState,
    StageCredentialRequest,
    TestConnectionRequest,
    TestConnectionResponse,
    UpdateProviderRequest,
    VersionedActionRequest,
)
from app.services.provider_admin import ProviderAdminService

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

Admin = Annotated[ProviderAdminService, Depends(get_provider_admin)]
ProviderId = Annotated[str, Path(pattern=r"^[a-z0-9_-]{1,50}$")]


@router.get("/providers/catalog", response_model=ProviderCatalogResponse)
async def provider_catalog(admin: Admin) -> ProviderCatalogResponse:
    """Backend-driven metadata the admin UI renders from (providers, models, settings)."""
    return ProviderCatalogResponse(
        providers=[ProviderMetadata(**p.to_public_dict()) for p in await admin.provider_catalog()]
    )


@router.get("/providers", response_model=ProviderListResponse)
async def list_provider_states(admin: Admin) -> ProviderListResponse:
    return ProviderListResponse(providers=await admin.list_states())


@router.get("/providers/{provider_id}", response_model=ProviderState)
async def get_provider_state(provider_id: ProviderId, admin: Admin) -> ProviderState:
    return await admin.get_state(provider_id)


@router.post("/providers/custom", response_model=ProviderState)
async def create_custom_provider(payload: CreateCustomProviderRequest, admin: Admin) -> ProviderState:
    return await admin.create_custom_provider(
        payload.display_name,
        payload.base_url,
        payload.model,
        payload.api_key.get_secret_value(),
    )


@router.put("/providers/{provider_id}", response_model=ProviderState)
async def update_provider(provider_id: ProviderId, payload: UpdateProviderRequest, admin: Admin) -> ProviderState:
    api_key = payload.api_key.get_secret_value() if payload.api_key else None
    return await admin.update_config(
        provider_id, payload.model, payload.settings, payload.expected_version, payload.base_url, api_key
    )


@router.post("/providers/{provider_id}/test", response_model=TestConnectionResponse)
async def test_provider_connection(
    provider_id: ProviderId, payload: TestConnectionRequest, admin: Admin
) -> TestConnectionResponse:
    api_key = payload.api_key.get_secret_value() if payload.api_key else None
    return await admin.test_connection(provider_id, api_key, payload.model, payload.base_url)


@router.post("/providers/{provider_id}/credentials", response_model=ProviderState)
async def stage_provider_credential(
    provider_id: ProviderId, payload: StageCredentialRequest, admin: Admin
) -> ProviderState:
    return await admin.stage_credential(
        provider_id, payload.api_key.get_secret_value(), payload.expected_version
    )


@router.post("/providers/{provider_id}/activate", response_model=ProviderState)
async def activate_provider(provider_id: ProviderId, payload: VersionedActionRequest, admin: Admin) -> ProviderState:
    return await admin.activate(provider_id, payload.expected_version)


@router.post("/providers/{provider_id}/deactivate", response_model=ProviderState)
async def deactivate_provider(
    provider_id: ProviderId, payload: VersionedActionRequest, admin: Admin
) -> ProviderState:
    return await admin.deactivate(provider_id, payload.expected_version)


@router.put("/providers/{provider_id}/image-sharing", response_model=ProviderState)
async def set_image_sharing(provider_id: ProviderId, payload: ImageSharingRequest, admin: Admin) -> ProviderState:
    return await admin.set_image_sharing(provider_id, payload.authorized, payload.expected_version)


@router.delete("/providers/{provider_id}", status_code=204)
async def remove_provider(
    provider_id: ProviderId, admin: Admin, expected_version: Annotated[int, Query(ge=1)]
) -> Response:
    await admin.remove(provider_id, expected_version)
    return Response(status_code=204)


@router.get("/audit-logs", response_model=AuditLogResponse)
async def audit_logs(admin: Admin, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> AuditLogResponse:
    entries = await admin.recent_audit_logs(limit)
    return AuditLogResponse(entries=[AuditLogEntry.model_validate(e, from_attributes=True) for e in entries])
