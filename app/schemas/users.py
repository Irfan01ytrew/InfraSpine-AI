import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DepartmentAssignment(BaseModel):
    id: uuid.UUID
    name: str
    manager: bool


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    is_active: bool
    created_at: datetime
    departments: list[DepartmentAssignment]


class UserListResponse(BaseModel):
    users: list[UserOut]


class InviteUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(..., min_length=3, max_length=320)
    role: Literal["manager", "employee"] = "employee"
    department_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    # A custom role (role template) with the same base role, applied in these departments at sign-up.
    role_template_id: uuid.UUID | None = None
    # Organization placement at sign-up (job title and manager); neither grants access.
    position_id: uuid.UUID | None = None
    reports_to_id: uuid.UUID | None = None

    @field_validator("email")
    @classmethod
    def valid_email_shape(cls, value: str) -> str:
        if value.count("@") != 1 or "." not in value.rsplit("@", 1)[-1] or any(ch.isspace() for ch in value):
            raise ValueError("Enter a valid email address.")
        return value.lower()


class UserInvitationOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    department_ids: list[str]
    role_template_id: uuid.UUID | None = None
    position_id: uuid.UUID | None = None
    reports_to_id: uuid.UUID | None = None
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    token: str | None = None


class InvitationListResponse(BaseModel):
    invitations: list[UserInvitationOut]


class CsvInvitationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv_text: str = Field(..., min_length=1, max_length=1_000_000)


class CsvInvitationPreviewRow(BaseModel):
    row_number: int = Field(..., ge=2)
    email: str | None = None
    role: Literal["manager", "employee"] | None = None
    department_ids: list[uuid.UUID] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    position_id: uuid.UUID | None = None
    position_title: str | None = None
    reports_to_id: uuid.UUID | None = None
    reports_to_email: str | None = None
    valid: bool
    errors: list[str] = Field(default_factory=list)


class CsvInvitationPreviewResponse(BaseModel):
    total_rows: int = Field(..., ge=0)
    valid_rows: int = Field(..., ge=0)
    invalid_rows: int = Field(..., ge=0)
    invitations_sent: bool = False
    rows: list[CsvInvitationPreviewRow]


class ManagerInviteEmployeeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(..., min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def valid_email_shape(cls, value: str) -> str:
        if value.count("@") != 1 or "." not in value.rsplit("@", 1)[-1] or any(ch.isspace() for ch in value):
            raise ValueError("Enter a valid email address.")
        return value.lower()


class UpdateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["admin", "manager", "employee"]
    department_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)


class ActiveStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool
    knowledge_transfer_to: uuid.UUID | None = None


class KnowledgeTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    successor_user_id: uuid.UUID


class KnowledgeTransferResponse(BaseModel):
    transferred_entries: int = Field(..., ge=0)


class AcceptInvitationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(..., min_length=30, max_length=128)
    password: str = Field(..., min_length=8, max_length=256)

    @field_validator("password")
    @classmethod
    def printable_password(cls, value: str) -> str:
        if any(not ch.isprintable() for ch in value):
            raise ValueError("Password contains an invisible or control character.")
        return value


class OnboardingAccepted(BaseModel):
    email: str
    role: str
    message: str
