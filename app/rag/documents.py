"""Document lifecycle: upload, list, replace, reprocess, delete. Authorization is enforced here."""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi import UploadFile
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    FileRejectedError,
    FileTooLargeError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
from app.db.models import (
    DOC_FAILED,
    DOC_PENDING,
    DOC_PROCESSING,
    ROLE_ADMIN,
    ROLE_MANAGER,
    VISIBILITY_PRIVATE,
    VISIBILITY_RESTRICTED,
    VISIBILITY_SHARED,
    AccessGroup,
    AccessGroupMember,
    DepartmentManager,
    Document,
    DocumentChunk,
    DocumentVersion,
    ResourceAcl,
    User,
)
from app.rag import access
from app.platform.events import publish
from app.rag.ingestion import IndexSynchronizer
from app.rag.parsers.base import Inspection, ParseError
from app.rag.parsers.registry import SUPPORTED_EXTENSIONS, inspect_document, limits_from_settings
from app.rag.storage import DocumentStorage, StoredFile, UnsafeFileError, sanitize_filename
from app.services import audit
from app.services.audit import AuditContext
from app.services.auth import normalize_email

logger = logging.getLogger(__name__)



async def page_image_withheld(db: AsyncSession, document: Document, page_number: int) -> bool:
    """Whether a page must not be shown as an image because it may show a credential. Images cannot be redacted,
    so this fails closed: a PDF page is withheld when its text names a credential or holds a secret, and when its
    text is unknown (nothing was extracted from it). Office renderings do not map pages to sections, so all their
    pages are withheld when the document mentions a credential anywhere. An uploaded image is judged by its
    description, and withheld while it has none."""
    from app.rag.parsers.image_parser import IMAGE_FORMATS
    from app.rag.sufficiency import mentions_credentials

    if document.file_format == "pdf" and document.page_count is not None and page_number > document.page_count:
        return False  # no such page: the renderer reports it as not found
    query = select(DocumentChunk.content).where(DocumentChunk.document_id == document.id)
    if document.file_format == "pdf":
        query = query.where(DocumentChunk.page_number == page_number)
    texts = list((await db.execute(query)).scalars())
    if (document.file_format == "pdf" or document.file_format in IMAGE_FORMATS) and not any(t.strip() for t in texts):
        return True
    return any(mentions_credentials(text) for text in texts)


