"""Input and output contracts for internal RFP/tender evidence maps."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from app.schemas.rag import Citation, ModelChoice


def _clean_text(value: str) -> str:
    return " ".join(value.split())


class ProposalRequirementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement: str = Field(min_length=5, max_length=2000)
    source_chunk_ids: list[StrictInt] = Field(default_factory=list, max_length=5)

    @field_validator("source_chunk_ids")
    @classmethod
    def unique_source_chunks(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("Use each RFP source passage only once per requirement.")
        return value

    @field_validator("requirement")
    @classmethod
    def normalize_requirement(cls, value: str) -> str:
        value = _clean_text(value)
        if len(value) < 5:
            raise ValueError("Enter a requirement with at least five characters.")
        return value


class ProposalCoverageCreate(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    department_group_id: uuid.UUID | None = None
    requirements: list[ProposalRequirementIn] = Field(min_length=1, max_length=30)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        value = _clean_text(value)
        if not value:
            raise ValueError("Enter a case title.")
        return value

    @field_validator("requirements")
    @classmethod
    def unique_requirements(cls, value: list[ProposalRequirementIn]) -> list[ProposalRequirementIn]:
        keys = [item.requirement.casefold() for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("Remove duplicate requirements before saving the coverage case.")
        return value


class ProposalCoverageUpdate(ProposalCoverageCreate):
    pass


class ProposalAnalyzeRequest(ModelChoice):
    document_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)


class ProposalRequirementExtractRequest(ModelChoice):
    model_config = ConfigDict(extra="forbid")

    document_ids: list[uuid.UUID] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def unique_documents(self):
        if len(self.document_ids) != len(set(self.document_ids)):
            raise ValueError("Select each RFP source document only once.")
        return self


class ProposalRequirementCandidateDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    requirement: str = Field(min_length=5, max_length=2000)
    evidence_chunk_ids: list[StrictInt] = Field(min_length=1, max_length=5)


class ProposalRequirementExtractionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirements: list[ProposalRequirementCandidateDraft] = Field(default_factory=list, max_length=30)


class ProposalRequirementEvidenceOut(BaseModel):
    chunk_id: int
    document_id: uuid.UUID
    document_version: int
    document_sha256: str
    filename: str
    location: str = Field(max_length=300)
    excerpt: str = Field(max_length=1000)


class ProposalRequirementCandidateOut(BaseModel):
    requirement: str
    evidence: list[ProposalRequirementEvidenceOut] = Field(min_length=1, max_length=5)


class ProposalRequirementExtractOut(BaseModel):
    source_document_count: int = Field(ge=1, le=5)
    candidates: list[ProposalRequirementCandidateOut] = Field(max_length=30)


class ProposalAssessmentRequest(BaseModel):
    assessment: Literal["meets", "partial", "gap"]
    rationale: str = Field(min_length=3, max_length=2000)

    @field_validator("rationale")
    @classmethod
    def normalize_rationale(cls, value: str) -> str:
        value = _clean_text(value)
        if len(value) < 3:
            raise ValueError("Add a rationale of at least three characters.")
        return value


class ProposalCoverageReviewRequest(BaseModel):
    decision: Literal["approved", "changes_requested"]
    rationale: str = Field(min_length=3, max_length=2000)

    @field_validator("rationale")
    @classmethod
    def normalize_rationale(cls, value: str) -> str:
        value = _clean_text(value)
        if len(value) < 3:
            raise ValueError("Add a review rationale of at least three characters.")
        return value


class ProposalRequirementOut(BaseModel):
    id: uuid.UUID
    position: int
    requirement: str
    answer: str
    insufficient_evidence: bool
    retrieved_chunks: int
    analyzed_at: datetime | None
    evidence_current: bool
    citations: list[Citation]
    origin_evidence: list[ProposalRequirementEvidenceOut]
    origin_evidence_current: bool
    assessment: Literal["not_assessed", "meets", "partial", "gap"]
    assessment_rationale: str
    assessed_by_user_id: uuid.UUID | None
    assessed_at: datetime | None


class ProposalCoverageReviewOut(BaseModel):
    id: int
    revision_number: int
    reviewer_user_id: uuid.UUID
    reviewer_email: str
    decision: Literal["approved", "changes_requested"]
    rationale: str
    reviewed_at: datetime


class ProposalCoverageSummary(BaseModel):
    id: uuid.UUID
    title: str
    status: Literal["draft", "submitted", "changes_requested", "approved", "withdrawn"]
    revision_number: int
    owner_user_id: uuid.UUID
    owner_email: str
    department_group_id: uuid.UUID | None
    department_name: str | None
    requirement_count: int
    analyzed_count: int
    assessment_count: int
    created_at: datetime
    updated_at: datetime


class ProposalCoverageDetail(ProposalCoverageSummary):
    submitted_at: datetime | None
    decided_at: datetime | None
    requirements: list[ProposalRequirementOut]
    reviews: list[ProposalCoverageReviewOut]
    can_edit: bool
    can_submit: bool
    can_review: bool


class ProposalCoverageListOut(BaseModel):
    cases: list[ProposalCoverageSummary]


class ProposalCoverageOptions(BaseModel):
    departments: list[dict[str, str]]
