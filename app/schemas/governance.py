import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.claims import SourceOut
from app.schemas.knowledge import Category, Visibility


class GovernanceDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    stage: Literal["review", "publish"]
    rationale: str = Field(..., min_length=3, max_length=2000)


class GovernanceDecisionOut(BaseModel):
    stage: Literal["review", "publish"]
    approver_email: str
    rationale: str
    decided_at: datetime


class GovernanceClaimOut(BaseModel):
    id: uuid.UUID
    kind: Literal["proposal", "correction"]
    statement: str
    claimant_email: str
    category: Literal["policy", "financial", "legal", "client", "operational", "general"]
    scope_group_id: uuid.UUID | None
    sources: list[SourceOut]
    created_at: datetime
    decisions: list[GovernanceDecisionOut]
    allowed_stages: list[Literal["review", "publish"]]


class GovernanceClaimListResponse(BaseModel):
    items: list[GovernanceClaimOut]


class GovernanceKnowledgeSourceOut(BaseModel):
    filename: str
    page_number: int
    quote: str


class GovernanceKnowledgeOut(BaseModel):
    id: uuid.UUID
    entry_id: uuid.UUID
    statement: str
    author_email: str
    category: Category
    visibility: Visibility
    scope_group_id: uuid.UUID | None
    created_at: datetime
    sources: list[GovernanceKnowledgeSourceOut]
    decisions: list[GovernanceDecisionOut]
    allowed_stages: list[Literal["review", "publish"]]


class GovernanceKnowledgeListResponse(BaseModel):
    items: list[GovernanceKnowledgeOut]


class KnowledgeFreshnessReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rationale: str = Field(..., min_length=3, max_length=2000)


class KnowledgeFreshnessHistoryOut(BaseModel):
    reviewer_email: str
    rationale: str
    reviewed_at: datetime
    next_review_due_at: datetime


class StaleKnowledgeOut(BaseModel):
    id: uuid.UUID
    entry_id: uuid.UUID
    statement: str
    author_email: str
    category: Category
    visibility: Visibility
    scope_group_id: uuid.UUID | None
    created_at: datetime
    last_reviewed_at: datetime | None
    review_due_at: datetime
    sources: list[GovernanceKnowledgeSourceOut]
    last_freshness_review: KnowledgeFreshnessHistoryOut | None


class StaleKnowledgeListResponse(BaseModel):
    items: list[StaleKnowledgeOut]


class KnowledgeFreshnessReviewOut(KnowledgeFreshnessHistoryOut):
    revision_id: uuid.UUID


class KnowledgeConflictDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["confirmed", "dismissed"]
    rationale: str = Field(..., min_length=3, max_length=2000)


class KnowledgeConflictSourceOut(BaseModel):
    filename: str
    page_number: int
    quote: str


class KnowledgeConflictStatementOut(BaseModel):
    revision_id: uuid.UUID
    statement: str
    author_email: str
    visibility: Visibility
    scope_group_id: uuid.UUID | None
    sources: list[KnowledgeConflictSourceOut]


class KnowledgeConflictReviewOut(BaseModel):
    reviewer_email: str
    decision: Literal["confirmed", "dismissed"]
    rationale: str
    decided_at: datetime


class KnowledgeConflictOut(BaseModel):
    id: uuid.UUID
    category: Category
    status: Literal["open", "confirmed", "dismissed"]
    detected_at: datetime
    submitted: KnowledgeConflictStatementOut
    existing: KnowledgeConflictStatementOut
    review: KnowledgeConflictReviewOut | None


class KnowledgeConflictListResponse(BaseModel):
    items: list[KnowledgeConflictOut]
    next_cursor: str | None = None


class KnowledgeConflictDecisionOut(BaseModel):
    conflict_id: uuid.UUID
    status: Literal["confirmed", "dismissed"]
    reviewer_email: str
    rationale: str
    decided_at: datetime


class PolicyProcessCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=180)
    description: str = Field(default="", max_length=2000)
    policy_entry_id: uuid.UUID


class PolicyProcessLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: uuid.UUID


class PolicyProcessSourceOut(BaseModel):
    filename: str
    page_number: int
    quote: str


class PolicyImpactPolicyOut(BaseModel):
    entry_id: uuid.UUID
    category: Literal["policy", "legal", "operational"]
    scope_group_id: uuid.UUID | None
    published_revision_id: uuid.UUID
    published_revision_number: int
    published_statement: str
    published_sources: list[PolicyProcessSourceOut]
    pending_revision_id: uuid.UUID | None
    pending_revision_number: int | None
    pending_statement: str | None
    pending_sources: list[PolicyProcessSourceOut]
    can_manage: bool


class PolicyProcessOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    scope_group_id: uuid.UUID | None
    owner_email: str
    created_at: datetime
    policies: list[PolicyImpactPolicyOut]


class PolicyProcessListResponse(BaseModel):
    items: list[PolicyProcessOut]
    next_cursor: str | None = None


class PolicyCandidateOut(BaseModel):
    entry_id: uuid.UUID
    revision_id: uuid.UUID
    revision_number: int
    category: Literal["policy", "legal", "operational"]
    statement: str
    scope_group_id: uuid.UUID | None
    sources: list[PolicyProcessSourceOut]


class PolicyCandidateListResponse(BaseModel):
    items: list[PolicyCandidateOut]


class PolicyProcessAssigneeOut(BaseModel):
    user_id: uuid.UUID
    email: str
    role: str


class PolicyProcessAssigneeListResponse(BaseModel):
    items: list[PolicyProcessAssigneeOut]


class PolicyExceptionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    entry_id: uuid.UUID
    assigned_to: uuid.UUID
    rationale: str = Field(..., min_length=3, max_length=2000)
    due_at: datetime | None = None


class PolicyExceptionActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Literal["acknowledge", "resolve", "reassign"]
    rationale: str = Field(..., min_length=3, max_length=2000)
    assigned_to: uuid.UUID | None = None


class PolicyExceptionEventOut(BaseModel):
    action: Literal["assigned", "reassigned", "acknowledged", "resolved"]
    actor_email: str
    from_assignee_email: str | None
    to_assignee_email: str
    from_status: Literal["open", "acknowledged", "resolved"] | None
    to_status: Literal["open", "acknowledged", "resolved"]
    rationale: str
    created_at: datetime


class PolicyExceptionOut(BaseModel):
    id: uuid.UUID
    process_id: uuid.UUID
    process_name: str
    entry_id: uuid.UUID
    revision_id: uuid.UUID
    policy_statement: str
    status: Literal["open", "acknowledged", "resolved"]
    assigned_to: PolicyProcessAssigneeOut
    created_by_email: str
    rationale: str
    due_at: datetime | None
    created_at: datetime
    events: list[PolicyExceptionEventOut]
    can_acknowledge: bool
    can_resolve: bool
    can_reassign: bool


class PolicyExceptionListResponse(BaseModel):
    items: list[PolicyExceptionOut]
