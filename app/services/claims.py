"""Unverified organizational claims (ADR-004 WP3).

* Claimant identity is always the signed-in user. The request body cannot name another person.
* Owner-only API: every query filters by the claimant; other users (administrators included) get 404.
* Separate from approved knowledge: claims are never embedded, never cited, never written to
  personal memory and never treated as verified facts. Review and publish remain a later workflow.
* PostgreSQL is the authority: claimant, statement, kind, category, scope and sources are immutable;
  the only status change is withdrawal; events are append-only and written by a trigger.
* Logs and audit entries carry IDs and codes only, never the statement.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.db.models import (
    CLAIM_KINDS,
    CLAIM_SOURCE_DOCUMENT,
    CLAIM_SOURCE_KINDS,
    CLAIM_SOURCE_NOTE,
    CLAIM_SOURCE_URL,
    CLAIM_SOURCES_MAX,
    CLAIM_STATEMENT_MAX,
    CLAIM_SUBMITTED,
    CLAIM_WITHDRAWN,
    KNOWLEDGE_CATEGORIES,
    AccessGroup,
    AccessGroupMember,
    Document,
    KnowledgeClaim,
    KnowledgeClaimEvent,
    User,
)
from app.rag import access
from app.services import audit
from app.services.audit import AuditContext

_MISSING_CLAIM = "Claim not found."


class ClaimsService:
    def __init__(self, db: AsyncSession, user: User, ctx: AuditContext) -> None:
        self.db = db
        self.user = user
        self.ctx = ctx

    async def list_own(self) -> list[KnowledgeClaim]:
        rows = await self.db.execute(
            select(KnowledgeClaim)
            .where(KnowledgeClaim.claimant_user_id == self.user.id)
            .order_by(KnowledgeClaim.created_at.desc())
        )
        return list(rows.scalars())

    async def get_own(self, claim_id: uuid.UUID) -> KnowledgeClaim:
        return await self._own(claim_id)

    async def events(self, claim_id: uuid.UUID) -> list[KnowledgeClaimEvent]:
        await self._own(claim_id)
        rows = await self.db.execute(
            select(KnowledgeClaimEvent)
            .where(KnowledgeClaimEvent.claim_id == claim_id)
            .order_by(KnowledgeClaimEvent.id)
        )
        return list(rows.scalars())

    async def submit(
        self,
        *,
        kind: str,
        statement: str,
        category: str,
        scope_group_id: uuid.UUID | None,
        sources: list[dict],
    ) -> KnowledgeClaim:
        if kind not in CLAIM_KINDS:
            raise InvalidInputError("kind must be 'proposal' or 'correction'.")
        if category not in KNOWLEDGE_CATEGORIES:
            raise InvalidInputError("category is not a recognised knowledge category.")
        text = clean_statement(statement)
        stored_sources = await self._resolve_sources(sources)
        if scope_group_id is not None:
            await self._usable_scope(scope_group_id)

        claim = KnowledgeClaim(
            claimant_user_id=self.user.id,
            kind=kind,
            statement=text,
            category=category,
            scope_group_id=scope_group_id,
            sources=stored_sources,
            status=CLAIM_SUBMITTED,
        )
        self.db.add(claim)
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raise InvalidInputError("The claim was rejected by an integrity rule.") from None
        audit.record(
            self.db, self.ctx, "claim.submitted", target=str(claim.id),
            details={"kind": kind, "category": category, "source_count": len(stored_sources)},
        )
        await self.db.commit()
        await self.db.refresh(claim)
        return claim

    async def withdraw(self, claim_id: uuid.UUID) -> KnowledgeClaim:
        claim = await self._own(claim_id, lock=True)
        if claim.status != CLAIM_SUBMITTED:
            raise ConflictError("This claim has already been withdrawn.")
        claim.status = CLAIM_WITHDRAWN
        claim.withdrawn_at = datetime.now(UTC)
        audit.record(self.db, self.ctx, "claim.withdrawn", target=str(claim.id), details={"kind": claim.kind})
        await self.db.commit()
        await self.db.refresh(claim)
        return claim

    async def _own(self, claim_id: uuid.UUID, *, lock: bool = False) -> KnowledgeClaim:
        query = select(KnowledgeClaim).where(
            KnowledgeClaim.id == claim_id, KnowledgeClaim.claimant_user_id == self.user.id
        )
        if lock:
            query = query.with_for_update()
        claim = (await self.db.execute(query)).scalar_one_or_none()
        if claim is None:
            raise NotFoundError(_MISSING_CLAIM)
        return claim

    async def _usable_scope(self, group_id: uuid.UUID) -> AccessGroup:
        group = await self.db.get(AccessGroup, group_id)
        if group is None:
            raise InvalidInputError("Unknown scope group.")
        if self.user.role == "admin":
            return group
        member = await self.db.get(AccessGroupMember, (group.id, self.user.id))
        if member is None:
            raise InvalidInputError("You can only scope a claim to a group you belong to.")
        return group

    async def _resolve_sources(self, sources: list[dict]) -> list[dict]:
        if len(sources) > CLAIM_SOURCES_MAX:
            raise InvalidInputError(f"A claim can list at most {CLAIM_SOURCES_MAX} sources.")
        resolved: list[dict] = []
        seen_documents: set[uuid.UUID] = set()
        for raw in sources:
            kind = raw.get("kind")
            if kind not in CLAIM_SOURCE_KINDS:
                raise InvalidInputError("Each source must be a document, URL or note.")
            if kind == CLAIM_SOURCE_DOCUMENT:
                document_id = raw["document_id"]
                if document_id in seen_documents:
                    raise InvalidInputError("The same document cannot be attached twice.")
                seen_documents.add(document_id)
                document = (
                    await self.db.execute(
                        select(Document).where(Document.id == document_id, access.readable_by(self.user))
                    )
                ).scalar_one_or_none()
                if document is None:
                    raise InvalidInputError("A source document must be one you can already read.")
                resolved.append(
                    {"kind": CLAIM_SOURCE_DOCUMENT, "document_id": str(document.id), "filename": document.filename}
                )
            elif kind == CLAIM_SOURCE_URL:
                resolved.append({"kind": CLAIM_SOURCE_URL, "url": normalize_source_url(raw["url"])})
            else:
                note = clean_statement(raw["note"])
                if not note or len(note) > 500:
                    raise InvalidInputError("A source note must be between 1 and 500 characters.")
                resolved.append({"kind": CLAIM_SOURCE_NOTE, "note": note})
        return resolved


def clean_statement(value: str) -> str:
    text = " ".join(value.split()).strip()
    if not text:
        raise InvalidInputError("Write the claim statement.")
    if len(text) > CLAIM_STATEMENT_MAX:
        raise InvalidInputError(f"Keep the statement under {CLAIM_STATEMENT_MAX} characters.")
    return text


def normalize_source_url(raw: str) -> str:
    value = raw.strip()
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        raise InvalidInputError("Enter a valid source URL.") from None
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(ch.isspace() or not ch.isprintable() for ch in value)
    ):
        raise InvalidInputError("Enter a source URL without credentials.")
    if len(value) > 1024:
        raise InvalidInputError("A source URL must be at most 1024 characters.")
    return value
