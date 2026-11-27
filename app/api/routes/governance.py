"""Scoped two-person governance decisions for submitted organizational knowledge claims."""

import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, DbSession, audit_context
from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import (
    CLAIM_SUBMITTED,
    KNOWLEDGE_SUBMITTED,
    PERMISSION_PUBLISH,
    PERMISSION_REVIEW,
    STAGE_PUBLISH,
    STAGE_REVIEW,
    GovernanceApproval,
    AccessGroupMember,
    DepartmentManager,
    KnowledgeConflict,
    KnowledgeConflictReview,
    KnowledgeFreshnessReview,
    KnowledgeClaim,
    KnowledgeEvidence,
    KnowledgeRevision,
    KnowledgeEntry,
    KnowledgeProcess,
    KnowledgeProcessPolicyLink,
    KnowledgePolicyException,
    KnowledgePolicyExceptionEvent,
    User,
)
from app.schemas.governance import (
    GovernanceClaimListResponse,
    GovernanceClaimOut,
    GovernanceDecisionOut,
    GovernanceDecisionRequest,
    GovernanceKnowledgeListResponse,
    GovernanceKnowledgeOut,
    GovernanceKnowledgeSourceOut,
    KnowledgeFreshnessReviewOut,
    KnowledgeFreshnessReviewRequest,
    KnowledgeFreshnessHistoryOut,
    KnowledgeConflictDecisionOut,
    KnowledgeConflictDecisionRequest,
    KnowledgeConflictListResponse,
    KnowledgeConflictOut,
    KnowledgeConflictReviewOut,
    KnowledgeConflictSourceOut,
    KnowledgeConflictStatementOut,
    PolicyCandidateListResponse,
    PolicyCandidateOut,
    PolicyExceptionActionRequest,
    PolicyExceptionCreateRequest,
    PolicyExceptionEventOut,
    PolicyExceptionListResponse,
    PolicyExceptionOut,
    PolicyImpactPolicyOut,
    PolicyProcessAssigneeListResponse,
    PolicyProcessAssigneeOut,
    PolicyProcessCreateRequest,
    PolicyProcessLinkRequest,
    PolicyProcessListResponse,
    PolicyProcessOut,
    PolicyProcessSourceOut,
    StaleKnowledgeListResponse,
    StaleKnowledgeOut,
)
from app.rag.access import knowledge_readable_by
from app.services.access_control import find_active_grant, record_approval
from app.services import audit

router = APIRouter(prefix="/api/governance", tags=["governance"])


def _source(raw: dict) -> dict:
    kind = raw["kind"]
    if kind == "document":
        return {"kind": kind, "document_id": raw["document_id"], "filename": raw["filename"]}
    if kind == "url":
        return {"kind": kind, "url": raw["url"]}
    return {"kind": "note", "note": raw["note"]}


@router.get("/claims", response_model=GovernanceClaimListResponse)
async def review_queue(db: DbSession, user: CurrentUser) -> GovernanceClaimListResponse:
    claims = list(
        (
            await db.execute(
                select(KnowledgeClaim, User.email)
                .join(User, User.id == KnowledgeClaim.claimant_user_id)
                .where(KnowledgeClaim.status == CLAIM_SUBMITTED)
                .order_by(KnowledgeClaim.created_at)
            )
        ).all()
    )
    output = []
    for claim, claimant_email in claims:
        approvals = list(
            (
                await db.execute(
                    select(GovernanceApproval, User.email)
                    .join(User, User.id == GovernanceApproval.approver_user_id)
                    .where(
                        GovernanceApproval.subject_type == "knowledge_claim",
                        GovernanceApproval.subject_id == claim.id,
                    )
                    .order_by(GovernanceApproval.decided_at)
                )
            ).all()
        )
        by_stage = {approval.stage: (approval, email) for approval, email in approvals}
        allowed: list[str] = []
        if user.id != claim.claimant_user_id and STAGE_REVIEW not in by_stage:
            if await find_active_grant(db, user.id, PERMISSION_REVIEW, claim.category, claim.scope_group_id):
                allowed.append(STAGE_REVIEW)
        if user.id != claim.claimant_user_id and STAGE_PUBLISH not in by_stage and STAGE_REVIEW in by_stage:
            prior_reviewer = by_stage[STAGE_REVIEW][0].approver_user_id
            if user.id != prior_reviewer and await find_active_grant(
                db, user.id, PERMISSION_PUBLISH, claim.category, claim.scope_group_id
            ):
                allowed.append(STAGE_PUBLISH)
        if not allowed:
            continue
        output.append(
            GovernanceClaimOut(
                id=claim.id,
                kind=claim.kind,
                statement=claim.statement,
                claimant_email=claimant_email,
                category=claim.category,
                scope_group_id=claim.scope_group_id,
                sources=[_source(source) for source in claim.sources],
                created_at=claim.created_at,
                decisions=[
                    GovernanceDecisionOut(
                        stage=approval.stage,
                        approver_email=email,
                        rationale=approval.rationale,
                        decided_at=approval.decided_at,
                    )
                    for approval, email in approvals
                ],
                allowed_stages=allowed,
            )
        )
    return GovernanceClaimListResponse(items=output)


