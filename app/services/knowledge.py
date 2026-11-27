"""Versioned organizational knowledge with source-bound, two-person publication.

Entries retain their original contributor and a separately assigned current steward. Revisions are
immutable: a correction is a new draft attributed to its actual editor. Evidence quotes a passage of a document revision the author can read,
and the stored visibility is never wider than those sources. Private drafts stay personal. Scoped
revisions require separate review and publish decisions; approved statements are searchable only
while every cited source remains current and readable to the requesting user.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import numpy as np
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import (
    KNOWLEDGE_CATEGORIES,
    KNOWLEDGE_DRAFT,
    KNOWLEDGE_PRIVATE,
    KNOWLEDGE_RESTRICTED,
    KNOWLEDGE_SHARED,
    AccessGroup,
    AccessGroupMember,
    DepartmentManager,
    Document,
    DocumentChunk,
    KnowledgeEntry,
    KnowledgeConflict,
    KnowledgeEvidence,
    KnowledgeRevision,
    User,
)
from app.rag import access
from app.rag.embeddings import Embedder
from app.rag.conflict_detection import conflict_search_query, potential_conflict
from app.services import audit
from app.services.audit import AuditContext

_CONTEXT = 80
_MISSING = "Knowledge entry not found."
_MAX_CONFLICT_CANDIDATES = 200
logger = logging.getLogger(__name__)


class KnowledgeService:
    def __init__(self, db: AsyncSession, user: User, ctx: AuditContext, embedder: Embedder | None = None) -> None:
        self.db = db
        self.user = user
        self.ctx = ctx
        self.embedder = embedder

    async def list_own(self) -> list[tuple[KnowledgeEntry, KnowledgeRevision]]:
        rows = await self.db.execute(
            select(KnowledgeEntry, KnowledgeRevision)
            .join(KnowledgeRevision, KnowledgeRevision.id == KnowledgeEntry.current_revision_id)
            .where(KnowledgeEntry.owner_user_id == self.user.id)
            .order_by(KnowledgeEntry.created_at.desc())
        )
        return list(rows.all())

    async def get_own(self, entry_id: uuid.UUID) -> tuple[KnowledgeEntry, list[KnowledgeRevision], list[KnowledgeEvidence]]:
        entry = await self._own(entry_id)
        revisions = list(
            (
                await self.db.execute(
                    select(KnowledgeRevision)
                    .where(KnowledgeRevision.entry_id == entry.id)
                    .order_by(KnowledgeRevision.revision_number)
                )
            ).scalars()
        )
        evidence = list(
            (
                await self.db.execute(
                    select(KnowledgeEvidence)
                    .where(KnowledgeEvidence.revision_id.in_([r.id for r in revisions]))
                    .order_by(KnowledgeEvidence.id)
                )
            ).scalars()
        ) if revisions else []
        return entry, revisions, evidence

    async def create(
        self,
        *,
        statement: str,
        category: str,
        visibility: str,
        scope_group_id: uuid.UUID | None,
        passages: list[tuple[uuid.UUID, str]],
    ) -> KnowledgeEntry:
        text = _statement(statement)
        self._visibility(visibility, scope_group_id)
        if scope_group_id is not None:
            await self._scope(scope_group_id)
        entry = KnowledgeEntry(author_user_id=self.user.id, owner_user_id=self.user.id)
        self.db.add(entry)
        try:
            await self.db.flush()
            revision = self._revision(entry, 1, text, category, visibility, scope_group_id)
            self.db.add(revision)
            await self.db.flush()
            await self._attach(revision, passages)
            audit.record(
                self.db, self.ctx, "knowledge.draft_created", target=str(entry.id),
                details={"category": category, "visibility": visibility, "evidence": len(passages), "revision": 1},
            )
            await self.db.commit()
        except (IntegrityError, DBAPIError) as exc:
            await self.db.rollback()
            raise _map(exc) from None
        await self.db.refresh(entry)
        return entry

    async def add_revision(
        self,
        entry_id: uuid.UUID,
        *,
        statement: str,
        category: str,
        visibility: str,
        scope_group_id: uuid.UUID | None,
        passages: list[tuple[uuid.UUID, str]],
    ) -> KnowledgeRevision:
        text = _statement(statement)
        self._visibility(visibility, scope_group_id)
        if scope_group_id is not None:
            await self._scope(scope_group_id)
        entry = await self._own(entry_id, lock=True)
        current = await self._current(entry, lock=True)
        if current.status not in (KNOWLEDGE_DRAFT, "published", "withdrawn", "superseded"):
            raise ConflictError("Only a draft can be replaced with a new revision.")
        if current.status == KNOWLEDGE_DRAFT:
            current.status = "superseded"
        try:
            await self.db.flush()
            revision = self._revision(
                entry, current.revision_number + 1, text, category, visibility, scope_group_id
            )
            self.db.add(revision)
            await self.db.flush()
            await self._attach(revision, passages)
            audit.record(
                self.db, self.ctx, "knowledge.revision_created", target=str(entry.id),
                details={"revision": revision.revision_number, "category": category, "evidence": len(passages)},
            )
            await self.db.commit()
        except (IntegrityError, DBAPIError) as exc:
            await self.db.rollback()
            raise _map(exc) from None
        await self.db.refresh(revision)
        return revision

    async def withdraw(self, entry_id: uuid.UUID) -> KnowledgeRevision:
        entry = await self._own(entry_id, lock=True)
        current = await self._current(entry, lock=True)
        if current.status not in (KNOWLEDGE_DRAFT, "submitted", "published"):
            raise ConflictError("This draft has already been withdrawn or superseded.")
        current.status = "withdrawn"
        audit.record(self.db, self.ctx, "knowledge.withdrawn", target=str(entry.id), details={"revision": current.revision_number})
        try:
            await self.db.commit()
        except (IntegrityError, DBAPIError) as exc:
            await self.db.rollback()
            raise _map(exc) from None
        await self.db.refresh(current)
        return current

    async def submit(self, entry_id: uuid.UUID) -> KnowledgeRevision:
        """Send department or shared knowledge through its scoped two-person approval workflow."""
        entry = await self._own(entry_id, lock=True)
        current = await self._current(entry, lock=True)
        if current.status != KNOWLEDGE_DRAFT:
            raise ConflictError("Only a draft can be submitted for review.")
        if current.visibility == KNOWLEDGE_PRIVATE:
            raise InvalidInputError("Private drafts stay personal; choose a department or shared scope before review.")
        current.status = "submitted"
        conflict_count = await self._detect_conflicts(current)
        audit.record(
            self.db, self.ctx, "knowledge.submitted", target=str(entry.id),
            details={
                "category": current.category,
                "visibility": current.visibility,
                "revision": current.revision_number,
                "potential_conflicts": conflict_count,
            },
        )
        try:
            await self.db.commit()
        except (IntegrityError, DBAPIError) as exc:
            await self.db.rollback()
            raise _map(exc) from None
        await self.db.refresh(current)
        return current

    async def _detect_conflicts(self, submitted: KnowledgeRevision) -> int:
        """Persist bounded-by-category/scope candidates for a scoped human review queue.

        Private knowledge is excluded. Source ACL is checked again when a reviewer reads the candidate;
        detection alone never exposes a statement or changes whether either statement is searchable.
        """
        scope_overlap = or_(
            KnowledgeRevision.visibility == KNOWLEDGE_SHARED,
            and_(
                KnowledgeRevision.visibility == KNOWLEDGE_RESTRICTED,
                submitted.visibility == KNOWLEDGE_SHARED,
            ),
            and_(
                KnowledgeRevision.visibility == KNOWLEDGE_RESTRICTED,
                submitted.visibility == KNOWLEDGE_RESTRICTED,
                KnowledgeRevision.scope_group_id == submitted.scope_group_id,
            ),
        )
        search_query = conflict_search_query(submitted.statement)
        candidate_query = select(KnowledgeRevision).where(
            KnowledgeRevision.status == "published",
            KnowledgeRevision.entry_id != submitted.entry_id,
            KnowledgeRevision.category == submitted.category,
            KnowledgeRevision.visibility != KNOWLEDGE_PRIVATE,
            scope_overlap,
            or_(KnowledgeRevision.review_due_at.is_(None), KnowledgeRevision.review_due_at > func.now()),
        )
        if search_query:
            lexical_match = func.to_tsvector("simple", KnowledgeRevision.statement).op("@@")(
                func.to_tsquery("simple", search_query)
            )
            candidate_query = candidate_query.order_by(
                lexical_match.desc(), KnowledgeRevision.created_at.desc(), KnowledgeRevision.id
            )
        else:
            candidate_query = candidate_query.order_by(KnowledgeRevision.created_at.desc(), KnowledgeRevision.id)
        candidates = list((await self.db.execute(candidate_query.limit(_MAX_CONFLICT_CANDIDATES))).scalars())
        created = 0
        semantic_candidates: list[KnowledgeRevision] = []
        for candidate in candidates:
            if potential_conflict(submitted.statement, candidate.statement):
                self.db.add(KnowledgeConflict(revision_id=submitted.id, compared_revision_id=candidate.id))
                created += 1
            else:
                semantic_candidates.append(candidate)

        if semantic_candidates and self.embedder is not None and self.embedder.available_locally:
            try:
                vectors = await asyncio.to_thread(
                    self.embedder.embed_local,
                    [submitted.statement, *(candidate.statement for candidate in semantic_candidates)],
                )
                vectors = np.asarray(vectors, dtype=np.float32)
                if vectors.ndim != 2 or vectors.shape != (len(semantic_candidates) + 1, self.embedder.dimension):
                    raise RuntimeError("Local conflict embeddings returned an unexpected shape")
                if not np.isfinite(vectors).all():
                    raise RuntimeError("Local conflict embeddings returned non-finite values")
                norms = np.linalg.norm(vectors, axis=1)
                similarities = np.divide(
                    vectors[1:] @ vectors[0],
                    norms[1:] * norms[0],
                    out=np.zeros(len(semantic_candidates), dtype=np.float32),
                    where=(norms[1:] * norms[0]) > 0,
                )
                for candidate, similarity in zip(semantic_candidates, similarities, strict=True):
                    if potential_conflict(
                        submitted.statement,
                        candidate.statement,
                        semantic_similarity=float(similarity),
                    ):
                        self.db.add(KnowledgeConflict(revision_id=submitted.id, compared_revision_id=candidate.id))
                        created += 1
            except (RuntimeError, OSError) as exc:
                logger.warning(
                    "Optional local conflict embeddings unavailable (%s); retaining lexical candidates only",
                    type(exc).__name__,
                )
        if created:
            await self.db.flush()
            audit.record(
                self.db,
                self.ctx,
                "knowledge.conflict_candidates_detected",
                target=str(submitted.id),
                details={"category": submitted.category, "count": created},
            )
        return created

    def _revision(
        self,
        entry: KnowledgeEntry,
        number: int,
        statement: str,
        category: str,
        visibility: str,
        scope_group_id: uuid.UUID | None,
    ) -> KnowledgeRevision:
        if category not in KNOWLEDGE_CATEGORIES:
            raise InvalidInputError("category is not a recognised knowledge category.")
        return KnowledgeRevision(
            entry_id=entry.id,
            author_user_id=self.user.id,
            revision_number=number,
            statement=statement,
            category=category,
            visibility=visibility,
            scope_group_id=scope_group_id,
            status=KNOWLEDGE_DRAFT,
        )

    async def _attach(self, revision: KnowledgeRevision, passages: list[tuple[uuid.UUID, str]]) -> None:
        if not passages:
            raise InvalidInputError("Add at least one source passage.")
        if len(passages) > 10:
            raise InvalidInputError("A revision can cite at most 10 passages.")
        seen: set[tuple[uuid.UUID, str]] = set()
        for document_id, quote in passages:
            key = (document_id, " ".join(quote.split()))
            if key in seen:
                raise InvalidInputError("The same passage cannot be attached twice.")
            seen.add(key)
            self.db.add(await self._evidence(revision, document_id, quote))
        await self.db.flush()

    async def _evidence(self, revision: KnowledgeRevision, document_id: uuid.UUID, quote: str) -> KnowledgeEvidence:
        snippet = " ".join(quote.split())
        if not snippet or len(snippet) > 2000:
            raise InvalidInputError("A quote must be between 1 and 2000 characters.")
        document = (
            await self.db.execute(select(Document).where(Document.id == document_id, access.readable_by(self.user)))
        ).scalar_one_or_none()
        if document is None:
            raise InvalidInputError("A source document must be one you can already read.")
        if document.indexed_version is None or document.indexed_version != document.version:
            raise ConflictError("This document's indexed revision is stale. Wait until processing finishes.")
        chunks = list(
            (
                await self.db.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == document.id)
                    .order_by(DocumentChunk.chunk_index)
                )
            ).scalars()
        )
        for chunk in chunks:
            start = chunk.content.find(snippet)
            if start < 0:
                continue
            end = start + len(snippet)
            return KnowledgeEvidence(
                revision_id=revision.id,
                document_id=document.id,
                document_key=document.id,
                document_version=document.version,
                document_sha256=document.sha256,
                filename=document.filename,
                chunk_id=chunk.id,
                chunk_key=chunk.id,
                page_number=chunk.page_number,
                start_offset=start,
                end_offset=end,
                quote=chunk.content[start:end],
                prefix=chunk.content[max(0, start - _CONTEXT) : start],
                suffix=chunk.content[end : end + _CONTEXT],
            )
        raise InvalidInputError("That quote is not in the current indexed text of this document.")

    def _visibility(self, visibility: str, scope_group_id: uuid.UUID | None) -> None:
        if visibility not in (KNOWLEDGE_PRIVATE, KNOWLEDGE_RESTRICTED, KNOWLEDGE_SHARED):
            raise InvalidInputError("visibility must be 'private', 'restricted' or 'shared'.")
        if (visibility == KNOWLEDGE_RESTRICTED) != (scope_group_id is not None):
            raise InvalidInputError("A restricted draft needs a scope group, and only a restricted draft has one.")
        if visibility == KNOWLEDGE_SHARED and self.user.role != "admin":
            raise PermissionDeniedError("Only administrators can contribute shared knowledge.")

    async def _scope(self, group_id: uuid.UUID) -> None:
        group = await self.db.get(AccessGroup, group_id)
        if group is None:
            raise InvalidInputError("Unknown scope group.")
        if self.user.role != "admin":
            member = await self.db.get(AccessGroupMember, (group.id, self.user.id))
            manager = await self.db.get(DepartmentManager, (group.id, self.user.id))
            if member is None and manager is None:
                raise InvalidInputError("You can only scope a draft to a department you belong to.")

    async def _own(self, entry_id: uuid.UUID, *, lock: bool = False) -> KnowledgeEntry:
        query = select(KnowledgeEntry).where(
            KnowledgeEntry.id == entry_id, KnowledgeEntry.owner_user_id == self.user.id
        )
        if lock:
            query = query.with_for_update()
        entry = (await self.db.execute(query)).scalar_one_or_none()
        if entry is None:
            raise NotFoundError(_MISSING)
        return entry

    async def _current(self, entry: KnowledgeEntry, *, lock: bool = False) -> KnowledgeRevision:
        if entry.current_revision_id is None:
            raise NotFoundError(_MISSING)
        query = select(KnowledgeRevision).where(KnowledgeRevision.id == entry.current_revision_id)
        if lock:
            query = query.with_for_update()
        revision = (await self.db.execute(query)).scalar_one_or_none()
        if revision is None:
            raise NotFoundError(_MISSING)
        return revision


def _statement(value: str) -> str:
    text = " ".join(value.split()).strip()
    if not text:
        raise InvalidInputError("Write the knowledge statement.")
    if len(text) > 4000:
        raise InvalidInputError("Keep the statement under 4000 characters.")
    return text


def _map(exc: Exception) -> Exception:
    text = str(getattr(exc, "orig", exc))
    if "cannot be published" in text:
        return PermissionDeniedError("Publication is not available.")
    if "cannot exceed the source" in text or "broader than the source" in text or "cannot read the evidence" in text:
        return InvalidInputError("This draft would be visible more widely than its sources.")
    if "current indexed document revision" in text or "source is not readable" in text:
        return InvalidInputError("Evidence must cite a document revision you can read that is currently indexed.")
    if "does not match" in text or "offsets fall outside" in text:
        return InvalidInputError("The quote does not match the indexed passage.")
    if "requires evidence" in text:
        return InvalidInputError("Add at least one source passage.")
    if "immutable" in text or "already orphaned" in text:
        return ConflictError("Recorded provenance cannot be changed.")
    return InvalidInputError("The draft was rejected by an integrity rule.")
