"""Administrator API for outgoing webhooks."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AdminUser, DbSession, audit_context, require_admin
from app.db.webhook_models import WEBHOOK_EVENTS, Webhook, WebhookDelivery
from app.services.webhooks import WebhookService

router = APIRouter(prefix="/api/admin/webhooks", tags=["webhooks"], dependencies=[Depends(require_admin)])
Event = Literal[WEBHOOK_EVENTS]  # type: ignore[valid-type]


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=2, max_length=120)
    url: str = Field(..., min_length=8, max_length=2000)
    events: list[Event] = Field(..., min_length=1, max_length=len(WEBHOOK_EVENTS))


class WebhookPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=2, max_length=120)
    url: str | None = Field(default=None, min_length=8, max_length=2000)
    events: list[Event] | None = Field(default=None, min_length=1, max_length=len(WEBHOOK_EVENTS))
    is_active: bool | None = None


class DeliveryOut(BaseModel):
    id: uuid.UUID
    event: str
    status: str
    attempts: int
    response_status: int | None
    last_error: str | None
    duration_ms: int | None
    created_at: datetime
    sent_at: datetime | None
    next_attempt_at: datetime | None


class WebhookOut(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    events: list[str]
    is_active: bool
    consecutive_failures: int
    disabled_reason: str | None
    created_at: datetime
    last_delivery: DeliveryOut | None


class WebhookListResponse(BaseModel):
    webhooks: list[WebhookOut]
    events: list[str]


class WebhookSecretResponse(WebhookListResponse):
    # Shown once; store it in the receiving system to verify signatures.
    secret: str


class DeliveryListResponse(BaseModel):
    deliveries: list[DeliveryOut]


def _service(db: DbSession, admin: AdminUser, request: Request) -> WebhookService:
    return WebhookService(db, admin, audit_context(request, admin))


Hooks = Annotated[WebhookService, Depends(_service)]


def _delivery(d: WebhookDelivery) -> DeliveryOut:
    return DeliveryOut(id=d.id, event=d.event, status=d.status, attempts=d.attempts, response_status=d.response_status,
                       last_error=d.last_error, duration_ms=d.duration_ms, created_at=d.created_at, sent_at=d.sent_at,
                       next_attempt_at=d.next_attempt_at)


def _hook(h: Webhook, last: WebhookDelivery | None) -> WebhookOut:
    return WebhookOut(id=h.id, name=h.name, url=h.url, events=h.events, is_active=h.is_active, consecutive_failures=h.consecutive_failures,
                      disabled_reason=h.disabled_reason, created_at=h.created_at, last_delivery=_delivery(last) if last else None)


async def _out(service: WebhookService) -> WebhookListResponse:
    return WebhookListResponse(webhooks=[_hook(h, last) for h, last in await service.list()], events=list(WEBHOOK_EVENTS))


@router.get("", response_model=WebhookListResponse)
async def list_webhooks(service: Hooks) -> WebhookListResponse:
    return await _out(service)


@router.post("", response_model=WebhookSecretResponse, status_code=201)
async def create_webhook(payload: WebhookIn, service: Hooks) -> WebhookSecretResponse:
    _, secret = await service.create(name=payload.name, url=payload.url, events=list(payload.events))
    return WebhookSecretResponse(**(await _out(service)).model_dump(), secret=secret)


@router.patch("/{webhook_id}", response_model=WebhookListResponse)
async def update_webhook(webhook_id: uuid.UUID, payload: WebhookPatch, service: Hooks) -> WebhookListResponse:
    await service.update(webhook_id, name=payload.name, url=payload.url,
                         events=list(payload.events) if payload.events is not None else None, is_active=payload.is_active)
    return await _out(service)


@router.post("/{webhook_id}/rotate-secret", response_model=WebhookSecretResponse)
async def rotate_secret(webhook_id: uuid.UUID, service: Hooks) -> WebhookSecretResponse:
    secret = await service.rotate_secret(webhook_id)
    return WebhookSecretResponse(**(await _out(service)).model_dump(), secret=secret)


@router.post("/{webhook_id}/test", response_model=WebhookListResponse)
async def test_webhook(webhook_id: uuid.UUID, service: Hooks) -> WebhookListResponse:
    """Queue a `webhook.test` delivery; the worker sends it within seconds."""
    await service.send_test(webhook_id)
    return await _out(service)


@router.delete("/{webhook_id}", response_model=WebhookListResponse)
async def delete_webhook(webhook_id: uuid.UUID, service: Hooks) -> WebhookListResponse:
    await service.archive(webhook_id)
    return await _out(service)


@router.get("/{webhook_id}/deliveries", response_model=DeliveryListResponse)
async def list_deliveries(webhook_id: uuid.UUID, service: Hooks) -> DeliveryListResponse:
    return DeliveryListResponse(deliveries=[_delivery(d) for d in await service.deliveries(webhook_id)])


@router.post("/deliveries/{delivery_id}/redeliver", response_model=WebhookListResponse)
async def redeliver(delivery_id: uuid.UUID, service: Hooks) -> WebhookListResponse:
    await service.redeliver(delivery_id)
    return await _out(service)