class DocumentService:
    def __init__(
        self,
        db: AsyncSession,
        settings: Settings,
        storage: DocumentStorage,
        indexer: IndexSynchronizer,
        user: User,
        ctx: AuditContext,
    ) -> None:
        self.db = db
        self.settings = settings
        self.storage = storage
        self.indexer = indexer
        self.user = user
        self.ctx = ctx

    # ----------------------------------------------------------------- reads

    async def list(self) -> list[Document]:
        result = await self.db.execute(
            select(Document).where(access.readable_by(self.user)).order_by(Document.created_at.desc())
        )
        return list(result.scalars())

    async def get(self, document_id: uuid.UUID, *, lock: bool = False) -> Document:
        """Readable document or 404 (never reveals whether an unreadable document exists)."""
        query = select(Document).where(Document.id == document_id, access.readable_by(self.user))
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        doc = (await self.db.execute(query)).scalar_one_or_none()
        if doc is None:
            raise NotFoundError("Document not found.")
        return doc

    async def _get_modifiable(self, document_id: uuid.UUID, *, lock: bool = False) -> Document:
        doc = await self.get(document_id, lock=lock)
        if not await self.can_modify(doc):
            raise PermissionDeniedError(
                "Only the owner (or an admin, for shared documents) can change this document. "
                "Synced documents change only through their source."
            )
        return doc

    async def can_modify(self, doc: Document) -> bool:
        if access.can_modify(self.user, doc):
            return True
        if self.user.role != ROLE_MANAGER or doc.visibility != VISIBILITY_RESTRICTED:
            return False
        return (
            await self.db.execute(
                select(ResourceAcl.id)
                .join(DepartmentManager, DepartmentManager.group_id == ResourceAcl.principal_group_id)
                .where(
                    ResourceAcl.document_id == doc.id,
                    ResourceAcl.principal_group_id.is_not(None),
                    DepartmentManager.manager_user_id == self.user.id,
                )
                .limit(1)
            )
        ).first() is not None

    # ------------------------------------------------------------- mutations

    async def upload(
        self, upload: UploadFile, visibility: str, department_id: uuid.UUID | None = None
    ) -> Document:
        if visibility not in (VISIBILITY_PRIVATE, VISIBILITY_SHARED, VISIBILITY_RESTRICTED):
            raise InvalidInputError("visibility must be 'private', 'shared' or 'restricted'.")
        scope_group: AccessGroup | None = None
        if visibility == VISIBILITY_RESTRICTED:
            if department_id is None:
                raise InvalidInputError("A department or workspace is required for a restricted document.")
            scope_group = await self._upload_scope_group(department_id)
            if getattr(scope_group, "lifecycle_status", "active") == "archived":
                raise ConflictError("Cannot upload to an archived workspace.")
            if self.user.role != ROLE_ADMIN:
                if scope_group.kind != "department":
                    raise PermissionDeniedError("Only administrators can upload directly to a client or project workspace.")
                member = await self.db.get(AccessGroupMember, (scope_group.id, self.user.id))
                manager = await self.db.get(DepartmentManager, (scope_group.id, self.user.id))
                if member is None and manager is None:
                    raise PermissionDeniedError("You can only upload to a department you belong to or manage.")
        elif department_id is not None:
            raise InvalidInputError("Choose department-restricted visibility when selecting a department.")
        if self.settings.rag_admin_only_uploads and self.user.role != ROLE_ADMIN:
            manager_assignment = (
                self.user.role == ROLE_MANAGER
                and scope_group is not None
                and scope_group.kind == "department"
                and await self.db.get(DepartmentManager, (scope_group.id, self.user.id)) is not None
            )
            if not manager_assignment:
                raise PermissionDeniedError("Only administrators or assigned department managers can add documents.")
        if visibility == VISIBILITY_SHARED and not access.can_publish(self.user, visibility):
            raise PermissionDeniedError("Only administrators can publish shared documents.")
        filename = self._filename(upload)
        stored, inspection = await self._store_validated(upload, filename)

        doc = Document(
            owner_id=self.user.id,
            visibility=visibility,
            filename=filename,
            file_format=inspection.file_format,
            storage_key=stored.key,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            page_count=inspection.unit_count,
            status=DOC_PENDING,
        )
        self.db.add(doc)
        try:
            await self.db.flush()
            if scope_group is not None:
                self.db.add(
                    ResourceAcl(
                        resource_type="document",
                        document_id=doc.id,
                        principal_group_id=scope_group.id,
                        origin="manual",
                        created_by=self.user.id,
                    )
                )
            audit.record(
                self.db, self.ctx, "document.uploaded", target=str(doc.id),
                details={
                    "filename": filename, "format": inspection.file_format, "size_bytes": stored.size_bytes,
                    "pages": inspection.unit_count, "visibility": visibility,
                    "department_id": str(scope_group.id) if scope_group and scope_group.kind == "department" else None,
                    "scope_group_id": str(scope_group.id) if scope_group else None,
                },
            )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            await asyncio.to_thread(self.storage.delete, stored.key)
            raise
        return doc

    async def replace(self, document_id: uuid.UUID, upload: UploadFile) -> Document:
        """Swap in a new file. The old indexed content stays searchable until the new one is ready."""
        await self._get_modifiable(document_id)
        filename = self._filename(upload)
        stored, inspection = await self._store_validated(upload, filename)
        try:
            doc = await self._get_modifiable(document_id, lock=True)
        except Exception:
            await asyncio.to_thread(self.storage.delete, stored.key)
            raise
        old_key = doc.storage_key
        self.db.add(
            DocumentVersion(
                document_id=doc.id,
                version=doc.version,
                filename=doc.filename,
                file_format=doc.file_format,
                storage_key=old_key,
                sha256=doc.sha256,
                size_bytes=doc.size_bytes,
                archived_by=self.user.id,
                reason="replacement",
            )
        )
        doc.filename = filename
        doc.file_format = inspection.file_format
        doc.storage_key = stored.key
        doc.sha256 = stored.sha256
        doc.size_bytes = stored.size_bytes
        doc.page_count = inspection.unit_count
        doc.version += 1  # any in-flight job for the old version discards its result
        doc.status = DOC_PENDING
        doc.attempts = 0
        doc.next_attempt_at = datetime.now(UTC)
        doc.locked_until = None
        doc.error_code = doc.error_message = None
        audit.record(
            self.db, self.ctx, "document.replaced", target=str(doc.id),
            details={
                "filename": filename, "format": inspection.file_format, "size_bytes": stored.size_bytes,
                "version": doc.version,
            },
        )
        await self.db.commit()
        await self.db.refresh(doc)  # load server-generated columns (updated_at) for the response
        return doc

    async def reprocess(self, document_id: uuid.UUID) -> Document:
        doc = await self._get_modifiable(document_id, lock=True)
        if doc.status in (DOC_PENDING, DOC_PROCESSING):
            raise ConflictError("This document is already queued for processing.")
        doc.status = DOC_PENDING
        doc.attempts = 0
        doc.next_attempt_at = datetime.now(UTC)
        doc.error_code = doc.error_message = None
        audit.record(self.db, self.ctx, "document.reprocess_requested", target=str(doc.id))
        await self.db.commit()
        await self.db.refresh(doc)
        return doc

    async def cancel(self, document_id: uuid.UUID) -> Document:
        """Stop a queued or running job. The worker's lease renewal sees the change and kills its parser;
        a result that still arrives is discarded (the job no longer matches). Reprocess starts again."""
        doc = await self._get_modifiable(document_id, lock=True)
        if doc.status not in (DOC_PENDING, DOC_PROCESSING):
            raise ConflictError("Only queued or processing documents can be cancelled.")
        message = "Processing was cancelled. Use Reprocess to try again."
        if doc.indexed_version is not None:
            message += " The previously indexed version is still searchable."
        was = doc.status
        doc.status = DOC_FAILED
        doc.error_code = "cancelled"
        doc.error_message = message
        doc.locked_until = None
        audit.record(self.db, self.ctx, "document.processing_cancelled", target=str(doc.id), details={"was": was})
        await self.db.commit()
        await self.db.refresh(doc)
        return doc


    async def _publish_access_event(self, event_type: str, document_id, **payload) -> None:
        from app.platform.side_effects import org_scope_id
        scope_id = await org_scope_id(self.db)
        await publish(self.db, event_type, scope_id, {"document_id": str(document_id), **payload}, uuid.uuid4())

    async def set_access(
        self, document_id: uuid.UUID, visibility: str, user_emails: Sequence[str], group_ids: Sequence[uuid.UUID]
    ) -> Document:
        """Change who can read an uploaded document. Narrowing applies on the next request of every user."""
        if visibility not in (VISIBILITY_PRIVATE, VISIBILITY_SHARED, VISIBILITY_RESTRICTED):
            raise InvalidInputError("visibility must be 'private', 'shared' or 'restricted'.")
        manager = self.user.role == ROLE_MANAGER
        if manager and visibility != VISIBILITY_RESTRICTED:
            raise PermissionDeniedError("Department managers can only share documents within a managed department.")
        if not access.can_publish(self.user, visibility) and not (
            manager and visibility == VISIBILITY_RESTRICTED and not user_emails and bool(group_ids)
        ):
            raise PermissionDeniedError("Only administrators or assigned department managers can share documents.")
        if visibility != VISIBILITY_RESTRICTED and (user_emails or group_ids):
            raise InvalidInputError("Users and groups can only be given for restricted documents.")
        doc = await self._get_modifiable(document_id, lock=True)

        user_ids: set[uuid.UUID] = set()
        if user_emails:
            emails = {normalize_email(e) for e in user_emails}
            rows = (await self.db.execute(select(User.id, User.email).where(User.email.in_(emails)))).all()
            if len(rows) != len(emails):
                raise InvalidInputError("One or more users do not exist.")
            user_ids = {r.id for r in rows}
        groups = set(group_ids)
        if groups:
            group_query = select(AccessGroup.id).where(AccessGroup.id.in_(groups))
            if manager:
                group_query = (
                    select(DepartmentManager.group_id)
                    .join(AccessGroup, AccessGroup.id == DepartmentManager.group_id)
                    .where(
                        DepartmentManager.manager_user_id == self.user.id,
                        DepartmentManager.group_id.in_(groups),
                        AccessGroup.kind == "department",
                    )
                )
            found = set((await self.db.execute(group_query)).scalars())
            if found != groups:
                raise PermissionDeniedError("You can only share a document within departments you manage.") if manager else InvalidInputError("One or more groups do not exist.")
        if visibility == VISIBILITY_RESTRICTED and not (user_ids or groups):
            raise InvalidInputError("A restricted document needs at least one user or group.")

        await self.db.execute(delete(ResourceAcl).where(ResourceAcl.document_id == doc.id))
        for principal in [{"principal_user_id": u} for u in user_ids] + [{"principal_group_id": g} for g in groups]:
            self.db.add(
                ResourceAcl(
                    resource_type="document", document_id=doc.id, origin="manual", created_by=self.user.id, **principal
                )
            )
        doc.visibility = visibility
        audit.record(
            self.db, self.ctx, "document.access_changed", target=str(doc.id),
            details={"visibility": visibility, "users": len(user_ids), "groups": len(groups)},
        )
        await self._publish_access_event(
            "document.acl_changed", doc.id, visibility=visibility, users=len(user_ids), groups=len(groups),
        )
        await self.db.commit()
        await self.db.refresh(doc)
        return doc

    async def set_freshness(
        self,
        document_id: uuid.UUID,
        **fields: datetime | uuid.UUID | None,
    ) -> Document:
        """Owner-set freshness metadata (Step AI slice 5).

        ``fields`` is any subset of ``review_date``, ``expires_at`` and
        ``superseded_by``; only passed fields are changed, and a passed
        ``None`` clears the field. ``superseded_by`` must point at another
        readable document (never the document itself). Flagging is advisory
        only — it never hides the document or changes what is cited.
        """
        unknown = set(fields) - {"review_date", "expires_at", "superseded_by"}
        if unknown:
            raise InvalidInputError(f"Unknown freshness field(s): {', '.join(sorted(unknown))}.")
        doc = await self._get_modifiable(document_id, lock=True)
        changes: dict[str, object] = {}
        if "review_date" in fields:
            review_date = fields["review_date"]
            assert review_date is None or isinstance(review_date, datetime)
            doc.review_date = review_date
            changes["review_date"] = review_date.isoformat() if review_date else None
        if "expires_at" in fields:
            expires_at = fields["expires_at"]
            assert expires_at is None or isinstance(expires_at, datetime)
            doc.expires_at = expires_at
            changes["expires_at"] = expires_at.isoformat() if expires_at else None
        if "superseded_by" in fields:
            superseded_by = fields["superseded_by"]
            if superseded_by is not None:
                assert isinstance(superseded_by, uuid.UUID)
                if superseded_by == doc.id:
                    raise InvalidInputError("A document cannot supersede itself.")
                await self.get(superseded_by)  # must exist and be readable
                doc.superseded_by = superseded_by
            else:
                doc.superseded_by = None
            changes["superseded_by"] = str(superseded_by) if superseded_by else None
        if not changes:
            return doc
        audit.record(
            self.db, self.ctx, "document.freshness_changed", target=str(doc.id),
            details=changes,
        )
        await self.db.commit()
        await self.db.refresh(doc)
        return doc

    async def _upload_scope_group(self, group_id: uuid.UUID) -> AccessGroup:
        group = (
            await self.db.execute(
                select(AccessGroup).where(
                    AccessGroup.id == group_id,
                    AccessGroup.kind.in_(("department", "project", "client", "custom")),
                    AccessGroup.source == "local",
                )
            )
        ).scalar_one_or_none()
        if group is None:
            raise InvalidInputError("Select an existing local department or workspace.")
        return group

    async def delete(self, document_id: uuid.UUID) -> None:
        doc = await self._get_modifiable(document_id, lock=True)
        chunk_ids = list(
            (await self.db.execute(select(DocumentChunk.id).where(DocumentChunk.document_id == doc.id))).scalars()
        )
        storage_key, filename = doc.storage_key, doc.filename
        await self.db.delete(doc)  # chunks are removed by ON DELETE CASCADE
        audit.record(
            self.db, self.ctx, "document.deleted", target=str(document_id),
            details={"filename": filename, "chunks": len(chunk_ids)},
        )
        await self._publish_access_event("document.deleted", document_id, chunks=len(chunk_ids))
        await self.db.commit()
        # The database is authoritative: retrieval can no longer return these chunks even if the
        # index update below fails (the worker then reconciles the index).
        try:
            await self.indexer.remove(chunk_ids)
        except Exception as exc:
            logger.error("Vector removal failed for deleted document %s (%s)", document_id, type(exc).__name__)
        await asyncio.to_thread(self.storage.delete, storage_key)

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _filename(upload: UploadFile) -> str:
        try:
            return sanitize_filename(upload.filename, SUPPORTED_EXTENSIONS)
        except UnsafeFileError as exc:
            raise FileRejectedError(exc.message) from None

    async def _store_validated(self, upload: UploadFile, filename: str) -> tuple[StoredFile, Inspection]:
        """Store the upload, then require its content to be a supported format matching its extension."""
        extension = filename[filename.rfind("."):].lower()
        try:
            stored = await asyncio.to_thread(
                self.storage.save, upload.file, self.settings.rag_max_upload_bytes, extension
            )
        except UnsafeFileError as exc:
            if exc.code == "file_too_large":
                raise FileTooLargeError(exc.message) from None
            raise FileRejectedError(exc.message, details={"reason": exc.code}) from None
        try:
            inspection = await asyncio.to_thread(
                inspect_document, self.storage.path_for(stored.key), filename, limits_from_settings(self.settings)
            )
        except ParseError as exc:
            await asyncio.to_thread(self.storage.delete, stored.key)
            raise FileRejectedError(exc.message, details={"reason": exc.code}) from None
        except Exception:  # a parser bug must not leave the stored file behind
            await asyncio.to_thread(self.storage.delete, stored.key)
            raise
        return stored, inspection
