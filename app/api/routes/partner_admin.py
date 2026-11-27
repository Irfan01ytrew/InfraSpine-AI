"""Administrator management for partner workspaces and their separate text corpus."""

import asyncio
import uuid
from pathlib import PurePath
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status

from app.api.deps import AdminUser, AppSettings, DbSession, audit_context, require_admin
from app.core.errors import ConflictError, FileRejectedError, FileTooLargeError, InvalidInputError, NotFoundError
from app.rag.parsers.base import ParseError
from app.rag.parsers.registry import inspect_document, limits_from_settings
from app.rag.storage import DocumentStorage, UnsafeFileError, sanitize_filename
from app.schemas.partner_portal import (
    PartnerIdentityActiveRequest,
    PartnerIdentityAdminOut,
    PartnerInviteCreate,
    PartnerInvitationOut,
    PartnerKnowledgeSourceCreate,
    PartnerKnowledgeSourceOut,
    PartnerWorkspaceCreate,
    PartnerWorkspaceOut,
    PartnerWorkspaceStatusRequest,
)
from app.services.partner_portal import PartnerWorkspaceAdminService

router = APIRouter(prefix="/api/admin/partner-workspaces", tags=["partner-workspaces"], dependencies=[Depends(require_admin)])
PartnerAdmin = AdminUser
_PARTNER_SOURCE_UPLOAD_LIMIT_BYTES = 200_000
_PARTNER_SOURCE_UPLOAD_EXTENSIONS = {".txt", ".md"}
_PARTNER_DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx"}


@router.get("", response_model=list[PartnerWorkspaceOut])
async def list_workspaces(db: DbSession, admin: PartnerAdmin, request: Request) -> list[PartnerWorkspaceOut]:
    return await PartnerWorkspaceAdminService(db, admin.id, audit_context(request, admin)).list_workspaces()


@router.post("", response_model=PartnerWorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: PartnerWorkspaceCreate, db: DbSession, admin: PartnerAdmin, request: Request
) -> PartnerWorkspaceOut:
    return await PartnerWorkspaceAdminService(db, admin.id, audit_context(request, admin)).create_workspace(
        payload.slug, payload.name
    )


@router.patch("/{workspace_id}/status", response_model=PartnerWorkspaceOut)
async def set_workspace_status(
    workspace_id: uuid.UUID,
    payload: PartnerWorkspaceStatusRequest,
    db: DbSession,
    admin: PartnerAdmin,
    request: Request,
) -> PartnerWorkspaceOut:
    return await PartnerWorkspaceAdminService(db, admin.id, audit_context(request, admin)).set_workspace_status(
        workspace_id, payload.status
    )


@router.post(
    "/{workspace_id}/invitations", response_model=PartnerInvitationOut, status_code=status.HTTP_201_CREATED
)
async def invite_partner(
    workspace_id: uuid.UUID,
    payload: PartnerInviteCreate,
    db: DbSession,
    admin: PartnerAdmin,
    request: Request,
) -> PartnerInvitationOut:
    invitation, _ = await PartnerWorkspaceAdminService(
        db, admin.id, audit_context(request, admin)
    ).create_invitation(workspace_id, payload.email)
    return PartnerInvitationOut(**invitation)


@router.get("/{workspace_id}/invitations", response_model=list[PartnerInvitationOut])
async def list_partner_invitations(
    workspace_id: uuid.UUID, db: DbSession, admin: PartnerAdmin, request: Request
) -> list[PartnerInvitationOut]:
    rows = await PartnerWorkspaceAdminService(
        db, admin.id, audit_context(request, admin)
    ).list_invitations(workspace_id)
    return [PartnerInvitationOut(**row) for row in rows]


