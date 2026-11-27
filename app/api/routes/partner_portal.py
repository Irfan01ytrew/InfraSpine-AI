"""External partner sign-in and workspace-scoped, cited assistance."""

import asyncio
import hashlib
import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentPartner, DbSession, audit_context, get_llm_runtime, verify_csrf
from app.core.errors import AuthenticationRequiredError, ConflictError, NotFoundError
from app.core.security import PARTNER_SESSION_COOKIE_NAME
from app.db.models import PartnerKnowledgeSource
from app.rag.storage import DocumentStorage, UnsafeFileError
from app.schemas.partner_portal import (
    PartnerAskRequest,
    PartnerAskResponse,
    PartnerIdentityOut,
    PartnerInviteAccept,
    PartnerLoginRequest,
)
from app.services import audit
from app.services.audit import AuditContext
from app.services.partner_portal import accept_invitation, answer_partner_question, login, logout, resolve_session

router = APIRouter(prefix="/api/partner", tags=["partner-portal"])
Partner = CurrentPartner


@router.post("/auth/accept", response_model=PartnerIdentityOut, dependencies=[Depends(verify_csrf)])
async def accept(payload: PartnerInviteAccept, request: Request, db: DbSession) -> PartnerIdentityOut:
    principal = await accept_invitation(
        db,
        payload.token.get_secret_value(),
        payload.password.get_secret_value(),
        audit_context(request, None),
    )
    return principal.response()


@router.post("/auth/login", response_model=PartnerIdentityOut, dependencies=[Depends(verify_csrf)])
async def sign_in(
    payload: PartnerLoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> PartnerIdentityOut:
    principal, token = await login(
        db,
        settings,
        payload.email,
        payload.workspace_slug,
        payload.password.get_secret_value(),
        audit_context(request, None),
    )
    response.set_cookie(
        PARTNER_SESSION_COOKIE_NAME,
        token,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/api/partner",
    )
    response.headers["Cache-Control"] = "no-store"
    return principal.response()


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def sign_out(
    request: Request, response: Response, db: DbSession, settings: AppSettings, principal: Partner
) -> Response:
    token = request.cookies.get(PARTNER_SESSION_COOKIE_NAME, "")
    await logout(db, token, audit_context(request, None))
    response.delete_cookie(
        PARTNER_SESSION_COOKIE_NAME,
        path="/api/partner",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/auth/me", response_model=PartnerIdentityOut)
async def me(principal: Partner) -> PartnerIdentityOut:
    return principal.response()


@router.get("/sources/{source_id}/file")
async def original_source(source_id: uuid.UUID, request: Request, db: DbSession,
                          principal: Partner, settings: AppSettings) -> Response:
    source = await db.scalar(select(PartnerKnowledgeSource).where(
        PartnerKnowledgeSource.id == source_id, PartnerKnowledgeSource.workspace_id == principal.workspace_id,
        PartnerKnowledgeSource.status == "active", PartnerKnowledgeSource.index_status == "ready",
        PartnerKnowledgeSource.source_format.in_(("pdf", "docx", "xlsx", "pptx")),
    ))
    if source is None or not source.storage_key:
        raise NotFoundError("Partner source not found.")
    storage = DocumentStorage(settings.rag_data_dir.resolve() / "partner_uploads")
    try:
        path = storage.path_for(source.storage_key)
        if not path.is_file() or path.stat().st_size > settings.rag_max_upload_bytes:
            raise NotFoundError("Partner source file is unavailable.")
        payload = await asyncio.to_thread(path.read_bytes)
    except (OSError, UnsafeFileError):
        raise NotFoundError("Partner source file is unavailable.") from None
    if len(payload) > settings.rag_max_upload_bytes or hashlib.sha256(payload).hexdigest() != source.content_sha256:
        raise ConflictError("The original source no longer matches its indexed content.")
    readable = await db.scalar(select(PartnerKnowledgeSource.id).where(
        PartnerKnowledgeSource.id == source_id, PartnerKnowledgeSource.workspace_id == principal.workspace_id,
        PartnerKnowledgeSource.status == "active", PartnerKnowledgeSource.index_status == "ready",
    ))
    current = await resolve_session(db, request.cookies.get(PARTNER_SESSION_COOKIE_NAME, ""))
    if current is None or current.id != principal.id or current.workspace_id != principal.workspace_id:
        raise AuthenticationRequiredError()
    if readable is None:
        raise NotFoundError("Partner source not found.")
    audit.record(db, AuditContext(None, principal.email, request.client.host if request.client else None,
                                  getattr(request.state, "request_id", None)),
                 "partner.source_downloaded", target=str(source.id), details={"workspace_id": str(principal.workspace_id)})
    await db.commit()
    return Response(content=payload, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="partner-source-{source.id}.{source.source_format}"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })


@router.post("/assistant/ask", response_model=PartnerAskResponse, dependencies=[Depends(verify_csrf)])
async def ask(
    payload: PartnerAskRequest,
    request: Request,
    response: Response,
    db: DbSession,
    principal: Partner,
    settings: AppSettings,
) -> PartnerAskResponse:
    runtime = get_llm_runtime(request)

    async def get_llm():
        return await runtime.get_client(db)

    result = await answer_partner_question(db, principal, payload.question, get_llm,
        request.app.state.rag.partner_indexer, request.app.state.rag.embedder, settings.rag_min_similarity)
    current = await resolve_session(db, request.cookies.get(PARTNER_SESSION_COOKIE_NAME, ""))
    if current is None or current.id != principal.id or current.workspace_id != principal.workspace_id:
        raise AuthenticationRequiredError()
    audit.record(
        db,
        AuditContext(
            None,
            principal.email,
            request.client.host if request.client else None,
            getattr(request.state, "request_id", None),
        ),
        "partner.assistant_query",
        target=str(principal.workspace_id),
        details={"identity_id": str(principal.id), "question_chars": len(payload.question),
                 "retrieved_sources": result.retrieved_sources, "insufficient_evidence": result.insufficient_evidence},
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return result
