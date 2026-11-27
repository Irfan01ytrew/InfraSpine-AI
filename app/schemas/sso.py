"""Pydantic schemas for Enterprise SSO (OIDC/SAML) and SCIM 2.0 User Provisioning."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SsoConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    provider_type: str
    authorization_url: str | None = None


class SsoCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    state: str | None = None
    id_token: str | None = None


class SsoSamlAssertionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    saml_response: str = Field(min_length=1)


class ScimName(BaseModel):
    model_config = ConfigDict(extra="ignore")

    formatted: str | None = None
    familyName: str | None = None
    givenName: str | None = None


class ScimEmail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: str
    type: str = "work"
    primary: bool = True


class ScimUserCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schemas: list[str] = ["urn:ietf:params:scim:schemas:core:2.0:User"]
    userName: str = Field(min_length=3)
    externalId: str | None = Field(default=None, min_length=1, max_length=256)
    name: ScimName | None = None
    emails: list[ScimEmail] = []
    active: bool = True
    roles: list[str] = []


class ScimUserResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schemas: list[str] = ["urn:ietf:params:scim:schemas:core:2.0:User"]
    id: str
    userName: str
    externalId: str | None = None
    name: ScimName | None = None
    emails: list[ScimEmail] = []
    active: bool = True
    roles: list[str] = []
    meta: dict[str, Any] = {}


class ScimListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schemas: list[str] = ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    totalResults: int
    startIndex: int
    itemsPerPage: int
    Resources: list[ScimUserResponse]


class ScimGroupMember(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: str = Field(min_length=1, max_length=36)
    display: str | None = Field(default=None, max_length=320)
    type: str = "User"


class ScimGroupCreate(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    schemas: list[str] = ["urn:ietf:params:scim:schemas:core:2.0:Group"]
    externalId: str = Field(min_length=1, max_length=256)
    displayName: str = Field(min_length=1, max_length=120)
    members: list[ScimGroupMember] = Field(default_factory=list, max_length=1000)


class ScimGroupResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schemas: list[str] = ["urn:ietf:params:scim:schemas:core:2.0:Group"]
    id: str
    externalId: str
    displayName: str
    members: list[ScimGroupMember] = Field(default_factory=list, max_length=1000)
    meta: dict[str, Any] = Field(default_factory=dict)


class ScimGroupListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schemas: list[str] = ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    totalResults: int
    startIndex: int
    itemsPerPage: int
    Resources: list[ScimGroupResponse]


class ScimPatchOperation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    op: str  # "add", "replace", "remove"
    path: str | None = None
    value: Any = None


class ScimPatchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schemas: list[str] = ["urn:ietf:params:scim:api:messages:2.0:PatchOp"]
    Operations: list[ScimPatchOperation]
