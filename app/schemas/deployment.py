"""Company setup and offline license payloads."""

from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CompanyProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(min_length=2, max_length=120)
    industry: str = Field(min_length=2, max_length=80)
    brand_name: str = Field(min_length=2, max_length=48)
    primary_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    setup_complete: bool = True
    template_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    template_version: str | None = Field(default=None, min_length=1, max_length=32)
    # Regional formatting. None keeps each viewer's browser setting, which is how existing installs behave.
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    time_zone: str | None = Field(default=None, max_length=64)
    locale: str | None = Field(default=None, pattern=r"^[a-z]{2,3}(-[A-Z][a-z]{3})?(-(?:[A-Z]{2}|[0-9]{3}))?$")

    @field_validator("time_zone")
    @classmethod
    def known_time_zone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("time_zone must be an IANA time zone such as Asia/Karachi") from exc
        return value

    @field_validator("company_name", "industry", "brand_name")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must contain non-whitespace characters")
        return value

    @model_validator(mode="after")
    def template_version_is_paired(self) -> "CompanyProfile":
        if (self.template_id is None) != (self.template_version is None):
            raise ValueError("template_id and template_version must be set together")
        return self


class IndustryTemplateOut(BaseModel):
    template_id: str
    version: str
    name: str
    industry: str
    description: str
    default_departments: list[str]
    knowledge_areas: list[str]
    default_roles: list[str] = []


class IndustryTemplateApplyOut(BaseModel):
    template_id: str
    version: str
    created_departments: list[str]
    existing_departments: list[str]
    created_roles: list[str] = []
    existing_roles: list[str] = []


class LicenseClaims(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: Literal["infraspine-ai"]
    license_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    customer: str = Field(min_length=2, max_length=120)
    deployment_id: str = Field(min_length=36, max_length=36)
    issued_at: datetime
    expires_at: datetime
    seat_limit: int = Field(ge=1, le=1_000_000)
    features: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("features")
    @classmethod
    def validate_features(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(
            not value or len(value) > 64 or not all(ch.isalnum() or ch in "._-" for ch in value)
            for value in values
        ):
            raise ValueError("features must be unique short identifiers")
        return values

    @field_validator("customer")
    @classmethod
    def trim_customer(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("customer must contain non-whitespace characters")
        return value

    @field_validator("deployment_id")
    @classmethod
    def normalize_deployment_id(cls, value: str) -> str:
        from uuid import UUID

        try:
            return str(UUID(value))
        except ValueError:
            raise ValueError("deployment_id must be a UUID") from None

    @field_validator("issued_at", "expires_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("license dates must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_dates(self) -> "LicenseClaims":
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        return self


class SignedLicense(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: LicenseClaims
    signature: str = Field(min_length=80, max_length=128)


class LicenseStatus(BaseModel):
    status: Literal["not_configured", "missing", "invalid", "expired", "valid"]
    enforcement_enabled: bool
    deployment_id: str
    license_id: str | None = None
    customer: str | None = None
    expires_at: datetime | None = None
    seat_limit: int | None = None
    features: list[str] = Field(default_factory=list)
