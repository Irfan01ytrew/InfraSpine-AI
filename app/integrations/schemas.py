"""Bounded configuration; mappings are data, never scripts or expressions."""
import base64
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

PATH = r"^/[A-Za-z0-9_./@{}-]*$"
FIELD = r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Mapping(Input):
    source: str = Field(max_length=160, pattern=FIELD)
    target: str = Field(max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    transform: Literal["identity", "string", "integer", "lowercase", "uppercase", "boolean"] = "identity"
    required: bool = False


class ConnectorConfig(Input):
    sync_path: str = Field(default="/", max_length=500, pattern=PATH)
    verify_path: str = Field(default="/", max_length=500, pattern=PATH)
    write_path: str = Field(default="/", max_length=500, pattern=PATH)
    records_path: str = Field(default="items", max_length=160, pattern=FIELD)
    id_field: str = Field(default="id", max_length=160, pattern=FIELD)
    next_cursor_path: str = Field(default="next_cursor", max_length=160, pattern=FIELD)
    cursor_parameter: str = Field(default="cursor", max_length=60, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    mappings: list[Mapping] = Field(default_factory=list, max_length=100)
    schema_version: int = Field(default=1, ge=1, le=10000)
    conflict_policy: Literal["source_wins", "reject_changes"] = "source_wins"
    interval_seconds: int = Field(default=0, ge=0, le=86400)
    max_pages: int = Field(default=5, ge=1, le=10)
    scopes: list[str] = Field(default_factory=list, max_length=30)
    graphql_query: str = Field(default="", max_length=10000)
    target_module: Literal["knowledge", "irfanix", "company_graph", "process", "workflow", "governance", "recovery", "continuity"] = "workflow"
    event_types: list[str] = Field(default_factory=lambda: ["business.event"], max_length=30)
    auth_header: Literal["Authorization", "X-API-Key"] = "Authorization"
    microsoft_tenant: str = Field(default="organizations", pattern=r"^(organizations|[0-9a-fA-F-]{36})$")
    redirect_uri: str = Field(default="", max_length=1000)

    @field_validator("sync_path", "verify_path", "write_path")
    @classmethod
    def safe_path(cls, value):
        if ".." in value or "//" in value or "{" in value or "}" in value:
            raise ValueError("Use a fixed relative API path without traversal or placeholders.")
        return value

    @model_validator(mode="after")
    def validate_config(self):
        if self.interval_seconds and self.interval_seconds < 60:
            raise ValueError("Scheduled synchronization must be at least 60 seconds apart.")
        if len({m.target for m in self.mappings}) != len(self.mappings):
            raise ValueError("Mapping target names must be unique.")
        if any(not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", item) for item in self.event_types):
            raise ValueError("Invalid event type.")
        if any(len(item) > 200 or any(c.isspace() for c in item) for item in self.scopes):
            raise ValueError("Invalid OAuth scope.")
        return self


class Credentials(Input):
    access_token: SecretStr = SecretStr("")
    refresh_token: SecretStr = SecretStr("")
    client_id: SecretStr = SecretStr("")
    client_secret: SecretStr = SecretStr("")
    webhook_secret: SecretStr = SecretStr("")
    expires_at: datetime | None = None

    @field_validator("access_token", "refresh_token", "client_id", "client_secret", "webhook_secret")
    @classmethod
    def safe_secret(cls, value):
        raw = value.get_secret_value()
        if len(raw) > 10000 or any(ord(c) < 32 for c in raw):
            raise ValueError("Invalid credential format.")
        return value

    def plaintext(self):
        return {key: value.get_secret_value() if isinstance(value, SecretStr) else (value.isoformat() if value else None)
                for key, value in self.__dict__.items()}


class ConnectorInput(Input):
    name: str = Field(min_length=2, max_length=120)
    provider: Literal["rest", "graphql", "soap", "microsoft365", "gmail", "google_calendar", "google_drive", "slack"]
    base_url: str = Field(min_length=8, max_length=1000)
    config: ConnectorConfig = Field(default_factory=ConnectorConfig)
    credentials: Credentials = Field(default_factory=Credentials)


class ConnectorPatch(Input):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=2, max_length=120)
    enabled: bool | None = None
    config: ConnectorConfig | None = None
    credentials: Credentials | None = None


class JobInput(Input):
    operation: Literal["verify", "sync", "rest.write", "soap.call", "email.send", "calendar.create", "slack.send"]
    idempotency_key: str = Field(min_length=8, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    payload: dict = Field(default_factory=dict)


class Decision(Input):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    approve: bool


class EventInput(Input):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    type: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    schema_version: Literal[1] = 1
    data: dict


class Attachment(Input):
    name: str = Field(min_length=1, max_length=120, pattern=r"^[^/\\\x00-\x1f]+$")
    content_type: Literal["application/pdf", "text/plain", "image/png", "image/jpeg"]
    content_base64: str = Field(max_length=1400000)

    @model_validator(mode="after")
    def validate_bytes(self):
        try:
            data = base64.b64decode(self.content_base64, validate=True)
        except ValueError:
            raise ValueError("Attachment must be valid base64.") from None
        signatures = {"application/pdf": b"%PDF-", "image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}
        if not data or len(data) > 1000000 or (self.content_type in signatures and not data.startswith(signatures[self.content_type])):
            raise ValueError("Attachment size or content type is invalid.")
        if self.content_type == "text/plain":
            data.decode("utf-8")
        return self


class Email(Input):
    to: list[str] = Field(min_length=1, max_length=20)
    subject: str = Field(min_length=1, max_length=240)
    text: str = Field(max_length=20000)
    attachments: list[Attachment] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def headers(self):
        if any(not re.fullmatch(r"[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+", x) or len(x) > 254 for x in self.to) or any(c in self.subject for c in "\r\n"):
            raise ValueError("Invalid email header.")
        return self


class CalendarEvent(Input):
    title: str = Field(min_length=1, max_length=240)
    start: datetime
    end: datetime
    attendees: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def dates(self):
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise ValueError("Use timezone-aware dates and an end after the start.")
        for email in self.attendees:
            Email(to=[email], subject="Validation", text="")
        return self


class SlackMessage(Input):
    channel: str = Field(pattern=r"^[CDG][A-Z0-9]{4,40}$")
    text: str = Field(min_length=1, max_length=4000)
