"""Validated payloads for Agent Studio's Meta WhatsApp Cloud API connection."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class WhatsAppConnectionSave(BaseModel):
    agent_id: UUID
    phone_number_id: str = Field(min_length=5, max_length=64)
    business_account_id: str = Field(min_length=5, max_length=64)
    allowed_senders: list[str] = Field(min_length=1, max_length=30)
    api_version: str = Field(default="v26.0", min_length=4, max_length=12)
    access_token: SecretStr | None = None
    app_secret: SecretStr | None = None
    verify_token: SecretStr | None = None


class WhatsAppConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    agent_name: str
    phone_number_id: str
    business_account_id: str
    allowed_senders: list[str]
    api_version: str
    enabled: bool
    verified_name: str | None
    display_phone_number: str | None
    last_verified_at: datetime | None
    access_token_configured: bool
    app_secret_configured: bool
    verify_token_configured: bool
    webhook_url: str
    created_at: datetime
    updated_at: datetime


class WhatsAppConnectionResponse(BaseModel):
    connection: WhatsAppConnectionOut | None
    # Step F slice 3: the owner's runtime path setting and the transport a
    # reply would actually use right now (None when neither path can send).
    path_setting: str = "auto"
    active_path: str | None = None
    cloud_available: bool = False
    baileys_available: bool = False


class WhatsAppPathSave(BaseModel):
    path: Literal["cloud", "baileys", "auto"]


class WhatsAppPathResponse(BaseModel):
    path_setting: str
    active_path: str | None
    cloud_available: bool
    baileys_available: bool


class WhatsAppRotateSecret(BaseModel):
    # Step F slice 6: rotate one stored secret at a time (the old value
    # stops verifying immediately).
    secret: Literal["access_token", "app_secret", "verify_token"]
    new_value: SecretStr


class WhatsAppVerifyResponse(BaseModel):
    verified_name: str | None
    display_phone_number: str | None
    subscribed: bool = True
