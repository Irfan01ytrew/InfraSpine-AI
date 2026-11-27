"""Source-quoted obligation tracking with live document ACL and version checks."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import (
    DOC_READY,
    Document,
    DocumentChunk,
    DocumentObligation,
    DocumentObligationEvent,
    User,
)
from app.rag.access import readable_by
from app.rag.documents import DocumentService
from app.schemas.document_obligations import DocumentObligationCreate, DocumentObligationUpdate
from app.services import audit
from app.services.audit import AuditContext


def normalize_source_text(value: str) -> str:
    """Normalize whitespace only; an obligation must quote a real indexed source passage."""
    return " ".join(value.split())


def obligation_status_transition(current: str, requested: str) -> str:
    allowed = {
        "open": {"acknowledged", "completed"},
        "acknowledged": {"open", "completed"},
        "completed": {"open"},
    }
    if requested == current or requested not in allowed.get(current, set()):
        raise InvalidInputError("That obligation status transition is not allowed.")
    return requested


class DocumentObligationService:
    def __init__(self, db: AsyncSession, documents: DocumentService, user: User, ctx: AuditContext) -> None:
        self.db = db
        self.documents = documents
        self.user = user
        self.ctx = ctx

    async def list_for_document(
        self, document_id: uuid.UUID, *, source_version: int | None = None
    ) -> list[dict]:
        document = await self.documents.get(document_id)
        query = select(DocumentObligation).where(DocumentObligation.document_id == document.id)
        if source_version is not None:
            query = query.where(DocumentObligation.source_version == source_version)
        rows = list((await self.db.scalars(
            query
            .order_by(DocumentObligation.due_at.asc().nulls_last(), DocumentObligation.created_at.desc())
            .limit(100)
        )).all())
        visible = []
        for row in rows:
            if await self._can_manage(row, document) or row.assigned_to_user_id == self.user.id:
                visible.append(await self._out(row, document))
        return visible

    async def create(self, document_id: uuid.UUID, payload: DocumentObligationCreate) -> dict:
        document = await self.documents.get(document_id)
        if not await self.documents.can_modify(document):
            raise PermissionDeniedError("Only a document owner, administrator, or assigned department manager can add an obligation.")
        if document.status != DOC_READY or document.indexed_version != document.version:
            raise ConflictError("Wait until the current document version is indexed before recording an obligation.")
        chunks = list((await self.db.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document.id)
            .order_by(DocumentChunk.chunk_index)
        )).all())
        quote = normalize_source_text(payload.source_quote)
        matched = next((chunk for chunk in chunks if quote in normalize_source_text(chunk.content)), None)
        if matched is None:
            raise InvalidInputError("The quoted text must appear in a passage from the current document version.")

        assignee = self.user
        if payload.assigned_to_email:
            assignee = (await self.db.execute(
                select(User).where(User.email == payload.assigned_to_email, User.is_active.is_(True))
            )).scalar_one_or_none()
            if assignee is None:
                raise InvalidInputError("The assignee must be an active user who can read this document.")
        can_read = await self.db.scalar(
            select(Document.id).where(Document.id == document.id, readable_by(assignee)).limit(1)
        )
        if can_read is None:
            raise InvalidInputError("The assignee must be an active user who can read this document.")
        if payload.due_at is not None and payload.due_at.tzinfo is None:
            raise InvalidInputError("The due date must include a timezone.")

        row = DocumentObligation(
            document_id=document.id,
            source_version=document.version,
            source_sha256=document.sha256,
            source_quote=quote,
            source_page_number=matched.page_number,
            source_locator=matched.locator or {"kind": "page", "position": matched.page_number, "page": matched.page_number},
            title=payload.title,
            rationale=payload.rationale,
            assigned_to_user_id=assignee.id,
            created_by_user_id=self.user.id,
            due_at=payload.due_at,
        )
        self.db.add(row)
        await self.db.flush()
        self.db.add(DocumentObligationEvent(
            obligation_id=row.id,
            actor_user_id=self.user.id,
            action="created" if assignee.id == self.user.id else "assigned",
            rationale=payload.rationale,
            details={"source_version": document.version, "source_page_number": matched.page_number},
        ))
        audit.record(self.db, self.ctx, "document_obligation.created", target=str(row.id), details={
            "document_id": str(document.id), "source_version": document.version,
            "assigned_to_user_id": str(assignee.id),
        })
        await self.db.commit()
        return await self.get(row.id)

    async def get(self, obligation_id: uuid.UUID) -> dict:
        row = (await self.db.execute(
            select(DocumentObligation).where(DocumentObligation.id == obligation_id)
        )).scalar_one_or_none()
        if row is None:
            raise NotFoundError("Document obligation not found.")
        document = await self.documents.get(row.document_id)
        if not await self._can_manage(row, document) and row.assigned_to_user_id != self.user.id:
            raise NotFoundError("Document obligation not found.")
        return await self._out(row, document)

    async def update(self, obligation_id: uuid.UUID, payload: DocumentObligationUpdate) -> dict:
        row = (await self.db.execute(
            select(DocumentObligation).where(DocumentObligation.id == obligation_id).with_for_update()
        )).scalar_one_or_none()
        if row is None:
            raise NotFoundError("Document obligation not found.")
        document = await self.documents.get(row.document_id)
        can_manage = await self._can_manage(row, document)
        if not can_manage and row.assigned_to_user_id != self.user.id:
            raise NotFoundError("Document obligation not found.")
        if row.version != payload.expected_version:
            raise ConflictError("This obligation changed. Reload it before saving.")
        assigned = row.assigned_to_user_id == self.user.id
        changes = payload.model_fields_set - {"expected_version", "rationale"}
        if not changes:
            raise InvalidInputError("Choose a status, assignee, or due date change.")
        if not can_manage and not assigned:
            raise PermissionDeniedError("Only the assigned employee or a document manager can update this obligation.")
        if ("assigned_to_email" in changes or "due_at" in changes) and not can_manage:
            raise PermissionDeniedError("Only a document manager can change the assignee or due date.")
        if not payload.rationale:
            raise InvalidInputError("Add a short reason for this obligation update.")

        actions: list[tuple[str, dict]] = []
        from_status = row.status
        if "status" in changes and payload.status is not None:
            if row.source_version != document.version or row.source_sha256 != document.sha256:
                raise ConflictError("The cited document changed. Review the new version before changing obligation status.")
            row.status = obligation_status_transition(row.status, payload.status)
            row.completed_at = datetime.now(UTC) if row.status == "completed" else None
            action = (
                "completed" if row.status == "completed"
                else "acknowledged" if row.status == "acknowledged"
                else "reopened"
            )
            actions.append((action, {}))

        if "assigned_to_email" in changes:
            email = payload.assigned_to_email
            target = self.user if not email else (await self.db.execute(
                select(User).where(User.email == email, User.is_active.is_(True))
            )).scalar_one_or_none()
            if target is None:
                raise InvalidInputError("The assignee must be an active user who can read this document.")
            can_read = await self.db.scalar(
                select(Document.id).where(Document.id == document.id, readable_by(target)).limit(1)
            )
            if can_read is None:
                raise InvalidInputError("The assignee must be an active user who can read this document.")
            if target.id != row.assigned_to_user_id:
                previous = str(row.assigned_to_user_id)
                row.assigned_to_user_id = target.id
                actions.append(("reassigned", {"previous_assignee_user_id": previous, "assignee_user_id": str(target.id)}))

        if "due_at" in changes:
            due_at = payload.due_at
            if due_at is not None and due_at.tzinfo is None:
                raise InvalidInputError("The due date must include a timezone.")
            if due_at != row.due_at:
                row.due_at = due_at
                actions.append(("due_date_changed", {"due_at": due_at.isoformat() if due_at else None}))

        if not actions:
            raise InvalidInputError("The obligation already has those values.")
        row.version += 1
        row.updated_at = datetime.now(UTC)
        for action, details in actions:
            self.db.add(DocumentObligationEvent(
                obligation_id=row.id,
                actor_user_id=self.user.id,
                action=action,
                from_status=from_status if action in ("acknowledged", "completed", "open", "reopened") else None,
                to_status=row.status if action in ("acknowledged", "completed", "open", "reopened") else None,
                rationale=payload.rationale,
                details=details,
            ))
        audit.record(self.db, self.ctx, "document_obligation.updated", target=str(row.id), details={
            "actions": [action for action, _ in actions], "version": row.version,
        })
        await self.db.commit()
        return await self.get(row.id)

    async def _out(self, row: DocumentObligation, document: Document) -> dict:
        assignee = await self.db.get(User, row.assigned_to_user_id)
        history = list((await self.db.scalars(
            select(DocumentObligationEvent)
            .where(DocumentObligationEvent.obligation_id == row.id)
            .order_by(DocumentObligationEvent.id.desc())
            .limit(50)
        )).all())
        can_manage = await self._can_manage(row, document)
        return {
            "id": row.id,
            "document_id": row.document_id,
            "document_filename": document.filename,
            "source_version": row.source_version,
            "source_sha256": row.source_sha256,
            "source_quote": row.source_quote,
            "source_page_number": row.source_page_number,
            "source_locator": row.source_locator,
            "source_current": row.source_version == document.version and row.source_sha256 == document.sha256,
            "title": row.title,
            "rationale": row.rationale,
            "assigned_to_user_id": row.assigned_to_user_id,
            "assigned_to_email": assignee.email if assignee else "Unavailable",
            "created_by_user_id": row.created_by_user_id,
            "due_at": row.due_at,
            "status": row.status,
            "version": row.version,
            "completed_at": row.completed_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "can_manage": can_manage,
            "can_update_status": can_manage or row.assigned_to_user_id == self.user.id,
            "history": history,
        }

    async def _can_manage(self, row: DocumentObligation, document: Document) -> bool:
        return row.created_by_user_id == self.user.id or await self.documents.can_modify(document)
