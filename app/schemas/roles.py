"""Configurable least-privilege custom roles (role templates) built from the delegable permission catalog."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.permission_catalog import PERMISSION_BY_KEY, PERMISSIONS
from app.schemas.access import Category as KnowledgeCategory
from app.schemas.access import Permission as PermissionName


BaseRole = Literal["manager", "employee"]


class RolePermissionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permission: PermissionName
    # Knowledge permissions are limited by category; department and organization capabilities take none.
    categories: list[KnowledgeCategory] = Field(default_factory=list, max_length=6)

    @field_validator("categories")
    @classmethod
    def unique_categories(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Each category can appear only once per permission.")
        return value

    @model_validator(mode="after")
    def categories_match_permission(self):
        uses_category = "category" in PERMISSION_BY_KEY[self.permission].scope_dimensions
        if uses_category and not self.categories:
            raise ValueError(f"Choose at least one category for {self.permission}.")
        if not uses_category and self.categories:
            raise ValueError(f"{self.permission} is not limited by category.")
        return self


class CreateRoleTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=500)
    base_role: BaseRole
    permissions: list[RolePermissionSpec] = Field(min_length=1, max_length=len(PERMISSIONS))

    @model_validator(mode="after")
    def unique_permission_category_pairs(self):
        return _unique_permissions(self)


def _unique_permissions(request):
    permissions = [spec.permission for spec in request.permissions]
    if len(set(permissions)) != len(permissions):
        raise ValueError("Each permission can appear only once in a role.")
    return request


class RoleTemplateActiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool


class RoleTemplateChangeCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    description: str = Field(default="", max_length=500)
    permissions: list[RolePermissionSpec] = Field(min_length=1, max_length=len(PERMISSIONS))

    @model_validator(mode="after")
    def unique_permission_category_pairs(self):
        return _unique_permissions(self)


class RoleTemplateChangeDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["approve", "reject"]
    rationale: str = Field(min_length=3, max_length=2000)


class AssignRoleTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=320)
    department_ids: list[uuid.UUID] = Field(min_length=1, max_length=25)
    expires_at: datetime

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        if value.count("@") != 1 or "." not in value.rsplit("@", 1)[-1] or any(ch.isspace() for ch in value):
            raise ValueError("Enter a valid email address.")
        return value.lower()

    @field_validator("department_ids")
    @classmethod
    def unique_departments(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(value)) != len(value):
            raise ValueError("A department can only be selected once.")
        return value

    @field_validator("expires_at")
    @classmethod
    def bounded_expiration(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("A role assignment expiry must include a timezone.")
        now = datetime.now(UTC)
        if value <= now:
            raise ValueError("A role assignment expiry must be in the future.")
        if value > now + timedelta(days=365):
            raise ValueError("A role assignment cannot last longer than 365 days.")
        return value


class RoleTemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    base_role: str
    version: int
    permissions: list[RolePermissionSpec]
    is_active: bool
    supersedes_id: uuid.UUID | None = None
    created_at: datetime


class RoleTemplateChangeOut(BaseModel):
    id: uuid.UUID
    role_template_id: uuid.UUID
    template_name: str
    current_version: int
    requested_by: uuid.UUID
    requested_by_email: str
    proposed_description: str
    proposed_permissions: list[RolePermissionSpec]
    status: Literal["pending", "approved", "rejected"]
    decided_by: uuid.UUID | None
    decided_by_email: str | None
    decision_rationale: str | None
    new_template_id: uuid.UUID | None
    created_at: datetime
    decided_at: datetime | None


class RoleTemplateChangeListResponse(BaseModel):
    requests: list[RoleTemplateChangeOut]


class RoleTemplateListResponse(BaseModel):
    templates: list[RoleTemplateOut]


class RoleTemplateAssignmentOut(BaseModel):
    id: uuid.UUID
    role_template_id: uuid.UUID
    template_name: str
    template_version: int
    user_id: uuid.UUID
    email: str
    base_role: str
    department_ids: list[str]
    grant_count: int
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None


class RoleTemplateAssignmentListResponse(BaseModel):
    assignments: list[RoleTemplateAssignmentOut]
