"""Administrator API for custom request forms."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.deps import AdminUser, DbSession, audit_context, require_admin
from app.schemas.request_forms import RequestFormIn, RequestFormListResponse, form_out
from app.services.request_forms import RequestFormService, audience_names

router = APIRouter(prefix="/api/admin/request-forms", tags=["request-forms"], dependencies=[Depends(require_admin)])


def _service(db: DbSession, admin: AdminUser, request: Request) -> RequestFormService:
    return RequestFormService(db, admin, audit_context(request, admin))


Forms = Annotated[RequestFormService, Depends(_service)]


async def _out(service: RequestFormService) -> RequestFormListResponse:
    rows = await service.list()
    names = await audience_names(service.db, [form for form, _ in rows])
    return RequestFormListResponse(forms=[form_out(form, name, names) for form, name in rows])


@router.get("", response_model=RequestFormListResponse)
async def list_forms(service: Forms) -> RequestFormListResponse:
    return await _out(service)


@router.post("", response_model=RequestFormListResponse, status_code=201)
async def create_form(payload: RequestFormIn, service: Forms) -> RequestFormListResponse:
    await service.save(None, **payload.as_kwargs())
    return await _out(service)


@router.put("/{form_id}", response_model=RequestFormListResponse)
async def update_form(form_id: uuid.UUID, payload: RequestFormIn, service: Forms) -> RequestFormListResponse:
    """Changing the fields or category starts a new version; submitted requests keep their own answers."""
    await service.save(form_id, **payload.as_kwargs())
    return await _out(service)


@router.delete("/{form_id}", response_model=RequestFormListResponse)
async def archive_form(form_id: uuid.UUID, service: Forms) -> RequestFormListResponse:
    await service.archive(form_id)
    return await _out(service)
