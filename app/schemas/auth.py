import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(..., min_length=3, max_length=320)
    password: SecretStr = Field(..., min_length=1, max_length=1024)


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    mfa_enrollment_required: bool = False
    mfa_reauthentication_required: bool = False
    # Delegated business permissions this account currently holds (e.g. "reports.view"); administrators hold all.
    capabilities: list[str] = []


class MfaChallengeResponse(BaseModel):
    mfa_required: Literal[True] = True
    challenge_token: str


class MfaChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    challenge_token: SecretStr = Field(..., min_length=32, max_length=128)
    code: SecretStr = Field(..., min_length=6, max_length=64)


class MfaEnrollmentStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: SecretStr = Field(..., min_length=1, max_length=1024)


class MfaCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: SecretStr = Field(..., min_length=6, max_length=64)


class MfaPasswordCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    password: SecretStr = Field(..., min_length=1, max_length=1024)
    code: SecretStr = Field(..., min_length=6, max_length=64)


class MfaEnrollmentResponse(BaseModel):
    secret: str
    provisioning_uri: str


class MfaRecoveryCodesResponse(BaseModel):
    recovery_codes: list[str]


class MfaStatusResponse(BaseModel):
    enabled: bool
    recovery_codes_remaining: int
    required_by_organization: bool = False
