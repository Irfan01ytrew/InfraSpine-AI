"""ACL-scoped proposal coverage workflow using the normal grounded RAG path."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import (
    AccessGroup,
    AccessGroupMember,
    DepartmentManager,
    Document,
    DocumentChunk,
    ROLE_ADMIN,
    ROLE_MANAGER,
    User,
)
from app.db.proposal_models import ProposalCoverageCase, ProposalCoverageRequirement, ProposalCoverageReview
from app.rag.access import readable_by
from app.schemas.proposal_coverage import ProposalCoverageCreate, ProposalCoverageUpdate, ProposalRequirementIn
from app.services import audit
from app.services.audit import AuditContext


REVIEWABLE = ("submitted", "approved", "changes_requested")
SOURCE_WITHHELD = "Saved finding withheld because its source authorization or version changed. Re-run the evidence check."
PROPOSAL_EXTRACTION_MAX_CHUNKS = 200
PROPOSAL_EXTRACTION_MAX_CHARS = 80_000


def validate_requirement_assessment(assessment: str, rationale: str, *, has_citations: bool, insufficient_evidence: bool) -> str:
    if assessment not in ("meets", "partial", "gap"):
        raise InvalidInputError("Choose meets, partial, or evidence gap.")
    clean = " ".join(rationale.split())
    if not 3 <= len(clean) <= 2000:
        raise InvalidInputError("Add an assessment rationale between 3 and 2,000 characters.")
    if assessment in ("meets", "partial") and not has_citations:
        raise InvalidInputError("A supported or partial assessment requires at least one source citation.")
    if assessment == "meets" and insufficient_evidence:
        raise InvalidInputError("The AI marked evidence as insufficient; choose partial or evidence gap instead.")
    return clean


class ProposalCoverageService:
    def __init__(self, db: AsyncSession, user: User, ctx: AuditContext) -> None:
        self.db = db
        self.user = user
        self.ctx = ctx

    async def create(self, payload: ProposalCoverageCreate) -> ProposalCoverageCase:
        if not self.user.is_active:
            raise NotFoundError("Proposal coverage case not found.")
        if payload.department_group_id is not None:
            await self._validate_department(payload.department_group_id)
        origins = await self._stamp_origin_evidence(payload.requirements)
        case = ProposalCoverageCase(
            owner_user_id=self.user.id,
            department_group_id=payload.department_group_id,
            title=payload.title,
        )
        self.db.add(case)
        await self.db.flush()
        self.db.add_all([
            ProposalCoverageRequirement(
                case_id=case.id,
                position=index,
                requirement=item.requirement,
                origin_evidence=origins[index - 1],
            )
            for index, item in enumerate(payload.requirements, start=1)
        ])
        audit.record(
            self.db, self.ctx, "proposal_coverage.created", target=str(case.id),
            details={"requirement_count": len(payload.requirements), "department_scoped": payload.department_group_id is not None},
        )
        await self.db.commit()
        return await self.get(case.id)

    async def update(self, case_id: uuid.UUID, payload: ProposalCoverageUpdate) -> ProposalCoverageCase:
        case = await self.get(case_id, lock=True)
        self._require_owner(case)
        if case.status not in ("draft", "changes_requested"):
            raise ConflictError("Only a draft or returned case can be edited.")
        if payload.department_group_id is not None:
            await self._validate_department(payload.department_group_id)
        origins = await self._stamp_origin_evidence(payload.requirements)
        case.title = payload.title
        case.department_group_id = payload.department_group_id
        old = list((await self.db.scalars(
            select(ProposalCoverageRequirement).where(ProposalCoverageRequirement.case_id == case.id)
        )).all())
        for row in old:
            await self.db.delete(row)
        await self.db.flush()
        self.db.add_all([
            ProposalCoverageRequirement(
                case_id=case.id,
                position=index,
                requirement=item.requirement,
                origin_evidence=origins[index - 1],
            )
            for index, item in enumerate(payload.requirements, start=1)
        ])
        case.updated_at = datetime.now(UTC)
        audit.record(self.db, self.ctx, "proposal_coverage.draft_updated", target=str(case.id),
                     details={"requirement_count": len(payload.requirements)})
        await self.db.commit()
        return await self.get(case.id)

    async def list_cases(self) -> list[ProposalCoverageCase]:
        own = list((await self.db.scalars(
            select(ProposalCoverageCase)
            .where(ProposalCoverageCase.owner_user_id == self.user.id)
            .order_by(ProposalCoverageCase.updated_at.desc()).limit(100)
        )).all())
        reviewable: list[ProposalCoverageCase] = []
        if self.user.role == ROLE_ADMIN:
            query = select(ProposalCoverageCase).where(
                ProposalCoverageCase.owner_user_id != self.user.id,
                ProposalCoverageCase.status.in_(REVIEWABLE),
            )
            reviewable = list((await self.db.scalars(query.order_by(ProposalCoverageCase.updated_at.desc()).limit(300))).all())
        elif self.user.role == ROLE_MANAGER:
            query = select(ProposalCoverageCase).where(
                ProposalCoverageCase.owner_user_id != self.user.id,
                ProposalCoverageCase.status.in_(REVIEWABLE),
            )
            candidates = list((await self.db.scalars(query.order_by(ProposalCoverageCase.updated_at.desc()).limit(500))).all())
            reviewable = [case for case in candidates if await self._can_review(case)]
        unique = {row.id: row for row in [*own, *reviewable]}
        return sorted(unique.values(), key=lambda row: row.updated_at, reverse=True)[:500]

    async def departments(self) -> list[AccessGroup]:
        query = select(AccessGroup).where(AccessGroup.kind == "department", AccessGroup.source == "local")
        if self.user.role == ROLE_ADMIN:
            query = query.order_by(AccessGroup.name)
        elif self.user.role == ROLE_MANAGER:
            query = (
                query.join(DepartmentManager, DepartmentManager.group_id == AccessGroup.id)
                .join(AccessGroupMember, AccessGroupMember.group_id == AccessGroup.id)
                .where(
                    DepartmentManager.manager_user_id == self.user.id,
                    AccessGroupMember.user_id == self.user.id,
                ).order_by(AccessGroup.name)
            )
        else:
            query = (
                query.join(AccessGroupMember, AccessGroupMember.group_id == AccessGroup.id)
                .where(AccessGroupMember.user_id == self.user.id).order_by(AccessGroup.name)
            )
        return list((await self.db.scalars(query)).all())

    async def extraction_source_chunks(
        self, document_ids: list[uuid.UUID]
    ) -> list[tuple[Document, DocumentChunk]]:
        """Load bounded, current RFP passages that the caller can still read."""
        documents = list((await self.db.scalars(
            select(Document).where(Document.id.in_(document_ids), readable_by(self.user))
        )).all())
        if len(documents) != len(document_ids):
            raise NotFoundError("An RFP source document was not found.")
        if any(document.file_format not in {"pdf", "docx", "xlsx"} for document in documents):
            raise InvalidInputError("Select only PDF, DOCX, or XLSX RFP source documents.")
        if any(document.status != "ready" or document.indexed_version != document.version for document in documents):
            raise ConflictError("Wait until every selected RFP document is fully indexed before extracting requirements.")

        query_rows = list((await self.db.execute(
            select(DocumentChunk, Document)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(Document.id.in_(document_ids), readable_by(self.user))
            .order_by(Document.id, DocumentChunk.chunk_index)
        )).all())
        if not query_rows:
            raise InvalidInputError("The selected RFP documents do not contain readable extracted text.")
        source_ids = {document.id for _, document in query_rows}
        if source_ids != set(document_ids):
            raise InvalidInputError("Every selected RFP document must contain extracted text.")
        if len(query_rows) > PROPOSAL_EXTRACTION_MAX_CHUNKS:
            raise InvalidInputError(
                f"The selected RFPs exceed the {PROPOSAL_EXTRACTION_MAX_CHUNKS}-passage extraction limit. Select fewer pages or files."
            )
        total_chars = sum(len(chunk.content) for chunk, _ in query_rows)
        if total_chars > PROPOSAL_EXTRACTION_MAX_CHARS:
            raise InvalidInputError(
                f"The selected RFP text exceeds the {PROPOSAL_EXTRACTION_MAX_CHARS:,}-character extraction limit. Select fewer pages or files."
            )
        return [(document, chunk) for chunk, document in query_rows]

    async def _stamp_origin_evidence(
        self, requirements: list[ProposalRequirementIn]
    ) -> list[list[dict]]:
        requested_ids = {chunk_id for item in requirements for chunk_id in item.source_chunk_ids}
        if not requested_ids:
            return [[] for _ in requirements]
        rows = list((await self.db.execute(
            select(DocumentChunk, Document)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.id.in_(requested_ids),
                Document.file_format.in_(("pdf", "docx", "xlsx")),
                Document.status == "ready",
                Document.indexed_version == Document.version,
                readable_by(self.user),
            )
        )).all())
        if len(rows) != len(requested_ids):
            raise NotFoundError("An RFP source passage was not found.")
        by_id = {chunk.id: (document, chunk) for chunk, document in rows}
        if len(by_id) != len(requested_ids):
            raise NotFoundError("An RFP source passage was not found.")

        stamped: list[list[dict]] = []
        for item in requirements:
            citations = []
            for chunk_id in item.source_chunk_ids:
                document, chunk = by_id[chunk_id]
                locator = chunk.locator if isinstance(chunk.locator, dict) else {}
                location = locator.get("label")
                if not isinstance(location, str) or not location.strip():
                    location = f"Page {chunk.page_number}"
                citations.append({
                    "chunk_id": chunk.id,
                    "document_id": str(document.id),
                    "document_version": document.version,
                    "document_sha256": document.sha256,
                    "filename": document.indexed_filename or document.filename,
                    "location": location[:300],
                    "excerpt": chunk.content[:1000],
                })
            stamped.append(citations)
        return stamped

    async def get(self, case_id: uuid.UUID, *, lock: bool = False) -> ProposalCoverageCase:
        query = select(ProposalCoverageCase).where(ProposalCoverageCase.id == case_id)
        if lock:
            query = query.with_for_update()
        case = (await self.db.execute(query)).scalar_one_or_none()
        if case is None or not self.user.is_active:
            raise NotFoundError("Proposal coverage case not found.")
        if case.owner_user_id != self.user.id and not await self._can_review(case):
            raise NotFoundError("Proposal coverage case not found.")
        return case

    async def get_requirement(self, case_id: uuid.UUID, requirement_id: uuid.UUID) -> ProposalCoverageRequirement:
        case = await self.get(case_id)
        self._require_owner(case)
        if case.status not in ("draft", "changes_requested"):
            raise ConflictError("Evidence can be refreshed only on a draft or returned case.")
        item = await self.db.scalar(select(ProposalCoverageRequirement).where(
            ProposalCoverageRequirement.id == requirement_id,
            ProposalCoverageRequirement.case_id == case.id,
        ))
        if item is None:
            raise NotFoundError("Proposal requirement not found.")
        return item

    async def save_analysis(
        self,
        case_id: uuid.UUID,
        requirement_id: uuid.UUID,
        *,
        answer: str,
        insufficient_evidence: bool,
        citations: list[dict],
        retrieved_chunks: int,
    ) -> ProposalCoverageRequirement:
        case = await self.get(case_id, lock=True)
        self._require_owner(case)
        if case.status not in ("draft", "changes_requested"):
            raise ConflictError("Evidence can be refreshed only on a draft or returned case.")
        item = await self.db.scalar(select(ProposalCoverageRequirement).where(
            ProposalCoverageRequirement.id == requirement_id,
            ProposalCoverageRequirement.case_id == case.id,
        ).with_for_update())
        if item is None:
            raise NotFoundError("Proposal requirement not found.")
        saved_citations = await self._stamp_citations(citations)
        item.answer = answer[:8000] if saved_citations and not insufficient_evidence else (
            answer[:8000] if saved_citations else "The available company sources did not provide a citable answer."
        )
        item.insufficient_evidence = insufficient_evidence or not bool(saved_citations)
        item.citations = saved_citations
        item.retrieved_chunks = max(0, retrieved_chunks)
        item.analyzed_at = datetime.now(UTC)
        self._reset_assessment(item)
        case.updated_at = item.analyzed_at
        audit.record(
            self.db, self.ctx, "proposal_coverage.evidence_checked", target=str(case.id),
            details={"requirement_id": str(item.id), "citation_count": len(saved_citations),
                     "insufficient_evidence": item.insufficient_evidence},
        )
        await self.db.commit()
        await self.db.refresh(item)
        return item

    async def submit(self, case_id: uuid.UUID) -> ProposalCoverageCase:
        case = await self.get(case_id, lock=True)
        self._require_owner(case)
        if case.status not in ("draft", "changes_requested"):
            raise ConflictError("Only a draft or returned case can be submitted.")
        if case.department_group_id is None:
            raise InvalidInputError("Choose a department before submitting the proposal for review.")
        items = await self._requirements(case.id)
        if not items or any(item.analyzed_at is None for item in items):
            raise InvalidInputError("Run an evidence check for every requirement before review.")
        for item in items:
            if item.citations and not await self._citations_current(item.citations):
                raise ConflictError("A source changed or access was revoked. Refresh affected evidence before submission.")
            if item.origin_evidence and not await self._citations_current(item.origin_evidence):
                raise ConflictError("An RFP source changed or access was revoked. Re-extract the affected requirement before submission.")
        case.status = "submitted"
        case.revision_number += 1
        case.submitted_at = datetime.now(UTC)
        case.decided_at = None
        case.updated_at = case.submitted_at
        audit.record(self.db, self.ctx, "proposal_coverage.submitted", target=str(case.id),
                     details={"revision": case.revision_number, "requirement_count": len(items)})
        await self.db.commit()
        return await self.get(case.id)

    async def assess(
        self, case_id: uuid.UUID, requirement_id: uuid.UUID, assessment: str, rationale: str
    ) -> ProposalCoverageRequirement:
        case = await self.get(case_id, lock=True)
        if case.owner_user_id == self.user.id or not await self._can_review(case):
            raise PermissionDeniedError("A different authorized proposal reviewer is required.")
        if case.status != "submitted":
            raise ConflictError("Requirements can be assessed only while the case is under review.")
        item = await self.db.scalar(select(ProposalCoverageRequirement).where(
            ProposalCoverageRequirement.id == requirement_id,
            ProposalCoverageRequirement.case_id == case.id,
        ).with_for_update())
        if item is None:
            raise NotFoundError("Proposal requirement not found.")
        clean = validate_requirement_assessment(
            assessment, rationale, has_citations=bool(item.citations),
            insufficient_evidence=item.insufficient_evidence,
        )
        if item.citations and not await self._citations_current(item.citations):
            raise ConflictError("A source changed or access was revoked. The case owner must refresh the evidence.")
        if item.origin_evidence and not await self._citations_current(item.origin_evidence):
            raise ConflictError("An RFP source changed or access was revoked. The case owner must refresh the extracted requirement.")
        item.assessment = assessment
        item.assessment_rationale = clean
        item.assessed_by_user_id = self.user.id
        item.assessed_at = datetime.now(UTC)
        case.updated_at = item.assessed_at
        audit.record(self.db, self.ctx, "proposal_coverage.requirement_assessed", target=str(case.id),
                     details={"requirement_id": str(item.id), "assessment": assessment})
        await self.db.commit()
        await self.db.refresh(item)
        return item

    async def review(self, case_id: uuid.UUID, decision: str, rationale: str) -> ProposalCoverageCase:
        case = await self.get(case_id, lock=True)
        if case.owner_user_id == self.user.id or not await self._can_review(case):
            raise PermissionDeniedError("A different authorized department reviewer is required.")
        if case.status != "submitted":
            raise ConflictError("Only a submitted proposal revision can be reviewed.")
        if decision not in ("approved", "changes_requested"):
            raise InvalidInputError("Choose approve or request changes.")
        clean = " ".join(rationale.split())
        if not 3 <= len(clean) <= 2000:
            raise InvalidInputError("Add a review rationale between 3 and 2,000 characters.")
        items = await self._requirements(case.id)
        if decision == "approved":
            if not items or any(item.assessment == "not_assessed" for item in items):
                raise ConflictError("Assess every requirement before approving the proposal.")
            if any(item.assessment in ("meets", "partial") and not item.citations for item in items):
                raise ConflictError("A supported assessment is missing its required source citation.")
            for item in items:
                if item.citations and not await self._citations_current(item.citations):
                    raise ConflictError("A source changed or access was revoked. Refresh the case before approval.")
                if item.origin_evidence and not await self._citations_current(item.origin_evidence):
                    raise ConflictError("An RFP source changed or access was revoked. Refresh the case before approval.")
        snapshot = {
            "title": case.title,
            "department_group_id": str(case.department_group_id) if case.department_group_id else None,
            "requirements": [
                {
                    "requirement_id": str(item.id),
                    "position": item.position,
                    "requirement": item.requirement,
                    "assessment": item.assessment,
                    "citation_document_ids": sorted({str(c.get("document_id")) for c in item.citations}),
                    "origin_document_ids": sorted({str(c.get("document_id")) for c in item.origin_evidence}),
                }
                for item in items
            ],
        }
        self.db.add(ProposalCoverageReview(
            case_id=case.id,
            revision_number=case.revision_number,
            reviewer_user_id=self.user.id,
            decision=decision,
            rationale=clean,
            snapshot=snapshot,
        ))
        case.status = decision
        case.decided_at = datetime.now(UTC)
        case.updated_at = case.decided_at
        audit.record(self.db, self.ctx, f"proposal_coverage.{decision}", target=str(case.id),
                     details={"revision": case.revision_number, "requirement_count": len(items)})
        await self.db.commit()
        return await self.get(case.id)

    async def withdraw(self, case_id: uuid.UUID) -> ProposalCoverageCase:
        case = await self.get(case_id, lock=True)
        self._require_owner(case)
        if case.status not in ("draft", "submitted", "changes_requested"):
            raise ConflictError("An approved or withdrawn proposal cannot be withdrawn.")
        case.status = "withdrawn"
        case.updated_at = datetime.now(UTC)
        audit.record(self.db, self.ctx, "proposal_coverage.withdrawn", target=str(case.id),
                     details={"revision": case.revision_number})
        await self.db.commit()
        return await self.get(case.id)

    async def detail(self, case: ProposalCoverageCase) -> dict:
        owner = await self.db.get(User, case.owner_user_id)
        department = await self.db.get(AccessGroup, case.department_group_id) if case.department_group_id else None
        items = await self._requirements(case.id)
        citation_ids = {uuid.UUID(str(citation["document_id"])) for item in items for citation in item.citations
                        if citation.get("document_id")}
        origin_ids = {uuid.UUID(str(citation["document_id"])) for item in items for citation in item.origin_evidence
                      if citation.get("document_id")}
        current = await self._current_documents(citation_ids | origin_ids)
        requirement_views = []
        for item in items:
            citations_current = bool(item.citations) and all(
                self._citation_current(citation, current) for citation in item.citations
            )
            # A saved answer may combine multiple passages. Hide the entire finding if any source is no longer
            # readable or has changed, preventing stale excerpts from surviving an ACL or version change.
            visible = not item.citations or citations_current
            origin_evidence_current = not item.origin_evidence or all(
                self._citation_current(citation, current) for citation in item.origin_evidence
            )
            requirement_views.append({
                "id": item.id,
                "position": item.position,
                "requirement": item.requirement,
                "answer": item.answer if visible else SOURCE_WITHHELD,
                "insufficient_evidence": item.insufficient_evidence or (bool(item.citations) and not visible),
                "retrieved_chunks": item.retrieved_chunks if visible else 0,
                "analyzed_at": item.analyzed_at,
                "evidence_current": visible,
                "citations": item.citations if visible else [],
                "origin_evidence": item.origin_evidence if origin_evidence_current else [],
                "origin_evidence_current": origin_evidence_current,
                "assessment": item.assessment,
                "assessment_rationale": item.assessment_rationale,
                "assessed_by_user_id": item.assessed_by_user_id,
                "assessed_at": item.assessed_at,
            })
        review_rows = (await self.db.execute(
            select(ProposalCoverageReview, User.email)
            .join(User, User.id == ProposalCoverageReview.reviewer_user_id)
            .where(ProposalCoverageReview.case_id == case.id)
            .order_by(ProposalCoverageReview.reviewed_at)
        )).all()
        own = case.owner_user_id == self.user.id
        can_review = not own and await self._can_review(case) and case.status == "submitted"
        return {
            "id": case.id,
            "title": case.title,
            "status": case.status,
            "revision_number": case.revision_number,
            "owner_user_id": case.owner_user_id,
            "owner_email": owner.email if owner else "Former employee",
            "department_group_id": case.department_group_id,
            "department_name": department.name if department else None,
            "requirement_count": len(items),
            "analyzed_count": sum(item.analyzed_at is not None for item in items),
            "assessment_count": sum(item.assessment != "not_assessed" for item in items),
            "created_at": case.created_at,
            "updated_at": case.updated_at,
            "submitted_at": case.submitted_at,
            "decided_at": case.decided_at,
            "requirements": requirement_views,
            "reviews": [{
                "id": review.id,
                "revision_number": review.revision_number,
                "reviewer_user_id": review.reviewer_user_id,
                "reviewer_email": email,
                "decision": review.decision,
                "rationale": review.rationale,
                "reviewed_at": review.reviewed_at,
            } for review, email in review_rows],
            "can_edit": own and case.status in ("draft", "changes_requested"),
            "can_submit": (
                own
                and case.department_group_id is not None
                and case.status in ("draft", "changes_requested")
                and all(item.analyzed_at for item in items)
            ),
            "can_review": can_review,
        }

    async def summaries(self, cases: list[ProposalCoverageCase]) -> list[dict]:
        result = []
        for case in cases:
            owner = await self.db.get(User, case.owner_user_id)
            department = await self.db.get(AccessGroup, case.department_group_id) if case.department_group_id else None
            items = await self._requirements(case.id)
            result.append({
                "id": case.id, "title": case.title, "status": case.status,
                "revision_number": case.revision_number, "owner_user_id": case.owner_user_id,
                "owner_email": owner.email if owner else "Former employee",
                "department_group_id": case.department_group_id,
                "department_name": department.name if department else None,
                "requirement_count": len(items),
                "analyzed_count": sum(item.analyzed_at is not None for item in items),
                "assessment_count": sum(item.assessment != "not_assessed" for item in items),
                "created_at": case.created_at, "updated_at": case.updated_at,
            })
        return result

    async def _stamp_citations(self, citations: list[dict]) -> list[dict]:
        ids = {uuid.UUID(str(item["document_id"])) for item in citations if item.get("document_id")}
        docs = await self._current_documents(ids)
        saved = []
        for citation in citations[:10]:
            document_id = uuid.UUID(str(citation["document_id"]))
            doc = docs.get(document_id)
            if doc is None:
                continue
            version, sha256 = doc
            saved.append({**citation, "document_id": str(document_id),
                          "document_version": version, "document_sha256": sha256})
        return saved

    async def _current_documents(self, document_ids: set[uuid.UUID]) -> dict[uuid.UUID, tuple[int, str | None]]:
        if not document_ids:
            return {}
        rows = (await self.db.execute(
            select(Document.id, Document.version, Document.sha256).where(
                Document.id.in_(document_ids),
                readable_by(self.user),
                Document.status == "ready",
                Document.indexed_version == Document.version,
            )
        )).all()
        return {doc_id: (version, sha256) for doc_id, version, sha256 in rows}

    @staticmethod
    def _citation_current(citation: dict, current: dict[uuid.UUID, tuple[int, str | None]]) -> bool:
        try:
            doc_id = uuid.UUID(str(citation["document_id"]))
            version, sha256 = current[doc_id]
            return citation.get("document_version") == version and citation.get("document_sha256") == sha256
        except (KeyError, ValueError, TypeError):
            return False

    async def _citations_current(self, citations: list[dict]) -> bool:
        ids = {uuid.UUID(str(item["document_id"])) for item in citations if item.get("document_id")}
        current = await self._current_documents(ids)
        return len(current) == len(ids) and all(self._citation_current(item, current) for item in citations)

    async def _requirements(self, case_id: uuid.UUID) -> list[ProposalCoverageRequirement]:
        return list((await self.db.scalars(
            select(ProposalCoverageRequirement)
            .where(ProposalCoverageRequirement.case_id == case_id)
            .order_by(ProposalCoverageRequirement.position)
        )).all())

    async def _validate_department(self, group_id: uuid.UUID) -> None:
        group = await self.db.get(AccessGroup, group_id)
        if group is None or group.kind != "department" or group.source != "local":
            raise NotFoundError("Proposal department not found.")
        if self.user.role == ROLE_ADMIN:
            return
        member = await self.db.scalar(select(AccessGroupMember.user_id).where(
            AccessGroupMember.group_id == group_id, AccessGroupMember.user_id == self.user.id
        ))
        if member is None:
            raise PermissionDeniedError("You must be an active department member to use this proposal scope.")
        if self.user.role == ROLE_MANAGER:
            managed = await self.db.scalar(select(DepartmentManager.group_id).where(
                DepartmentManager.group_id == group_id, DepartmentManager.manager_user_id == self.user.id
            ))
            if managed is None:
                raise PermissionDeniedError("Managers can create proposals only in departments they manage.")

    async def _can_review(self, case: ProposalCoverageCase) -> bool:
        if case.status not in REVIEWABLE or case.owner_user_id == self.user.id:
            return False
        if self.user.role == ROLE_ADMIN:
            return True
        if self.user.role != ROLE_MANAGER or case.department_group_id is None:
            return False
        row = await self.db.scalar(
            select(DepartmentManager.group_id)
            .join(AccessGroupMember, AccessGroupMember.group_id == DepartmentManager.group_id)
            .join(AccessGroup, AccessGroup.id == DepartmentManager.group_id)
            .where(
                DepartmentManager.group_id == case.department_group_id,
                DepartmentManager.manager_user_id == self.user.id,
                AccessGroupMember.user_id == self.user.id,
                AccessGroup.kind == "department",
                AccessGroup.source == "local",
            )
        )
        return row is not None

    def _require_owner(self, case: ProposalCoverageCase) -> None:
        if case.owner_user_id != self.user.id:
            raise NotFoundError("Proposal coverage case not found.")

    @staticmethod
    def _reset_assessment(item: ProposalCoverageRequirement) -> None:
        item.assessment = "not_assessed"
        item.assessment_rationale = ""
        item.assessed_by_user_id = None
        item.assessed_at = None