@router.post("/claims/{claim_id}/decisions", response_model=GovernanceDecisionOut)
async def decide_claim(
    claim_id: uuid.UUID,
    payload: GovernanceDecisionRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> GovernanceDecisionOut:
    claim = await db.get(KnowledgeClaim, claim_id)
    if claim is None:
        raise NotFoundError("Claim not found.")
    if claim.status != CLAIM_SUBMITTED:
        raise ConflictError("Only a submitted claim can receive a decision.")
    approval = await record_approval(
        db,
        user,
        audit_context(request, user),
        subject_type="knowledge_claim",
        subject_id=claim.id,
        stage=payload.stage,
        category=claim.category,
        scope_group_id=claim.scope_group_id,
        rationale=payload.rationale,
    )
    return GovernanceDecisionOut(
        stage=approval.stage,
        approver_email=user.email,
        rationale=approval.rationale,
        decided_at=approval.decided_at,
    )


@router.get("/knowledge", response_model=GovernanceKnowledgeListResponse)
async def knowledge_review_queue(db: DbSession, user: CurrentUser) -> GovernanceKnowledgeListResponse:
    """Only show submitted revisions whose full source evidence is readable by this reviewer."""
    revisions = list(
        (
            await db.execute(
                select(KnowledgeRevision, User.email)
                .join(User, User.id == KnowledgeRevision.author_user_id)
                .where(knowledge_readable_by(user, KNOWLEDGE_SUBMITTED))
                .order_by(KnowledgeRevision.created_at)
            )
        ).all()
    )
    output = []
    for revision, author_email in revisions:
        approvals = list(
            (
                await db.execute(
                    select(GovernanceApproval, User.email)
                    .join(User, User.id == GovernanceApproval.approver_user_id)
                    .where(
                        GovernanceApproval.subject_type == "knowledge_revision",
                        GovernanceApproval.subject_id == revision.id,
                    )
                    .order_by(GovernanceApproval.decided_at)
                )
            ).all()
        )
        by_stage = {approval.stage: (approval, email) for approval, email in approvals}
        allowed: list[str] = []
        if user.id != revision.author_user_id and STAGE_REVIEW not in by_stage:
            if await find_active_grant(db, user.id, PERMISSION_REVIEW, revision.category, revision.scope_group_id):
                allowed.append(STAGE_REVIEW)
        if user.id != revision.author_user_id and STAGE_PUBLISH not in by_stage and STAGE_REVIEW in by_stage:
            prior_reviewer = by_stage[STAGE_REVIEW][0].approver_user_id
            if user.id != prior_reviewer and await find_active_grant(
                db, user.id, PERMISSION_PUBLISH, revision.category, revision.scope_group_id
            ):
                allowed.append(STAGE_PUBLISH)
        if not allowed:
            continue
        evidence_rows = list(
            (
                await db.execute(
                    select(KnowledgeEvidence)
                    .where(KnowledgeEvidence.revision_id == revision.id)
                    .order_by(KnowledgeEvidence.id)
                )
            ).scalars()
        )
        output.append(
            GovernanceKnowledgeOut(
                id=revision.id,
                entry_id=revision.entry_id,
                statement=revision.statement,
                author_email=author_email,
                category=revision.category,
                visibility=revision.visibility,
                scope_group_id=revision.scope_group_id,
                created_at=revision.created_at,
                sources=[
                    GovernanceKnowledgeSourceOut(
                        filename=source.filename, page_number=source.page_number, quote=source.quote
                    )
                    for source in evidence_rows
                ],
                decisions=[
                    GovernanceDecisionOut(
                        stage=approval.stage,
                        approver_email=email,
                        rationale=approval.rationale,
                        decided_at=approval.decided_at,
                    )
                    for approval, email in approvals
                ],
                allowed_stages=allowed,
            )
        )
    return GovernanceKnowledgeListResponse(items=output)


@router.post("/knowledge/{revision_id}/decisions", response_model=GovernanceDecisionOut)
async def decide_knowledge_revision(
    revision_id: uuid.UUID,
    payload: GovernanceDecisionRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> GovernanceDecisionOut:
    revision = (
        await db.execute(
            select(KnowledgeRevision).where(
                KnowledgeRevision.id == revision_id,
                knowledge_readable_by(user, KNOWLEDGE_SUBMITTED),
            )
        )
    ).scalar_one_or_none()
    if revision is None:
        raise NotFoundError("Knowledge revision not found.")
    approval = await record_approval(
        db,
        user,
        audit_context(request, user),
        subject_type="knowledge_revision",
        subject_id=revision.id,
        stage=payload.stage,
        category=revision.category,
        scope_group_id=revision.scope_group_id,
        rationale=payload.rationale,
    )
    return GovernanceDecisionOut(
        stage=approval.stage,
        approver_email=user.email,
        rationale=approval.rationale,
        decided_at=approval.decided_at,
    )


@router.get("/knowledge/stale", response_model=StaleKnowledgeListResponse)
async def stale_knowledge_queue(db: DbSession, user: CurrentUser) -> StaleKnowledgeListResponse:
    """Return due published knowledge only to reviewers who can still read all cited sources."""
    rows = list(
        (
            await db.execute(
                select(KnowledgeRevision, User.email)
                .join(User, User.id == KnowledgeRevision.author_user_id)
                .where(
                    KnowledgeRevision.status == "published",
                    KnowledgeRevision.review_due_at <= func.now(),
                    knowledge_readable_by(user, "published", include_stale=True),
                )
                .order_by(KnowledgeRevision.review_due_at, KnowledgeRevision.created_at)
            )
        ).all()
    )
    output: list[StaleKnowledgeOut] = []
    for revision, author_email in rows:
        if revision.author_user_id == user.id:
            continue
        if await find_active_grant(db, user.id, PERMISSION_REVIEW, revision.category, revision.scope_group_id) is None:
            continue
        evidence_rows = list(
            (
                await db.execute(
                    select(KnowledgeEvidence)
                    .where(KnowledgeEvidence.revision_id == revision.id)
                    .order_by(KnowledgeEvidence.id)
                )
            ).scalars()
        )
        last_review = (
            await db.execute(
                select(KnowledgeFreshnessReview, User.email)
                .join(User, User.id == KnowledgeFreshnessReview.reviewer_user_id)
                .where(KnowledgeFreshnessReview.revision_id == revision.id)
                .order_by(KnowledgeFreshnessReview.reviewed_at.desc())
                .limit(1)
            )
        ).first()
        output.append(
            StaleKnowledgeOut(
                id=revision.id,
                entry_id=revision.entry_id,
                statement=revision.statement,
                author_email=author_email,
                category=revision.category,
                visibility=revision.visibility,
                scope_group_id=revision.scope_group_id,
                created_at=revision.created_at,
                last_reviewed_at=revision.last_reviewed_at,
                review_due_at=revision.review_due_at,
                sources=[
                    GovernanceKnowledgeSourceOut(filename=source.filename, page_number=source.page_number, quote=source.quote)
                    for source in evidence_rows
                ],
                last_freshness_review=(
                    KnowledgeFreshnessHistoryOut(
                        reviewer_email=last_review[1],
                        rationale=last_review[0].rationale,
                        reviewed_at=last_review[0].reviewed_at,
                        next_review_due_at=last_review[0].next_review_due_at,
                    )
                    if last_review
                    else None
                ),
            )
        )
    return StaleKnowledgeListResponse(items=output)


@router.post(
    "/knowledge/{revision_id}/freshness-review",
    response_model=KnowledgeFreshnessReviewOut,
)
async def review_knowledge_freshness(
    revision_id: uuid.UUID,
    payload: KnowledgeFreshnessReviewRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> KnowledgeFreshnessReviewOut:
    revision = (
        await db.execute(
            select(KnowledgeRevision)
            .where(
                KnowledgeRevision.id == revision_id,
                KnowledgeRevision.status == "published",
                KnowledgeRevision.review_due_at <= func.now(),
                knowledge_readable_by(user, "published", include_stale=True),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if revision is None:
        raise NotFoundError("Due knowledge revision not found.")
    if revision.author_user_id == user.id:
        raise PermissionDeniedError("A contributor cannot review the freshness of their own statement.")
    grant = await find_active_grant(
        db, user.id, PERMISSION_REVIEW, revision.category, revision.scope_group_id, lock=True
    )
    if grant is None:
        raise PermissionDeniedError("You do not hold the permission required for this freshness review.")

    reviewed_at = datetime.now(UTC)
    next_due = reviewed_at + timedelta(days=365)
    event = KnowledgeFreshnessReview(
        revision_id=revision.id,
        reviewer_user_id=user.id,
        permission_grant_id=grant.id,
        rationale=payload.rationale,
        reviewed_at=reviewed_at,
        next_review_due_at=next_due,
    )
    db.add(event)
    try:
        # The database trigger on the revision deadline requires this append-only event to exist first.
        await db.flush()
        revision.last_reviewed_at = reviewed_at
        revision.review_due_at = next_due
        audit.record(
            db,
            audit_context(request, user),
            "knowledge.freshness_reviewed",
            target=str(revision.id),
            details={"category": revision.category, "permission_grant_id": str(grant.id)},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if "freshness review history is append-only" in str(exc.orig) or "authorized review event" in str(exc.orig):
            raise ConflictError("The freshness review could not be recorded. Refresh the queue and try again.") from None
        raise ConflictError("This knowledge item is no longer due for review or the reviewer grant changed.") from None
    return KnowledgeFreshnessReviewOut(
        revision_id=revision.id,
        reviewer_email=user.email,
        rationale=event.rationale,
        reviewed_at=event.reviewed_at,
        next_review_due_at=event.next_review_due_at,
    )


async def _conflict_revision_readable(db: DbSession, user: CurrentUser, revision: KnowledgeRevision) -> bool:
    return bool(
        await db.scalar(
            select(KnowledgeRevision.id).where(
                KnowledgeRevision.id == revision.id,
                knowledge_readable_by(user, revision.status, include_stale=True),
            )
        )
    )


async def _conflict_out(db: DbSession, conflict: KnowledgeConflict) -> KnowledgeConflictOut:
    submitted = await db.get(KnowledgeRevision, conflict.revision_id)
    existing = await db.get(KnowledgeRevision, conflict.compared_revision_id)
    if submitted is None or existing is None:
        raise NotFoundError("Knowledge conflict not found.")

    async def statement_out(revision: KnowledgeRevision) -> KnowledgeConflictStatementOut:
        author = await db.get(User, revision.author_user_id)
        evidence = list(
            (
                await db.execute(
                    select(KnowledgeEvidence).where(KnowledgeEvidence.revision_id == revision.id).order_by(KnowledgeEvidence.id)
                )
            ).scalars()
        )
        return KnowledgeConflictStatementOut(
            revision_id=revision.id,
            statement=revision.statement,
            author_email=author.email if author else "",
            visibility=revision.visibility,
            scope_group_id=revision.scope_group_id,
            sources=[
                KnowledgeConflictSourceOut(filename=source.filename, page_number=source.page_number, quote=source.quote)
                for source in evidence
            ],
        )

    review_row = (
        await db.execute(
            select(KnowledgeConflictReview, User.email)
            .join(User, User.id == KnowledgeConflictReview.reviewer_user_id)
            .where(KnowledgeConflictReview.conflict_id == conflict.id)
        )
    ).first()
    review = (
        KnowledgeConflictReviewOut(
            reviewer_email=review_row[1],
            decision=review_row[0].decision,
            rationale=review_row[0].rationale,
            decided_at=review_row[0].decided_at,
        )
        if review_row
        else None
    )
    return KnowledgeConflictOut(
        id=conflict.id,
        category=submitted.category,
        status=conflict.status,
        detected_at=conflict.detected_at,
        submitted=await statement_out(submitted),
        existing=await statement_out(existing),
        review=review,
    )


def _conflict_cursor_encode(conflict: KnowledgeConflict) -> str:
    raw = f"{conflict.detected_at.isoformat()}|{conflict.id}"
    return urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def _conflict_cursor_decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        decoded = urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
        timestamp, conflict_id = decoded.rsplit("|", 1)
        detected_at = datetime.fromisoformat(timestamp)
        if detected_at.tzinfo is None:
            raise ValueError("timezone required")
        return detected_at, uuid.UUID(conflict_id)
    except (ValueError, UnicodeDecodeError):
        raise InvalidInputError("The conflict review cursor is invalid. Refresh the queue and try again.") from None


@router.get("/knowledge/conflicts", response_model=KnowledgeConflictListResponse)
async def knowledge_conflict_queue(
    db: DbSession, user: CurrentUser, cursor: str | None = None
) -> KnowledgeConflictListResponse:
    """Return only conflict pairs the reviewer can currently read in full and review by grant."""
    if cursor is not None:
        if not cursor or len(cursor) > 160:
            raise InvalidInputError("The conflict review cursor is invalid. Refresh the queue and try again.")
        before_time, before_id = _conflict_cursor_decode(cursor)
        page_filter = or_(
            KnowledgeConflict.detected_at < before_time,
            (KnowledgeConflict.detected_at == before_time) & (KnowledgeConflict.id < before_id),
        )
    else:
        page_filter = None
    query = select(KnowledgeConflict).order_by(KnowledgeConflict.detected_at.desc(), KnowledgeConflict.id.desc())
    if page_filter is not None:
        query = query.where(page_filter)
    conflicts = list(
        (
            await db.execute(query.limit(51))
        ).scalars()
    )
    has_more = len(conflicts) > 50
    page = conflicts[:50]
    output: list[KnowledgeConflictOut] = []
    for conflict in page:
        submitted = await db.get(KnowledgeRevision, conflict.revision_id)
        existing = await db.get(KnowledgeRevision, conflict.compared_revision_id)
        if submitted is None or existing is None or user.id in (submitted.author_user_id, existing.author_user_id):
            continue
        if submitted.status not in ("submitted", "published") or existing.status != "published":
            continue
        if not await _conflict_revision_readable(db, user, submitted) or not await _conflict_revision_readable(db, user, existing):
            continue
        if await find_active_grant(db, user.id, PERMISSION_REVIEW, submitted.category, submitted.scope_group_id) is None:
            continue
        if await find_active_grant(db, user.id, PERMISSION_REVIEW, existing.category, existing.scope_group_id) is None:
            continue
        output.append(await _conflict_out(db, conflict))
    return KnowledgeConflictListResponse(
        items=output,
        next_cursor=_conflict_cursor_encode(page[-1]) if has_more and page else None,
    )


@router.post("/knowledge/conflicts/{conflict_id}/decisions", response_model=KnowledgeConflictDecisionOut)
async def decide_knowledge_conflict(
    conflict_id: uuid.UUID,
    payload: KnowledgeConflictDecisionRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> KnowledgeConflictDecisionOut:
    conflict = (
        await db.execute(select(KnowledgeConflict).where(KnowledgeConflict.id == conflict_id).with_for_update())
    ).scalar_one_or_none()
    if conflict is None:
        raise NotFoundError("Knowledge conflict not found.")
    if conflict.status != "open":
        raise ConflictError("This potential conflict has already been reviewed.")
    submitted = await db.get(KnowledgeRevision, conflict.revision_id)
    existing = await db.get(KnowledgeRevision, conflict.compared_revision_id)
    if submitted is None or existing is None or submitted.status not in ("submitted", "published") or existing.status != "published":
        raise ConflictError("The source statements are no longer in a reviewable state.")
    if user.id in (submitted.author_user_id, existing.author_user_id):
        raise PermissionDeniedError("A contributor cannot review their own conflicting statements.")
    if not await _conflict_revision_readable(db, user, submitted) or not await _conflict_revision_readable(db, user, existing):
        raise NotFoundError("Knowledge conflict not found.")
    grant = await find_active_grant(
        db, user.id, PERMISSION_REVIEW, submitted.category, submitted.scope_group_id, lock=True
    )
    compared_grant = await find_active_grant(
        db, user.id, PERMISSION_REVIEW, existing.category, existing.scope_group_id, lock=True
    )
    if grant is None or compared_grant is None:
        raise PermissionDeniedError("An active scoped knowledge-review grant is required for both statements.")

    event = KnowledgeConflictReview(
        conflict_id=conflict.id,
        reviewer_user_id=user.id,
        permission_grant_id=grant.id,
        compared_permission_grant_id=compared_grant.id,
        decision=payload.decision,
        rationale=payload.rationale,
    )
    db.add(event)
    try:
        # The database state guard requires the append-only review event before the status changes.
        await db.flush()
        conflict.status = payload.decision
        audit.record(
            db,
            audit_context(request, user),
            "knowledge.conflict_reviewed",
            target=str(conflict.id),
            details={
                "decision": payload.decision,
                "category": submitted.category,
                "permission_grant_id": str(grant.id),
                "compared_permission_grant_id": str(compared_grant.id),
            },
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if "append-only" in str(exc.orig) or "requires a recorded review event" in str(exc.orig):
            raise ConflictError("The conflict review could not be recorded. Refresh and try again.") from None
        raise ConflictError("The conflict or reviewer permission changed. Refresh the queue and retry.") from None
    return KnowledgeConflictDecisionOut(
        conflict_id=conflict.id,
        status=event.decision,
        reviewer_email=user.email,
        rationale=event.rationale,
        decided_at=event.decided_at,
    )


POLICY_PROCESS_CATEGORIES = ("policy", "legal", "operational")


async def _process_scope_visible(db: DbSession, user: CurrentUser, scope_group_id: uuid.UUID | None) -> bool:
    if scope_group_id is None:
        return True
    return bool(await db.scalar(
        select(AccessGroupMember.group_id).where(
            AccessGroupMember.group_id == scope_group_id, AccessGroupMember.user_id == user.id
        ).union(
            select(DepartmentManager.group_id).where(
                DepartmentManager.group_id == scope_group_id, DepartmentManager.manager_user_id == user.id
            )
        ).limit(1)
    ))


async def _process_assignee_in_scope(db: DbSession, user_id: uuid.UUID, scope_group_id: uuid.UUID | None) -> bool:
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        return False
    if scope_group_id is None:
        return True
    return bool(await db.scalar(
        select(AccessGroupMember.group_id).where(
            AccessGroupMember.group_id == scope_group_id, AccessGroupMember.user_id == user_id
        ).union(
            select(DepartmentManager.group_id).where(
                DepartmentManager.group_id == scope_group_id, DepartmentManager.manager_user_id == user_id
            )
        ).limit(1)
    ))


async def _revision_readable_to_user(db: DbSession, user_id: uuid.UUID, revision: KnowledgeRevision) -> bool:
    target = await db.get(User, user_id)
    if target is None or not target.is_active:
        return False
    return bool(await db.scalar(select(KnowledgeRevision.id).where(
        KnowledgeRevision.id == revision.id,
        knowledge_readable_by(target, revision.status, include_stale=True),
    )))


async def _require_policy_reviewer(db: DbSession, user: CurrentUser, revision: KnowledgeRevision, *, lock: bool = False):
    if revision.category not in POLICY_PROCESS_CATEGORIES:
        raise InvalidInputError("Only policy, legal, or operational knowledge can be mapped to a process.")
    if await find_active_grant(
        db, user.id, PERMISSION_REVIEW, revision.category, revision.scope_group_id, lock=lock
    ) is None:
        raise PermissionDeniedError("An active scoped knowledge-review grant is required for this policy.")


async def _latest_policy_revision(
    db: DbSession,
    user: CurrentUser,
    entry_id: uuid.UUID,
    *,
    status: str = "published",
) -> KnowledgeRevision | None:
    revisions = list((await db.execute(
        select(KnowledgeRevision)
        .where(
            KnowledgeRevision.entry_id == entry_id,
            KnowledgeRevision.status == status,
            KnowledgeRevision.category.in_(POLICY_PROCESS_CATEGORIES),
            KnowledgeRevision.visibility != "private",
        )
        .order_by(KnowledgeRevision.revision_number.desc())
    )).scalars())
    for revision in revisions:
        if await _conflict_revision_readable(db, user, revision):
            return revision
    return None


async def _policy_sources(db: DbSession, revision: KnowledgeRevision) -> list[PolicyProcessSourceOut]:
    rows = list((await db.execute(
        select(KnowledgeEvidence).where(KnowledgeEvidence.revision_id == revision.id).order_by(KnowledgeEvidence.id)
    )).scalars())
    return [PolicyProcessSourceOut(filename=row.filename, page_number=row.page_number, quote=row.quote) for row in rows]


async def _policy_impact_out(
    db: DbSession, user: CurrentUser, entry_id: uuid.UUID
) -> PolicyImpactPolicyOut | None:
    published = await _latest_policy_revision(db, user, entry_id, status="published")
    if published is None:
        return None
    submitted = await _latest_policy_revision(db, user, entry_id, status="submitted")
    if submitted is not None and submitted.revision_number <= published.revision_number:
        submitted = None
    can_manage = await find_active_grant(
        db, user.id, PERMISSION_REVIEW, published.category, published.scope_group_id
    ) is not None
    return PolicyImpactPolicyOut(
        entry_id=entry_id,
        category=published.category,
        scope_group_id=published.scope_group_id,
        published_revision_id=published.id,
        published_revision_number=published.revision_number,
        published_statement=published.statement,
        published_sources=await _policy_sources(db, published),
        pending_revision_id=submitted.id if submitted else None,
        pending_revision_number=submitted.revision_number if submitted else None,
        pending_statement=submitted.statement if submitted else None,
        pending_sources=await _policy_sources(db, submitted) if submitted else [],
        can_manage=can_manage,
    )


async def _policy_candidate_out(db: DbSession, revision: KnowledgeRevision) -> PolicyCandidateOut:
    return PolicyCandidateOut(
        entry_id=revision.entry_id,
        revision_id=revision.id,
        revision_number=revision.revision_number,
        category=revision.category,
        statement=revision.statement,
        scope_group_id=revision.scope_group_id,
        sources=await _policy_sources(db, revision),
    )


async def _process_out(db: DbSession, user: CurrentUser, process: KnowledgeProcess) -> PolicyProcessOut | None:
    if not await _process_scope_visible(db, user, process.scope_group_id):
        return None
    links = list((await db.execute(
        select(KnowledgeProcessPolicyLink)
        .where(KnowledgeProcessPolicyLink.process_id == process.id, KnowledgeProcessPolicyLink.active.is_(True))
        .order_by(KnowledgeProcessPolicyLink.created_at)
    )).scalars())
    policies: list[PolicyImpactPolicyOut] = []
    for link in links:
        policy = await _policy_impact_out(db, user, link.entry_id)
        if policy is not None and (
            process.scope_group_id is not None
            and policy.scope_group_id is not None
            and process.scope_group_id != policy.scope_group_id
        ):
            continue
        if policy is not None:
            policies.append(policy)
    if not policies:
        return None
    owner = await db.get(User, process.owner_user_id)
    return PolicyProcessOut(
        id=process.id,
        name=process.name,
        description=process.description,
        scope_group_id=process.scope_group_id,
        owner_email=owner.email if owner else "",
        created_at=process.created_at,
        policies=policies,
    )


async def _exception_out(db: DbSession, exception: KnowledgePolicyException, user: CurrentUser) -> PolicyExceptionOut:
    process = await db.get(KnowledgeProcess, exception.process_id)
    revision = await db.get(KnowledgeRevision, exception.revision_id)
    assignee = await db.get(User, exception.assigned_to)
    creator = await db.get(User, exception.created_by)
    if process is None or revision is None or assignee is None or creator is None:
        raise NotFoundError("Policy exception not found.")
    event_rows = list((await db.execute(
        select(KnowledgePolicyExceptionEvent, User.email)
        .join(User, User.id == KnowledgePolicyExceptionEvent.actor_user_id)
        .where(KnowledgePolicyExceptionEvent.exception_id == exception.id)
        .order_by(KnowledgePolicyExceptionEvent.id)
    )).all())
    events: list[PolicyExceptionEventOut] = []
    for event, actor_email in event_rows:
        from_user = await db.get(User, event.from_assignee_id) if event.from_assignee_id else None
        to_user = await db.get(User, event.to_assignee_id)
        events.append(PolicyExceptionEventOut(
            action=event.action,
            actor_email=actor_email,
            from_assignee_email=from_user.email if from_user else None,
            to_assignee_email=to_user.email if to_user else "",
            from_status=event.from_status,
            to_status=event.to_status,
            rationale=event.rationale,
            created_at=event.created_at,
        ))
    return PolicyExceptionOut(
        id=exception.id,
        process_id=process.id,
        process_name=process.name,
        entry_id=exception.entry_id,
        revision_id=revision.id,
        policy_statement=revision.statement,
        status=exception.status,
        assigned_to=PolicyProcessAssigneeOut(user_id=assignee.id, email=assignee.email, role=assignee.role),
        created_by_email=creator.email,
        rationale=exception.rationale,
        due_at=exception.due_at,
        created_at=exception.created_at,
        events=events,
        can_acknowledge=user.id == exception.assigned_to and exception.status == "open",
        can_resolve=user.id == exception.assigned_to and exception.status in ("open", "acknowledged"),
        can_reassign=(
            exception.status != "resolved"
            and await find_active_grant(db, user.id, PERMISSION_REVIEW, revision.category, revision.scope_group_id) is not None
        ),
    )


@router.get("/policy-candidates", response_model=PolicyCandidateListResponse)
async def policy_process_candidates(db: DbSession, user: CurrentUser) -> PolicyCandidateListResponse:
    """Policies a reviewer can safely map, with citations kept behind current source ACLs."""
    rows = list((await db.execute(
        select(KnowledgeRevision)
        .where(
            KnowledgeRevision.status == "published",
            KnowledgeRevision.category.in_(POLICY_PROCESS_CATEGORIES),
            KnowledgeRevision.visibility != "private",
        )
        .order_by(KnowledgeRevision.entry_id, KnowledgeRevision.revision_number.desc())
    )).scalars())
    seen: set[uuid.UUID] = set()
    output: list[PolicyCandidateOut] = []
    for revision in rows:
        if revision.entry_id in seen:
            continue
        seen.add(revision.entry_id)
        if not await _conflict_revision_readable(db, user, revision):
            continue
        if await find_active_grant(db, user.id, PERMISSION_REVIEW, revision.category, revision.scope_group_id) is None:
            continue
        output.append(await _policy_candidate_out(db, revision))
        if len(output) >= 100:
            break
    return PolicyCandidateListResponse(items=output)


def _policy_process_cursor_encode(process: KnowledgeProcess) -> str:
    raw = f"{process.created_at.isoformat()}|{process.id}"
    return urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def _policy_process_cursor_decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        decoded = urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
        timestamp, process_id = decoded.rsplit("|", 1)
        created_at = datetime.fromisoformat(timestamp)
        if created_at.tzinfo is None:
            raise ValueError("timezone required")
        return created_at, uuid.UUID(process_id)
    except (ValueError, UnicodeDecodeError):
        raise InvalidInputError("The process map cursor is invalid. Refresh and try again.") from None


@router.get("/processes", response_model=PolicyProcessListResponse)
async def list_policy_processes(
    db: DbSession, user: CurrentUser, cursor: str | None = None
) -> PolicyProcessListResponse:
    page_filter = None
    if cursor is not None:
        if not cursor or len(cursor) > 160:
            raise InvalidInputError("The process map cursor is invalid. Refresh and try again.")
        before_time, before_id = _policy_process_cursor_decode(cursor)
        page_filter = or_(
            KnowledgeProcess.created_at < before_time,
            (KnowledgeProcess.created_at == before_time) & (KnowledgeProcess.id < before_id),
        )
    query = select(KnowledgeProcess).where(KnowledgeProcess.archived_at.is_(None))
    if page_filter is not None:
        query = query.where(page_filter)
    processes = list((await db.execute(
        query.order_by(KnowledgeProcess.created_at.desc(), KnowledgeProcess.id.desc()).limit(51)
    )).scalars())
    has_more = len(processes) > 50
    page = processes[:50]
    output: list[PolicyProcessOut] = []
    for process in page:
        result = await _process_out(db, user, process)
        if result is not None:
            output.append(result)
    return PolicyProcessListResponse(
        items=output,
        next_cursor=_policy_process_cursor_encode(page[-1]) if has_more and page else None,
    )


@router.post("/processes", response_model=PolicyProcessOut, status_code=201)
async def create_policy_process(
    payload: PolicyProcessCreateRequest, request: Request, db: DbSession, user: CurrentUser
) -> PolicyProcessOut:
    revision = await _latest_policy_revision(db, user, payload.policy_entry_id)
    if revision is None:
        raise NotFoundError("A readable published policy is required to create a process impact link.")
    await _require_policy_reviewer(db, user, revision)
    owner = user
    process = KnowledgeProcess(
        name=payload.name,
        description=payload.description,
        scope_group_id=revision.scope_group_id,
        owner_user_id=owner.id,
        created_by=user.id,
    )
    db.add(process)
    await db.flush()
    db.add(KnowledgeProcessPolicyLink(process_id=process.id, entry_id=revision.entry_id, linked_by=user.id))
    audit.record(
        db, audit_context(request, user), "knowledge.process_created", target=str(process.id),
        details={"policy_entry_id": str(revision.entry_id), "category": revision.category, "scope_group_id": str(revision.scope_group_id) if revision.scope_group_id else None},
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("The process could not be linked to that current policy. Refresh and try again.") from None
    await db.refresh(process)
    result = await _process_out(db, user, process)
    if result is None:
        raise NotFoundError("The process is no longer visible.")
    return result


@router.post("/processes/{process_id}/policies", response_model=PolicyProcessOut)
async def link_policy_to_process(
    process_id: uuid.UUID, payload: PolicyProcessLinkRequest, request: Request, db: DbSession, user: CurrentUser
) -> PolicyProcessOut:
    process = await db.get(KnowledgeProcess, process_id)
    if process is None or process.archived_at is not None or not await _process_scope_visible(db, user, process.scope_group_id):
        raise NotFoundError("Process not found.")
    revision = await _latest_policy_revision(db, user, payload.entry_id)
    if revision is None:
        raise NotFoundError("A readable published policy is required.")
    await _require_policy_reviewer(db, user, revision)
    if process.scope_group_id is None and revision.scope_group_id is not None:
        raise InvalidInputError("A shared process cannot be linked to a department-restricted policy.")
    if process.scope_group_id is not None and revision.scope_group_id not in (None, process.scope_group_id):
        raise InvalidInputError("The policy and process belong to different department scopes.")
    link = (await db.execute(select(KnowledgeProcessPolicyLink).where(
        KnowledgeProcessPolicyLink.process_id == process.id,
        KnowledgeProcessPolicyLink.entry_id == revision.entry_id,
    ).with_for_update())).scalar_one_or_none()
    if link is None:
        link = KnowledgeProcessPolicyLink(process_id=process.id, entry_id=revision.entry_id, linked_by=user.id)
        db.add(link)
    elif not link.active:
        link.active = True
        link.updated_at = datetime.now(UTC)
    else:
        return await _process_out(db, user, process)  # idempotent link operation
    audit.record(db, audit_context(request, user), "knowledge.process_policy_linked", target=str(process.id), details={
        "policy_entry_id": str(revision.entry_id), "category": revision.category,
    })
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("The policy relationship changed. Refresh and retry.") from None
    return await _process_out(db, user, process)


@router.delete("/processes/{process_id}/policies/{entry_id}", response_model=PolicyProcessOut)
async def unlink_policy_from_process(
    process_id: uuid.UUID, entry_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser
) -> PolicyProcessOut:
    process = await db.get(KnowledgeProcess, process_id)
    if process is None or process.archived_at is not None or not await _process_scope_visible(db, user, process.scope_group_id):
        raise NotFoundError("Process not found.")
    link = (await db.execute(select(KnowledgeProcessPolicyLink).where(
        KnowledgeProcessPolicyLink.process_id == process.id,
        KnowledgeProcessPolicyLink.entry_id == entry_id,
        KnowledgeProcessPolicyLink.active.is_(True),
    ).with_for_update())).scalar_one_or_none()
    if link is None:
        raise NotFoundError("Active policy relationship not found.")
    revision = await _latest_policy_revision(db, user, entry_id)
    if revision is None:
        raise NotFoundError("Policy not found.")
    await _require_policy_reviewer(db, user, revision)
    link.active = False
    link.updated_at = datetime.now(UTC)
    audit.record(db, audit_context(request, user), "knowledge.process_policy_unlinked", target=str(process.id), details={
        "policy_entry_id": str(entry_id), "category": revision.category,
    })
    await db.commit()
    result = await _process_out(db, user, process)
    return result or PolicyProcessOut(
        id=process.id, name=process.name, description=process.description,
        scope_group_id=process.scope_group_id, owner_email=user.email, created_at=process.created_at, policies=[],
    )


@router.get("/processes/{process_id}/assignees", response_model=PolicyProcessAssigneeListResponse)
async def policy_process_assignees(
    process_id: uuid.UUID, entry_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> PolicyProcessAssigneeListResponse:
    process = await db.get(KnowledgeProcess, process_id)
    if process is None or process.archived_at is not None or not await _process_scope_visible(db, user, process.scope_group_id):
        raise NotFoundError("Process not found.")
    if not await db.scalar(select(KnowledgeProcessPolicyLink.id).where(
        KnowledgeProcessPolicyLink.process_id == process.id,
        KnowledgeProcessPolicyLink.entry_id == entry_id,
        KnowledgeProcessPolicyLink.active.is_(True),
    )):
        raise NotFoundError("Active process policy link not found.")
    revision = await _latest_policy_revision(db, user, entry_id)
    if revision is None:
        raise NotFoundError("Policy not found.")
    await _require_policy_reviewer(db, user, revision)
    query = select(User).where(User.is_active.is_(True))
    if process.scope_group_id is not None:
        scoped_ids = select(AccessGroupMember.user_id).where(AccessGroupMember.group_id == process.scope_group_id).union(
            select(DepartmentManager.manager_user_id).where(DepartmentManager.group_id == process.scope_group_id)
        )
        query = query.where(User.id.in_(scoped_ids))
    # Keep the canonical knowledge ACL predicate authoritative. Reimplementing it as a
    # separate SQL expression allowed an ACL-revoked user into the picker because the
    # evidence/source subquery was not correlated to the candidate user as intended.
    # Keyset-paginate active candidates so large user tables do not require one query
    # or an unbounded in-memory list per account; stop after the response cap.
    users: list[User] = []
    after_email: str | None = None
    while len(users) < 200:
        page_query = query
        if after_email is not None:
            page_query = page_query.where(User.email > after_email)
        page = list((await db.execute(page_query.order_by(User.email).limit(200))).scalars())
        if not page:
            break
        for candidate in page:
            if await _revision_readable_to_user(db, candidate.id, revision):
                users.append(candidate)
                if len(users) == 200:
                    break
        after_email = page[-1].email
        if len(page) < 200:
            break
    return PolicyProcessAssigneeListResponse(items=[PolicyProcessAssigneeOut(user_id=item.id, email=item.email, role=item.role) for item in users])


@router.post("/processes/{process_id}/exceptions", response_model=PolicyExceptionOut, status_code=201)
async def assign_policy_exception(
    process_id: uuid.UUID,
    payload: PolicyExceptionCreateRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> PolicyExceptionOut:
    process = await db.get(KnowledgeProcess, process_id)
    if process is None or process.archived_at is not None or not await _process_scope_visible(db, user, process.scope_group_id):
        raise NotFoundError("Process not found.")
    if not await db.scalar(select(KnowledgeProcessPolicyLink.id).where(
        KnowledgeProcessPolicyLink.process_id == process.id,
        KnowledgeProcessPolicyLink.entry_id == payload.entry_id,
        KnowledgeProcessPolicyLink.active.is_(True),
    )):
        raise NotFoundError("Active process policy link not found.")
    revisions = list((await db.execute(select(KnowledgeRevision).where(
        KnowledgeRevision.entry_id == payload.entry_id,
        KnowledgeRevision.status.in_(("submitted", "published")),
        KnowledgeRevision.category.in_(POLICY_PROCESS_CATEGORIES),
    ).order_by(KnowledgeRevision.revision_number.desc()))).scalars())
    revision = None
    for candidate in revisions:
        if await _conflict_revision_readable(db, user, candidate):
            revision = candidate
            break
    if revision is None:
        raise NotFoundError("A readable submitted or published policy is required.")
    await _require_policy_reviewer(db, user, revision, lock=True)
    if process.scope_group_id is None and revision.scope_group_id is not None:
        raise InvalidInputError("A shared process cannot receive an exception for a department-restricted policy.")
    if process.scope_group_id is not None and revision.scope_group_id not in (None, process.scope_group_id):
        raise InvalidInputError("The policy and process belong to different department scopes.")
    if not await _process_assignee_in_scope(db, payload.assigned_to, process.scope_group_id):
        raise InvalidInputError("Choose an active assignee in the process department.")
    if not await _revision_readable_to_user(db, payload.assigned_to, revision):
        raise InvalidInputError("Choose an assignee who currently has access to the linked policy source.")
    due_at = payload.due_at
    if due_at is not None and (due_at.tzinfo is None or due_at <= datetime.now(UTC)):
        raise InvalidInputError("The due date must include a timezone and be in the future.")
    exception = KnowledgePolicyException(
        process_id=process.id, entry_id=payload.entry_id, revision_id=revision.id,
        assigned_to=payload.assigned_to, created_by=user.id, status="open",
        rationale=payload.rationale, due_at=due_at,
    )
    db.add(exception)
    try:
        await db.flush()
        db.add(KnowledgePolicyExceptionEvent(
            exception_id=exception.id, actor_user_id=user.id, action="assigned",
            from_status=None, to_status="open", from_assignee_id=None,
            to_assignee_id=payload.assigned_to, rationale=payload.rationale,
        ))
        audit.record(db, audit_context(request, user), "knowledge.policy_exception_assigned", target=str(exception.id), details={
            "process_id": str(process.id), "policy_entry_id": str(payload.entry_id),
            "revision_id": str(revision.id), "assigned_to": str(payload.assigned_to),
            "category": revision.category,
        })
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("The policy exception assignment changed or the assignee is outside its scope.") from None
    await db.refresh(exception)
    return await _exception_out(db, exception, user)


@router.get("/policy-exceptions", response_model=PolicyExceptionListResponse)
async def list_policy_exceptions(db: DbSession, user: CurrentUser) -> PolicyExceptionListResponse:
    rows = list((await db.execute(
        select(KnowledgePolicyException).order_by(KnowledgePolicyException.created_at.desc()).limit(501)
    )).scalars())
    output: list[PolicyExceptionOut] = []
    for exception in rows:
        process = await db.get(KnowledgeProcess, exception.process_id)
        revision = await db.get(KnowledgeRevision, exception.revision_id)
        if process is None or revision is None or not await _process_scope_visible(db, user, process.scope_group_id):
            continue
        if not await _conflict_revision_readable(db, user, revision):
            continue
        assigned_or_creator = user.id in (exception.assigned_to, exception.created_by)
        reviewer = await find_active_grant(db, user.id, PERMISSION_REVIEW, revision.category, revision.scope_group_id)
        if not assigned_or_creator and reviewer is None:
            continue
        output.append(await _exception_out(db, exception, user))
        if len(output) >= 500:
            break
    return PolicyExceptionListResponse(items=output)


@router.post("/policy-exceptions/{exception_id}/actions", response_model=PolicyExceptionOut)
async def act_on_policy_exception(
    exception_id: uuid.UUID,
    payload: PolicyExceptionActionRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> PolicyExceptionOut:
    exception = (await db.execute(
        select(KnowledgePolicyException).where(KnowledgePolicyException.id == exception_id).with_for_update()
    )).scalar_one_or_none()
    if exception is None:
        raise NotFoundError("Policy exception not found.")
    process = await db.get(KnowledgeProcess, exception.process_id)
    revision = await db.get(KnowledgeRevision, exception.revision_id)
    if process is None or revision is None or not await _process_scope_visible(db, user, process.scope_group_id):
        raise NotFoundError("Policy exception not found.")
    if not await _conflict_revision_readable(db, user, revision):
        raise NotFoundError("Policy exception not found.")
    old_assignee = exception.assigned_to
    new_assignee = old_assignee
    new_status = exception.status
    if payload.action == "reassign":
        if payload.assigned_to is None:
            raise InvalidInputError("Choose an active in-scope assignee.")
        await _require_policy_reviewer(db, user, revision, lock=True)
        if not await _process_assignee_in_scope(db, payload.assigned_to, process.scope_group_id):
            raise InvalidInputError("Choose an active assignee in the process department.")
        if not await _revision_readable_to_user(db, payload.assigned_to, revision):
            raise InvalidInputError("Choose an assignee who currently has access to the linked policy source.")
        if exception.status == "resolved" or payload.assigned_to == old_assignee:
            raise ConflictError("Only an open exception can be reassigned to a different person.")
        new_assignee = payload.assigned_to
    else:
        if user.id != old_assignee:
            raise PermissionDeniedError("Only the currently assigned employee can acknowledge or resolve this exception.")
        if payload.assigned_to is not None:
            raise InvalidInputError("An assignee is only accepted for reassignment.")
        if payload.action == "acknowledge":
            if exception.status != "open":
                raise ConflictError("Only an open policy exception can be acknowledged.")
            new_status = "acknowledged"
        else:
            if exception.status not in ("open", "acknowledged"):
                raise ConflictError("This policy exception is already resolved.")
            new_status = "resolved"
    event_action = {"acknowledge": "acknowledged", "resolve": "resolved", "reassign": "reassigned"}[payload.action]
    event = KnowledgePolicyExceptionEvent(
        exception_id=exception.id, actor_user_id=user.id, action=event_action,
        from_status=exception.status, to_status=new_status,
        from_assignee_id=old_assignee, to_assignee_id=new_assignee, rationale=payload.rationale,
    )
    db.add(event)
    try:
        await db.flush()
        exception.status = new_status
        exception.assigned_to = new_assignee
        exception.updated_at = datetime.now(UTC)
        audit.record(db, audit_context(request, user), f"knowledge.policy_exception_{event_action}", target=str(exception.id), details={
            "process_id": str(process.id), "policy_entry_id": str(exception.entry_id),
            "status": new_status, "assignee_id": str(new_assignee), "category": revision.category,
        })
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("The policy exception changed. Refresh and retry.") from None
    await db.refresh(exception)
    return await _exception_out(db, exception, user)
