"""Module 10 API: authenticated owner/tenant routes and signed public ingress."""
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, audit_context
from app.company_graph.service import list_tenants
from app.core.errors import InvalidInputError, NotFoundError
from app.integrations import oauth
from app.integrations.catalog import CATALOG
from app.integrations.models import IntegrationEvent, IntegrationRecord
from app.integrations.schemas import ConnectorInput, ConnectorPatch, Decision, Input, JobInput
from app.integrations.security import scope, unseal
from app.integrations.service import IntegrationService, MAX_INPUT, MAX_WEBHOOK, accept_event, public_job
from app.integrations.transport import TransportError

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


async def body(request, limit=MAX_INPUT):
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > limit: raise InvalidInputError("Integration request exceeds the payload limit.")
        raw.extend(chunk)
    return bytes(raw)


async def parse(request, schema):
    try:
        return schema.model_validate_json(await body(request))
    except ValueError:
        # Validation errors may contain submitted credentials. Return no raw input.
        raise InvalidInputError("Invalid integration request. Check field formats and limits.") from None


def service(request, db, user):
    return IntegrationService(db, user, request.app.state.cipher, audit_context(request, user))


@router.get("/catalog")
async def catalog(user: CurrentUser):
    return {"items": [{"id": key, **value} for key, value in CATALOG.items()],
            "whatsapp": {"route": "#/agent-studio", "transport": "WhatsApp Business Cloud API", "live_verification_required": True}}


@router.get("/tenants")
async def tenants(db: DbSession, user: CurrentUser):
    return {"items": await list_tenants(db, user)}


@router.get("/{tenant_id}/overview")
async def overview(tenant_id: UUID, request: Request, db: DbSession, user: CurrentUser):
    return await service(request, db, user).overview(tenant_id)


@router.post("/{tenant_id}/connectors", status_code=201)
async def create(tenant_id: UUID, request: Request, db: DbSession, user: CurrentUser):
    row, secret = await service(request, db, user).create(tenant_id, await parse(request, ConnectorInput))
    return {"connector": row, "webhook_secret": secret}


@router.patch("/{tenant_id}/connectors/{connector_id}")
async def patch(tenant_id: UUID, connector_id: UUID, request: Request, db: DbSession, user: CurrentUser):
    return await service(request, db, user).patch(tenant_id, connector_id, await parse(request, ConnectorPatch))


@router.post("/{tenant_id}/connectors/{connector_id}/revoke")
async def revoke(tenant_id: UUID, connector_id: UUID, request: Request, db: DbSession, user: CurrentUser):
    return await service(request, db, user).revoke(tenant_id, connector_id)


@router.post("/{tenant_id}/connectors/{connector_id}/jobs", status_code=201)
async def enqueue(tenant_id: UUID, connector_id: UUID, request: Request, db: DbSession, user: CurrentUser):
    return await service(request, db, user).enqueue(tenant_id, connector_id, await parse(request, JobInput))


@router.get("/{tenant_id}/jobs/{job_id}")
async def detail(tenant_id: UUID, job_id: UUID, request: Request, db: DbSession, user: CurrentUser):
    svc = service(request, db, user); job = await svc.job(tenant_id, job_id)
    svc.record("action_reviewed", job); await db.commit()
    return {**public_job(job), "payload": unseal(svc.cipher, "job", job, job.payload), "untrusted_content": True}


@router.post("/{tenant_id}/jobs/{job_id}/decision")
async def decision(tenant_id: UUID, job_id: UUID, request: Request, db: DbSession, user: CurrentUser):
    return await service(request, db, user).decision(tenant_id, job_id, await parse(request, Decision))


@router.post("/{tenant_id}/jobs/{job_id}/retry")
async def retry(tenant_id: UUID, job_id: UUID, request: Request, db: DbSession, user: CurrentUser):
    return await service(request, db, user).retry(tenant_id, job_id)


@router.post("/{tenant_id}/jobs/{job_id}/cancel")
async def cancel(tenant_id: UUID, job_id: UUID, request: Request, db: DbSession, user: CurrentUser):
    return await service(request, db, user).cancel(tenant_id, job_id)


@router.get("/{tenant_id}/connectors/{connector_id}/records")
async def records(tenant_id: UUID, connector_id: UUID, request: Request, db: DbSession, user: CurrentUser):
    svc = service(request, db, user); row = await svc.connector(tenant_id, connector_id)
    rows = (await db.scalars(select(IntegrationRecord).where(*scope(IntegrationRecord, tenant_id, user.id),
        IntegrationRecord.connector_id == connector_id).order_by(IntegrationRecord.updated_at.desc()).limit(100))).all()
    svc.record("records_read", row, count=len(rows)); await db.commit()
    return {"items": [{"id": r.id, "external_id": r.external_id, "version": r.version, "schema_version": r.schema_version,
                       "data": unseal(svc.cipher, "record", r, r.payload)} for r in rows], "untrusted_content": True, "limit": 100}


@router.get("/{tenant_id}/events/{event_id}")
async def event(tenant_id: UUID, event_id: UUID, request: Request, db: DbSession, user: CurrentUser):
    svc = service(request, db, user); await svc.authorize(tenant_id)
    row = await db.scalar(select(IntegrationEvent).where(*scope(IntegrationEvent, tenant_id, user.id), IntegrationEvent.id == event_id))
    if row is None: raise NotFoundError()
    svc.record("event_read", row); await db.commit()
    return {"id": row.id, "tenant_id": row.tenant_id, "owner_id": row.owner_id, "connector_id": row.connector_id,
            "target_module": row.target_module, "data": unseal(svc.cipher, "event", row, row.payload),
            "trust": "external_untrusted", "requires_consumer_authorization": True, "schema_version": 1}


@router.post("/webhooks/{connector_id}")
async def webhook(connector_id: UUID, request: Request, db: DbSession):
    return await accept_event(db, request.app.state.cipher, connector_id, await body(request, MAX_WEBHOOK),
                              request.headers.get("X-InfraSpine-Signature", ""))


@router.post("/{tenant_id}/connectors/{connector_id}/oauth/start")
async def oauth_start(tenant_id: UUID, connector_id: UUID, request: Request, db: DbSession, user: CurrentUser):
    return await oauth.begin(service(request, db, user), tenant_id, connector_id)


class OAuthCode(Input):
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=32, max_length=160)


@router.post("/{tenant_id}/connectors/{connector_id}/oauth/complete")
async def oauth_complete(tenant_id: UUID, connector_id: UUID, request: Request, db: DbSession, user: CurrentUser):
    data = await parse(request, OAuthCode)
    try:
        return await oauth.exchange(service(request, db, user), tenant_id, connector_id, data.state, data.code)
    except TransportError as exc:
        raise InvalidInputError("OAuth provider exchange failed: " + exc.code) from None
