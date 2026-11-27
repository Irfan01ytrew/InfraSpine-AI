"""Separate partner identity lifecycle and workspace-scoped, source-grounded assistance."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AccountLockedError, ConflictError, InvalidInputError, InvalidLoginError, NotFoundError
from app.core.security import (
    hash_password,
    hash_session_token,
    new_session_token,
    password_needs_rehash,
    verify_password,
)
from app.db.models import (
    PartnerIdentity,
    PartnerInvitation,
    PartnerKnowledgeSource,
    PartnerSession,
    PartnerWorkspace,
)
from app.rag.answer import NO_SOURCES_ANSWER, answer_question
from app.rag.sufficiency import redact_credentials
from app.schemas.partner_portal import (
    PartnerAskResponse,
    PartnerCitationOut,
    PartnerIdentityAdminOut,
    PartnerIdentityOut,
    PartnerKnowledgeSourceOut,
    PartnerWorkspaceOut,
)
from app.services import audit
from app.services.audit import AuditContext
from app.services.auth import normalize_email
from app.services.llm import LLMClient

INVITATION_HOURS = 72
PARTNER_SESSION_COOKIE_NAME = "infraspine_partner_session"


@dataclass(frozen=True)
class PartnerPrincipal:
    id: uuid.UUID
    workspace_id: uuid.UUID
    email: str
    workspace_slug: str
    workspace_name: str

    def response(self) -> PartnerIdentityOut:
        return PartnerIdentityOut(
            id=self.id, email=self.email, workspace_id=self.workspace_id,
            workspace_slug=self.workspace_slug, workspace_name=self.workspace_name,
        )


def _workspace_out(workspace: PartnerWorkspace) -> PartnerWorkspaceOut:
    return PartnerWorkspaceOut(
        id=workspace.id, slug=workspace.slug, name=workspace.name,
        status=workspace.status, created_at=workspace.created_at,
    )


def _invitation_out(invitation: PartnerInvitation, token: str | None = None) -> dict:
    return {
        "id": invitation.id,
        "workspace_id": invitation.workspace_id,
        "email": invitation.email,
        "created_at": invitation.created_at,
        "expires_at": invitation.expires_at,
        "accepted_at": invitation.accepted_at,
        "revoked_at": invitation.revoked_at,
        "token": token,
    }


def _source_out(source: PartnerKnowledgeSource) -> PartnerKnowledgeSourceOut:
    return PartnerKnowledgeSourceOut(
        id=source.id,
        workspace_id=source.workspace_id,
        title=source.title,
        content_sha256=source.content_sha256,
        status=source.status,
        created_at=source.created_at,
        archived_at=source.archived_at,
        source_format=source.source_format,
        index_status=source.index_status,
        index_attempts=source.index_attempts,
        index_error_code=source.index_error_code,
    )


def _identity_admin_out(identity: PartnerIdentity) -> PartnerIdentityAdminOut:
    return PartnerIdentityAdminOut(
        id=identity.id,
        workspace_id=identity.workspace_id,
        email=identity.email,
        is_active=identity.is_active,
        created_at=identity.created_at,
    )


class PartnerWorkspaceAdminService:
    """Internal-Admin management of isolated external workspaces and invitation records."""

    def __init__(self, db: AsyncSession, admin_id: uuid.UUID, ctx: AuditContext) -> None:
        self.db = db
        self.admin_id = admin_id
        self.ctx = ctx

    async def list_workspaces(self) -> list[PartnerWorkspaceOut]:
        rows = await self.db.scalars(select(PartnerWorkspace).order_by(PartnerWorkspace.slug))
        return [_workspace_out(row) for row in rows]

    async def create_workspace(self, slug: str, name: str) -> PartnerWorkspaceOut:
        normalized_slug = slug.strip().lower()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?", normalized_slug):
            raise InvalidInputError("Workspace key must use lowercase letters, numbers and interior hyphens.")
        workspace = PartnerWorkspace(
            slug=normalized_slug,
            name=name.strip(),
            created_by_user_id=self.admin_id,
        )
        self.db.add(workspace)
        audit.record(self.db, self.ctx, "partner.workspace_created", target=normalized_slug)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("A partner workspace with this key already exists.") from None
        await self.db.refresh(workspace)
        return _workspace_out(workspace)

    async def set_workspace_status(self, workspace_id: uuid.UUID, status: str) -> PartnerWorkspaceOut:
        workspace = await self.db.scalar(
            select(PartnerWorkspace).where(PartnerWorkspace.id == workspace_id).with_for_update()
        )
        if workspace is None:
            raise NotFoundError("Partner workspace not found.")
        if status not in {"active", "suspended"}:
            raise InvalidInputError("Partner workspace status must be active or suspended.")
        workspace.status = status
        if status == "suspended":
            now = datetime.now(UTC)
            await self.db.execute(
                update(PartnerSession)
                .where(
                    PartnerSession.identity_id.in_(
                        select(PartnerIdentity.id).where(PartnerIdentity.workspace_id == workspace_id)
                    ),
                    PartnerSession.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            await self.db.execute(
                update(PartnerInvitation)
                .where(
                    PartnerInvitation.workspace_id == workspace_id,
                    PartnerInvitation.accepted_at.is_(None),
                    PartnerInvitation.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
        audit.record(
            self.db, self.ctx, "partner.workspace_status_changed", target=str(workspace_id), details={"status": status}
        )
        await self.db.commit()
        await self.db.refresh(workspace)
        return _workspace_out(workspace)

    async def create_invitation(self, workspace_id: uuid.UUID, email: str) -> tuple[dict, str]:
        workspace = await self.db.scalar(
            select(PartnerWorkspace).where(PartnerWorkspace.id == workspace_id).with_for_update()
        )
        if workspace is None:
            raise NotFoundError("Partner workspace not found.")
        if workspace.status != "active":
            raise ConflictError("A suspended partner workspace cannot receive invitations.")
        normalized_email = normalize_email(email)
        if await self.db.scalar(
            select(PartnerIdentity.id).where(
                PartnerIdentity.workspace_id == workspace_id, PartnerIdentity.email == normalized_email
            )
        ) is not None:
            raise ConflictError("This email already has an identity in the partner workspace.")
        now = datetime.now(UTC)
        await self.db.execute(
            update(PartnerInvitation)
            .where(
                PartnerInvitation.workspace_id == workspace_id,
                PartnerInvitation.email == normalized_email,
                PartnerInvitation.accepted_at.is_(None),
                PartnerInvitation.revoked_at.is_(None),
                PartnerInvitation.expires_at <= now,
            )
            .values(revoked_at=now)
        )
        if await self.db.scalar(
            select(PartnerInvitation.id).where(
                PartnerInvitation.workspace_id == workspace_id,
                PartnerInvitation.email == normalized_email,
                PartnerInvitation.accepted_at.is_(None),
                PartnerInvitation.revoked_at.is_(None),
                PartnerInvitation.expires_at > now,
            )
        ) is not None:
            raise ConflictError("An active invitation already exists for this email in the workspace.")
        token = new_session_token()
        invitation = PartnerInvitation(
            workspace_id=workspace_id,
            email=normalized_email,
            token_hash=hash_session_token(token),
            created_by_user_id=self.admin_id,
            expires_at=now + timedelta(hours=INVITATION_HOURS),
        )
        self.db.add(invitation)
        audit.record(
            self.db,
            self.ctx,
            "partner.invitation_created",
            target=normalized_email,
            details={"workspace_id": str(workspace_id), "expires_in_hours": INVITATION_HOURS},
        )
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("An invitation could not be created for this partner identity.") from None
        await self.db.refresh(invitation)
        return _invitation_out(invitation, token), token

    async def list_invitations(self, workspace_id: uuid.UUID) -> list[dict]:
        if await self.db.scalar(select(PartnerWorkspace.id).where(PartnerWorkspace.id == workspace_id)) is None:
            raise NotFoundError("Partner workspace not found.")
        rows = await self.db.scalars(
            select(PartnerInvitation)
            .where(PartnerInvitation.workspace_id == workspace_id)
            .order_by(PartnerInvitation.created_at.desc())
        )
        return [_invitation_out(row) for row in rows]

    async def list_identities(self, workspace_id: uuid.UUID) -> list[PartnerIdentityAdminOut]:
        if await self.db.scalar(select(PartnerWorkspace.id).where(PartnerWorkspace.id == workspace_id)) is None:
            raise NotFoundError("Partner workspace not found.")
        rows = await self.db.scalars(
            select(PartnerIdentity)
            .where(PartnerIdentity.workspace_id == workspace_id)
            .order_by(PartnerIdentity.email)
        )
        return [_identity_admin_out(row) for row in rows]

    async def set_identity_active(
        self, workspace_id: uuid.UUID, identity_id: uuid.UUID, active: bool
    ) -> PartnerIdentityAdminOut:
        identity = await self.db.scalar(
            select(PartnerIdentity)
            .where(PartnerIdentity.id == identity_id, PartnerIdentity.workspace_id == workspace_id)
            .with_for_update()
        )
        if identity is None:
            raise NotFoundError("Partner identity not found.")
        if identity.is_active != active:
            identity.is_active = active
            if not active:
                await self.db.execute(
                    update(PartnerSession)
                    .where(PartnerSession.identity_id == identity_id, PartnerSession.revoked_at.is_(None))
                    .values(revoked_at=datetime.now(UTC))
                )
            audit.record(
                self.db,
                self.ctx,
                "partner.identity_status_changed",
                target=identity.email,
                details={"workspace_id": str(workspace_id), "active": active},
            )
            await self.db.commit()
            await self.db.refresh(identity)
        return _identity_admin_out(identity)

    async def revoke_invitation(self, workspace_id: uuid.UUID, invitation_id: uuid.UUID) -> None:
        invitation = await self.db.scalar(
            select(PartnerInvitation)
            .where(PartnerInvitation.id == invitation_id, PartnerInvitation.workspace_id == workspace_id)
            .with_for_update()
        )
        if invitation is None:
            raise NotFoundError("Partner invitation not found.")
        if invitation.accepted_at is not None:
            raise ConflictError("An accepted partner invitation cannot be revoked.")
        if invitation.revoked_at is None:
            invitation.revoked_at = datetime.now(UTC)
            audit.record(
                self.db, self.ctx, "partner.invitation_revoked", target=invitation.email,
                details={"workspace_id": str(workspace_id)},
            )
            await self.db.commit()

    async def create_source(
        self, workspace_id: uuid.UUID, title: str, content: str
    ) -> PartnerKnowledgeSourceOut:
        workspace = await self.db.scalar(select(PartnerWorkspace).where(PartnerWorkspace.id == workspace_id))
        if workspace is None:
            raise NotFoundError("Partner workspace not found.")
        if workspace.status != "active":
            raise ConflictError("A suspended partner workspace cannot be changed.")
        normalized_title = title.strip()
        normalized_content = content.strip()
        if not normalized_title or len(normalized_title) > 180 or not normalized_content or len(normalized_content) > 50_000:
            raise InvalidInputError("Partner source title or content is outside the allowed length.")
        source = PartnerKnowledgeSource(
            workspace_id=workspace_id,
            title=normalized_title,
            content=normalized_content,
            content_sha256=hashlib.sha256(normalized_content.encode("utf-8")).hexdigest(),
            created_by_user_id=self.admin_id,
        )
        self.db.add(source)
        audit.record(
            self.db,
            self.ctx,
            "partner.source_created",
            target=str(source.id),
            details={"workspace_id": str(workspace_id), "content_chars": len(normalized_content)},
        )
        await self.db.commit()
        await self.db.refresh(source)
        return _source_out(source)

    async def list_sources(self, workspace_id: uuid.UUID) -> list[PartnerKnowledgeSourceOut]:
        if await self.db.scalar(select(PartnerWorkspace.id).where(PartnerWorkspace.id == workspace_id)) is None:
            raise NotFoundError("Partner workspace not found.")
        rows = await self.db.scalars(
            select(PartnerKnowledgeSource)
            .where(PartnerKnowledgeSource.workspace_id == workspace_id)
            .order_by(PartnerKnowledgeSource.created_at.desc())
        )
        return [_source_out(row) for row in rows]

    async def create_document_source(self, workspace_id: uuid.UUID, title: str, source_format: str,
                                     storage_key: str, content_sha256: str) -> PartnerKnowledgeSourceOut:
        workspace = await self.db.scalar(select(PartnerWorkspace).where(PartnerWorkspace.id == workspace_id).with_for_update())
        if workspace is None:
            raise NotFoundError("Partner workspace not found.")
        if workspace.status != "active":
            raise ConflictError("A suspended partner workspace cannot be changed.")
        normalized_title = title.strip()
        if not normalized_title or len(normalized_title) > 180 or source_format not in {"pdf", "docx", "xlsx", "pptx"}:
            raise InvalidInputError("Partner document title or format is invalid.")
        source = PartnerKnowledgeSource(workspace_id=workspace_id, title=normalized_title, content="",
            content_sha256=content_sha256, source_format=source_format, storage_key=storage_key,
            created_by_user_id=self.admin_id)
        self.db.add(source)
        await self.db.flush()
        await self.db.refresh(source)
        result = _source_out(source)
        audit.record(self.db, self.ctx, "partner.document_uploaded", target=str(source.id),
                     details={"workspace_id": str(workspace_id), "format": source_format})
        await self.db.commit()
        return result

    async def retry_source(self, workspace_id: uuid.UUID, source_id: uuid.UUID) -> PartnerKnowledgeSourceOut:
        source = await self.db.scalar(select(PartnerKnowledgeSource).where(
            PartnerKnowledgeSource.id == source_id, PartnerKnowledgeSource.workspace_id == workspace_id,
        ).with_for_update())
        if source is None:
            raise NotFoundError("Partner source not found.")
        if source.status != "active" or source.index_status != "failed":
            raise ConflictError("Only an active source with failed processing can be retried.")
        source.index_status = "pending"
        source.index_attempts = 0
        source.index_error_code = None
        source.next_attempt_at = datetime.now(UTC)
        source.locked_until = None
        source.claim_token = None
        audit.record(self.db, self.ctx, "partner.source_retry_requested", target=str(source.id),
                     details={"workspace_id": str(workspace_id)})
        await self.db.commit()
        await self.db.refresh(source)
        return _source_out(source)

    async def archive_source(self, workspace_id: uuid.UUID, source_id: uuid.UUID) -> None:
        source = await self.db.scalar(
            select(PartnerKnowledgeSource)
            .where(
                PartnerKnowledgeSource.id == source_id,
                PartnerKnowledgeSource.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if source is None:
            raise NotFoundError("Partner source not found.")
        if source.status != "archived":
            source.status = "archived"
            source.archived_at = datetime.now(UTC)
            source.claim_token = None
            source.locked_until = None
            audit.record(
                self.db,
                self.ctx,
                "partner.source_archived",
                target=str(source_id),
                details={"workspace_id": str(workspace_id)},
            )
            await self.db.commit()


async def accept_invitation(db: AsyncSession, token: str, password: str, ctx: AuditContext) -> PartnerPrincipal:
    invitation = await db.scalar(
        select(PartnerInvitation).where(PartnerInvitation.token_hash == hash_session_token(token)).with_for_update()
    )
    now = datetime.now(UTC)
    if (
        invitation is None
        or invitation.accepted_at is not None
        or invitation.revoked_at is not None
        or invitation.expires_at <= now
    ):
        raise InvalidLoginError("This partner invitation is invalid or expired.")
    workspace = await db.scalar(
        select(PartnerWorkspace).where(PartnerWorkspace.id == invitation.workspace_id).with_for_update()
    )
    if workspace is None or workspace.status != "active":
        raise InvalidLoginError("This partner invitation is invalid or expired.")
    if await db.scalar(
        select(PartnerIdentity.id).where(
            PartnerIdentity.workspace_id == workspace.id, PartnerIdentity.email == invitation.email
        )
    ) is not None:
        raise ConflictError("This email already has an identity in the partner workspace.")
    identity = PartnerIdentity(
        workspace_id=workspace.id,
        email=invitation.email,
        password_hash=hash_password(password),
    )
    invitation.accepted_at = now
    db.add(identity)
    audit.record(
        db,
        AuditContext(None, invitation.email, ctx.ip_address, ctx.request_id),
        "partner.invitation_accepted",
        target=invitation.email,
        details={"workspace_id": str(workspace.id)},
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("This partner invitation was already used.") from None
    await db.refresh(identity)
    return PartnerPrincipal(identity.id, workspace.id, identity.email, workspace.slug, workspace.name)


async def login(
    db: AsyncSession, settings: Settings, email: str, workspace_slug: str, password: str, ctx: AuditContext
) -> tuple[PartnerPrincipal, str]:
    normalized_email = normalize_email(email)
    row = (
        await db.execute(
            select(PartnerIdentity, PartnerWorkspace)
            .join(PartnerWorkspace, PartnerWorkspace.id == PartnerIdentity.workspace_id)
            .where(PartnerIdentity.email == normalized_email, PartnerWorkspace.slug == workspace_slug)
            .with_for_update(of=PartnerIdentity)
        )
    ).first()
    identity, workspace = row if row is not None else (None, None)
    password_ok = verify_password(identity.password_hash if identity else None, password)
    failure_reason = None
    now = datetime.now(UTC)
    if identity is None or workspace is None:
        failure_reason = "unknown_partner"
    elif not identity.is_active or workspace.status != "active":
        failure_reason = "inactive_partner"
    elif identity.locked_until is not None and identity.locked_until > now:
        failure_reason = "locked"
    elif not password_ok:
        failure_reason = "bad_password"
        identity.failed_login_attempts += 1
        if identity.failed_login_attempts >= settings.login_max_failed_attempts:
            identity.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
            identity.failed_login_attempts = 0
            failure_reason = "bad_password_locked"

    if failure_reason is not None:
        audit.record(
            db,
            AuditContext(None, normalized_email, ctx.ip_address, ctx.request_id),
            "partner.login",
            outcome="failure",
            target=normalized_email,
            details={"reason": failure_reason, "workspace_id": str(workspace.id) if workspace else None},
        )
        await db.commit()
        if failure_reason in {"locked", "bad_password_locked"}:
            raise AccountLockedError()
        raise InvalidLoginError()

    assert identity is not None and workspace is not None
    identity.failed_login_attempts = 0
    identity.locked_until = None
    if password_needs_rehash(identity.password_hash):
        identity.password_hash = hash_password(password)
    await db.execute(
        delete(PartnerSession).where(PartnerSession.identity_id == identity.id, PartnerSession.expires_at < now)
    )
    token = new_session_token()
    db.add(
        PartnerSession(
            identity_id=identity.id,
            token_hash=hash_session_token(token),
            expires_at=now + timedelta(minutes=settings.session_ttl_minutes),
        )
    )
    audit.record(
        db,
        AuditContext(None, normalized_email, ctx.ip_address, ctx.request_id),
        "partner.login",
        target=normalized_email,
        details={"workspace_id": str(workspace.id)},
    )
    await db.commit()
    return PartnerPrincipal(identity.id, workspace.id, identity.email, workspace.slug, workspace.name), token


async def resolve_session(db: AsyncSession, token: str) -> PartnerPrincipal | None:
    now = datetime.now(UTC)
    row = (
        await db.execute(
            select(PartnerIdentity, PartnerWorkspace)
            .join(PartnerSession, PartnerSession.identity_id == PartnerIdentity.id)
            .join(PartnerWorkspace, PartnerWorkspace.id == PartnerIdentity.workspace_id)
            .where(
                PartnerSession.token_hash == hash_session_token(token),
                PartnerSession.revoked_at.is_(None),
                PartnerSession.expires_at > now,
                PartnerIdentity.is_active.is_(True),
                PartnerWorkspace.status == "active",
            )
        )
    ).first()
    if row is None:
        return None
    identity, workspace = row
    return PartnerPrincipal(identity.id, workspace.id, identity.email, workspace.slug, workspace.name)


async def logout(db: AsyncSession, token: str, ctx: AuditContext) -> None:
    session = await db.scalar(
        select(PartnerSession).where(
            PartnerSession.token_hash == hash_session_token(token), PartnerSession.revoked_at.is_(None)
        )
    )
    if session is None:
        return
    session.revoked_at = datetime.now(UTC)
    identity = await db.scalar(select(PartnerIdentity).where(PartnerIdentity.id == session.identity_id))
    audit.record(
        db,
        AuditContext(None, identity.email if identity else None, ctx.ip_address, ctx.request_id),
        "partner.logout",
        details={"workspace_id": str(identity.workspace_id)} if identity else {},
    )
    await db.commit()


async def answer_partner_question(
    db: AsyncSession,
    principal: PartnerPrincipal,
    question: str,
    get_llm,
    indexer=None,
    embedder=None,
    min_similarity: float = 0.2,
) -> PartnerAskResponse:
    """Retrieve only the authenticated workspace's passages and validate grounded answers."""
    from app.services.partner_retrieval import retrieve_partner_chunks

    chunks = await retrieve_partner_chunks(db, principal.workspace_id, question, indexer, embedder, min_similarity)
    llm: LLMClient | None = await get_llm() if chunks else None
    answer = await answer_question(
        llm,
        question,
        chunks,
        document_hint=True,
        system_prompt_addendum=(
            "You are answering an external customer or partner. Use only the partner workspace source blocks; "
            "do not disclose internal company information or infer access to any employee document library."
        ),
    )
    source_formats = {}
    if chunks:
        source_ids = {chunk.document_id for chunk in chunks}
        readable_rows = (await db.execute(select(PartnerKnowledgeSource.id, PartnerKnowledgeSource.source_format).where(
            PartnerKnowledgeSource.id.in_(source_ids), PartnerKnowledgeSource.workspace_id == principal.workspace_id,
            PartnerKnowledgeSource.status == "active",
            or_(PartnerKnowledgeSource.source_format == "text", PartnerKnowledgeSource.index_status == "ready"),
        ))).all()
        source_formats = {source_id: source_format for source_id, source_format in readable_rows}
        if set(source_formats) != source_ids:
            return PartnerAskResponse(answer=NO_SOURCES_ANSWER, insufficient_evidence=True, citations=[], retrieved_sources=0)
    return PartnerAskResponse(
        answer=answer.answer,
        insufficient_evidence=answer.insufficient_evidence,
        citations=[
            PartnerCitationOut(
                source_id=f"S{number}",
                source_record_id=source.document_id,
                title=source.filename,
                excerpt=redact_credentials(source.content[:320], []),
                location_label=source.location_label,
                download_available=source_formats.get(source.document_id) in {"pdf", "docx", "xlsx", "pptx"},
            )
            for number, source in answer.cited
        ],
        retrieved_sources=len(chunks),
    )