@router.delete("/{workspace_id}/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_partner_invitation(
    workspace_id: uuid.UUID,
    invitation_id: uuid.UUID,
    db: DbSession,
    admin: PartnerAdmin,
    request: Request,
) -> Response:
    await PartnerWorkspaceAdminService(db, admin.id, audit_context(request, admin)).revoke_invitation(
        workspace_id, invitation_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{workspace_id}/identities", response_model=list[PartnerIdentityAdminOut])
async def list_partner_identities(
    workspace_id: uuid.UUID, db: DbSession, admin: PartnerAdmin, request: Request
) -> list[PartnerIdentityAdminOut]:
    return await PartnerWorkspaceAdminService(
        db, admin.id, audit_context(request, admin)
    ).list_identities(workspace_id)


@router.patch("/{workspace_id}/identities/{identity_id}/active", response_model=PartnerIdentityAdminOut)
async def set_partner_identity_active(
    workspace_id: uuid.UUID,
    identity_id: uuid.UUID,
    payload: PartnerIdentityActiveRequest,
    db: DbSession,
    admin: PartnerAdmin,
    request: Request,
) -> PartnerIdentityAdminOut:
    return await PartnerWorkspaceAdminService(
        db, admin.id, audit_context(request, admin)
    ).set_identity_active(workspace_id, identity_id, payload.active)


@router.get("/{workspace_id}/sources", response_model=list[PartnerKnowledgeSourceOut])
async def list_sources(
    workspace_id: uuid.UUID, db: DbSession, admin: PartnerAdmin, request: Request
) -> list[PartnerKnowledgeSourceOut]:
    return await PartnerWorkspaceAdminService(db, admin.id, audit_context(request, admin)).list_sources(workspace_id)


@router.post(
    "/{workspace_id}/sources", response_model=PartnerKnowledgeSourceOut, status_code=status.HTTP_201_CREATED
)
async def add_source(
    workspace_id: uuid.UUID,
    payload: PartnerKnowledgeSourceCreate,
    db: DbSession,
    admin: PartnerAdmin,
    request: Request,
) -> PartnerKnowledgeSourceOut:
    return await PartnerWorkspaceAdminService(db, admin.id, audit_context(request, admin)).create_source(
        workspace_id, payload.title, payload.content
    )


@router.post(
    "/{workspace_id}/sources/upload", response_model=PartnerKnowledgeSourceOut, status_code=status.HTTP_201_CREATED
)
async def upload_source_file(
    workspace_id: uuid.UUID,
    request: Request,
    db: DbSession,
    admin: PartnerAdmin,
    settings: AppSettings,
    file: Annotated[UploadFile, File(description="UTF-8 text, Markdown, PDF, Word, Excel or PowerPoint")],
    title: Annotated[str | None, Form(max_length=180)] = None,
) -> PartnerKnowledgeSourceOut:
    """Queue a private partner document, or import bounded UTF-8 text."""
    filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    suffix = PurePath(filename).suffix.casefold()
    if suffix in _PARTNER_DOCUMENT_EXTENSIONS:
        try:
            safe_name = sanitize_filename(filename, tuple(sorted(_PARTNER_DOCUMENT_EXTENSIONS)))
        except UnsafeFileError as exc:
            raise FileRejectedError(exc.message) from None
        storage = DocumentStorage(settings.rag_data_dir.resolve() / "partner_uploads")
        try:
            stored = await asyncio.to_thread(storage.save, file.file, settings.rag_max_upload_bytes, suffix)
        except UnsafeFileError as exc:
            if exc.code == "file_too_large":
                raise FileTooLargeError(exc.message) from None
            raise FileRejectedError(exc.message) from None
        try:
            inspection = await asyncio.to_thread(inspect_document, storage.path_for(stored.key), safe_name,
                                                 limits_from_settings(settings))
        except (ParseError, UnsafeFileError) as exc:
            await asyncio.to_thread(storage.delete, stored.key)
            raise FileRejectedError(exc.message) from None
        try:
            return await PartnerWorkspaceAdminService(db, admin.id, audit_context(request, admin)).create_document_source(
                workspace_id, (title or "").strip() or PurePath(safe_name).stem[:180], inspection.file_format,
                stored.key, stored.sha256,
            )
        except (ConflictError, InvalidInputError, NotFoundError):
            await asyncio.to_thread(storage.delete, stored.key)
            raise
    if suffix not in _PARTNER_SOURCE_UPLOAD_EXTENSIONS:
        raise InvalidInputError("Partner source files must use .txt, .md, .pdf, .docx, .xlsx or .pptx.")

    payload = await file.read(_PARTNER_SOURCE_UPLOAD_LIMIT_BYTES + 1)
    if len(payload) > _PARTNER_SOURCE_UPLOAD_LIMIT_BYTES:
        raise InvalidInputError("Partner source files are limited to 200 KB.")
    try:
        content = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InvalidInputError("Partner source files must contain UTF-8 text.") from None
    if "\x00" in content:
        raise InvalidInputError("Partner source files must contain plain text.")

    source_title = (title or "").strip() or PurePath(filename).stem
    return await PartnerWorkspaceAdminService(db, admin.id, audit_context(request, admin)).create_source(
        workspace_id, source_title, content
    )


@router.post("/{workspace_id}/sources/{source_id}/retry", response_model=PartnerKnowledgeSourceOut)
async def retry_source(workspace_id: uuid.UUID, source_id: uuid.UUID, db: DbSession,
                       admin: PartnerAdmin, request: Request) -> PartnerKnowledgeSourceOut:
    return await PartnerWorkspaceAdminService(db, admin.id, audit_context(request, admin)).retry_source(
        workspace_id, source_id
    )


@router.delete("/{workspace_id}/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_source(
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    db: DbSession,
    admin: PartnerAdmin,
    request: Request,
) -> Response:
    await PartnerWorkspaceAdminService(db, admin.id, audit_context(request, admin)).archive_source(
        workspace_id, source_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
