"""SCIM 2.0 user provisioning and directory sync routes."""

import uuid
import hmac
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status

from app.api.deps import AppSettings, DbSession
from app.core.errors import InvalidLoginError, PermissionDeniedError
from app.schemas.sso import (
    ScimGroupCreate,
    ScimGroupListResponse,
    ScimGroupResponse,
    ScimListResponse,
    ScimPatchRequest,
    ScimUserCreate,
    ScimUserResponse,
)
from app.services import sso

router = APIRouter(prefix="/api/scim/v2", tags=["scim"])


def verify_scim_token(
    settings: AppSettings,
    authorization: str | None = Header(default=None),
) -> None:
    """Enforce SCIM bearer token authorization."""
    if not settings.scim_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SCIM 2.0 directory synchronization is not enabled.",
        )
    expected_token = settings.scim_bearer_token.get_secret_value()
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SCIM bearer token is not configured.",
        )
    parts = authorization.split(maxsplit=1) if authorization else []
    if len(parts) != 2 or parts[0].casefold() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
        )
    provided = parts[1].strip()
    if not hmac.compare_digest(provided.encode("utf-8"), expected_token.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid SCIM bearer token.",
        )


@router.get("/Users", response_model=ScimListResponse, dependencies=[Depends(verify_scim_token)])
async def list_users(
    db: DbSession,
    startIndex: int = Query(default=1, ge=1),
    count: int = Query(default=100, ge=0, le=100),
    filter: str | None = None,
) -> ScimListResponse:
    """List users per SCIM 2.0 query specification."""
    return await sso.scim_list_users(db, start_index=startIndex, count=count, filter_expr=filter)


@router.get("/Users/{user_id}", response_model=ScimUserResponse, dependencies=[Depends(verify_scim_token)])
async def get_user(user_id: uuid.UUID, db: DbSession) -> ScimUserResponse:
    """Get single user by UUID."""
    return await sso.scim_get_user(db, user_id)


@router.post(
    "/Users",
    response_model=ScimUserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_scim_token)],
)
async def create_user(
    payload: ScimUserCreate,
    db: DbSession,
    settings: AppSettings,
) -> ScimUserResponse:
    """Provision new user into the platform."""
    return await sso.scim_create_user(db, payload, settings)


@router.patch("/Users/{user_id}", response_model=ScimUserResponse, dependencies=[Depends(verify_scim_token)])
async def patch_user(
    user_id: uuid.UUID,
    payload: ScimPatchRequest,
    db: DbSession,
    settings: AppSettings,
) -> ScimUserResponse:
    """Apply partial update to user (e.g. activation, deactivation, roles)."""
    return await sso.scim_patch_user(db, user_id, payload, settings=settings)


@router.delete("/Users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(verify_scim_token)])
async def delete_user(user_id: uuid.UUID, db: DbSession) -> Response:
    """Deprovision user per directory sync."""
    await sso.scim_delete_user(db, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/Groups", response_model=ScimGroupListResponse, dependencies=[Depends(verify_scim_token)])
async def list_groups(
    db: DbSession,
    startIndex: int = Query(default=1, ge=1),
    count: int = Query(default=100, ge=0, le=100),
    filter: str | None = None,
) -> ScimGroupListResponse:
    """List active directory-owned groups with bounded membership responses."""
    return await sso.scim_list_groups(db, start_index=startIndex, count=count, filter_expr=filter)


@router.get("/Groups/{group_id}", response_model=ScimGroupResponse, dependencies=[Depends(verify_scim_token)])
async def get_group(group_id: uuid.UUID, db: DbSession) -> ScimGroupResponse:
    return await sso.scim_get_group(db, group_id)


@router.post(
    "/Groups",
    response_model=ScimGroupResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_scim_token)],
)
async def create_group(payload: ScimGroupCreate, db: DbSession) -> ScimGroupResponse:
    return await sso.scim_create_group(db, payload)


@router.patch("/Groups/{group_id}", response_model=ScimGroupResponse, dependencies=[Depends(verify_scim_token)])
async def patch_group(group_id: uuid.UUID, payload: ScimPatchRequest, db: DbSession) -> ScimGroupResponse:
    return await sso.scim_patch_group(db, group_id, payload)


@router.delete(
    "/Groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(verify_scim_token)]
)
async def delete_group(group_id: uuid.UUID, db: DbSession) -> Response:
    await sso.scim_delete_group(db, group_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/ServiceProviderConfig")
async def service_provider_config() -> dict[str, Any]:
    """SCIM 2.0 Service Provider Configuration."""
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False},
        "filter": {"supported": True, "maxResults": 100},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "name": "OAuth Bearer Token",
                "description": "Authentication scheme using the OAuth Bearer Token Standard",
                "specUri": "http://www.rfc-editor.org/info/rfc6750",
                "type": "oauthbearertoken",
                "primary": True,
            }
        ],
    }


@router.get("/Schemas")
async def get_schemas() -> dict[str, Any]:
    """SCIM 2.0 Schemas discovery."""
    return {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "startIndex": 1,
        "Resources": [
            {
                "id": "urn:ietf:params:scim:schemas:core:2.0:User",
                "name": "User",
                "description": "User Account",
                "schema": "urn:ietf:params:scim:schemas:core:2.0:User",
            },
            {
                "id": "urn:ietf:params:scim:schemas:core:2.0:Group",
                "name": "Group",
                "description": "Directory-owned group and membership",
                "schema": "urn:ietf:params:scim:schemas:core:2.0:Group",
            },
        ],
        "totalResults": 2,
        "itemsPerPage": 2,
    }
