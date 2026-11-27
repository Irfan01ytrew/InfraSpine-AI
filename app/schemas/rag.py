import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from app.db.models import Document, User
from app.rag import access


class DocumentOut(BaseModel):
    id: uuid.UUID
    filename: str
    file_format: Literal["pdf", "docx", "xlsx", "pptx", "txt", "csv", "md", "json", "html", "png", "jpeg", "webp", "audio", "video", "doc", "xls", "ppt"] = "pdf"
    visibility: Literal["private", "shared", "restricted"]
    source_system: Literal["upload", "file_server"]
    status: Literal["pending", "processing", "ready", "failed"]
    version: int
    size_bytes: int
    page_count: int | None
    chunk_count: int
    attempts: int
    error_code: str | None
    error_message: str | None
    searchable: bool = Field(description="True when an indexed version is available for retrieval.")
    indexed_version: int | None
    indexed_at: datetime | None
    is_owner: bool
    can_modify: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def build(
        cls, doc: Document, user: User, embedding_model: str, *, can_modify: bool | None = None
    ) -> "DocumentOut":
        return cls(
            id=doc.id,
            filename=doc.filename,
            file_format=doc.file_format,
            visibility=doc.visibility,
            source_system=doc.source_system,
            status=doc.status,
            version=doc.version,
            size_bytes=doc.size_bytes,
            page_count=doc.page_count,
            chunk_count=doc.chunk_count,
            attempts=doc.attempts,
            error_code=doc.error_code,
            error_message=doc.error_message,
            searchable=doc.indexed_version is not None and doc.embedding_model == embedding_model,
            indexed_version=doc.indexed_version,
            indexed_at=doc.indexed_at,
            is_owner=doc.owner_id == user.id,
            can_modify=access.can_modify(user, doc) if can_modify is None else can_modify,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
        )


class DocumentVersionOut(BaseModel):
    version: int
    filename: str
    file_format: Literal["pdf", "docx", "xlsx", "pptx", "txt", "csv", "md", "json", "html", "png", "jpeg", "webp", "audio", "video", "doc", "xls", "ppt"]
    sha256: str
    size_bytes: int
    archived_at: datetime
    reason: Literal["replacement", "folder_sync"]


class DocumentDiffLine(BaseModel):
    kind: Literal["added", "removed"]
    text: str = Field(max_length=1200)


class DocumentObligationImpactOut(BaseModel):
    id: uuid.UUID
    title: str
    status: Literal["open", "acknowledged", "completed"]
    source_version: StrictInt = Field(ge=1)
    source_quote: str
    quote_found_in_to_version: bool


class DocumentVersionDiffOut(BaseModel):
    document_id: uuid.UUID
    filename: str
    from_version: int
    to_version: int
    from_sha256: str
    to_sha256: str
    method: Literal["deterministic_text_diff"]
    added_lines: int
    removed_lines: int
    changes: list[DocumentDiffLine]
    truncated: bool
    obligation_impacts: list[DocumentObligationImpactOut] = Field(default_factory=list, max_length=100)


class IngestionStatus(BaseModel):
    """Whether queued documents will be processed. In "worker" mode a separate worker process must be running."""

    mode: Literal["worker", "embedded"]
    workers_online: int = Field(description="Worker processes (or embedded loops) with a recent heartbeat.")


class FolderImportProgress(BaseModel):
    source_id: str
    total_documents: int
    queued: int
    processing: int
    ready: int
    failed: int
    total_bytes: int
    last_updated: datetime | None


class DocumentListResponse(BaseModel):
    documents: list[DocumentOut]
    max_upload_mb: int
    embedding_model: str
    ingestion: IngestionStatus | None = None


class ModelChoice(BaseModel):
    """Optional per-chat model pick. Both or neither; the server checks it is a configured, keyed model."""

    provider: str | None = Field(default=None, min_length=1, max_length=120)
    model: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _both_or_neither(self):
        if (self.provider is None) != (self.model is None):
            raise ValueError("provider and model must be given together")
        return self

    def model_choice(self) -> tuple[str, str] | None:
        return (self.provider, self.model) if self.provider and self.model else None


class ModelOptionOut(BaseModel):
    provider: str
    provider_label: str
    model: str
    model_label: str
    is_default: bool


class ModelOptionsResponse(BaseModel):
    options: list[ModelOptionOut]


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(..., min_length=1, max_length=4000)
    answer: str = Field(..., max_length=8000)


class RagChatRequest(ModelChoice):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(..., min_length=1, max_length=4000)
    document_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    # Conversation context (client-side, untrusted): recent turns and documents cited in the last answer.
    history: list[ChatTurn] = Field(default_factory=list, max_length=6)
    focus_document_ids: list[uuid.UUID] | None = Field(default=None, max_length=20)


class RagResearchRequest(ModelChoice):
    """A bounded multi-question research run, optionally limited to readable documents."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(..., min_length=1, max_length=4000)
    document_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)


class RagTrainingRequest(ModelChoice):
    """A short internal training package, optionally limited to readable documents."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    topic: str = Field(..., min_length=2, max_length=2000)
    document_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)


class Citation(BaseModel):
    source_id: str  # matches the [S<n>] tag in the answer
    document_id: uuid.UUID
    filename: str
    page_number: int  # the page for PDFs; for other formats the locator's position (slide, sheet, ...)
    # Structured location, e.g. {"kind": "sheet", "sheet": "Q1", "range": "A5:C40", "label": "Sheet 'Q1' A5:C40"}.
    # Absent on citations saved before locators existed (those are PDF pages).
    locator: dict[str, Any] | None = None
    location: str | None = None  # the locator's human-readable label
    chunk_index: int
    score: float
    snippet: str
    # False when shown from a saved conversation and the user can no longer read the document
    # (deleted or access removed): the document's text is withheld.
    available: bool = True


class ResearchFinding(BaseModel):
    specialist: Literal["evidence_finder", "policy_analyst", "exception_checker"]
    question: str
    answer: str
    insufficient_evidence: bool
    citations: list[Citation]
    retrieved_chunks: int


class ResearchReport(BaseModel):
    question: str
    findings: list[ResearchFinding]
    model: str | None
    provider: str | None
    grounded_only: bool = True


class TrainingModule(BaseModel):
    title: str
    question: str
    answer: str
    insufficient_evidence: bool
    citations: list[Citation]
    retrieved_chunks: int


class TrainingQuizQuestion(BaseModel):
    question: str = Field(..., min_length=5, max_length=600)
    choices: list[str] = Field(..., min_length=2, max_length=5)
    citations: list[Citation] = Field(..., min_length=1, max_length=5)


class TrainingPlan(BaseModel):
    id: uuid.UUID | None = None
    topic: str
    modules: list[TrainingModule]
    quiz: list[TrainingQuizQuestion] = Field(default_factory=list, max_length=10)
    quiz_required: bool = False
    model: str | None
    provider: str | None
    grounded_only: bool = True
    status: Literal["in_progress", "completed"] = "in_progress"
    created_at: datetime | None = None
    completed_at: datetime | None = None


class TrainingProgressItem(BaseModel):
    id: uuid.UUID
    topic: str
    status: Literal["in_progress", "completed"]
    module_count: int
    created_at: datetime
    completed_at: datetime | None
    assigned: bool = False
    assignment_id: uuid.UUID | None = None
    due_at: datetime | None = None


class TrainingProgressListResponse(BaseModel):
    items: list[TrainingProgressItem]


class TrainingQuizSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: list[StrictInt] = Field(..., min_length=1, max_length=10)


class TrainingQuizFeedback(BaseModel):
    correct: bool
    correct_choice: int
    explanation: str = Field(..., max_length=1000)


class TrainingQuizResult(BaseModel):
    attempt_id: int
    score: int
    max_score: int
    percentage: int
    passed: bool
    feedback: list[TrainingQuizFeedback]
    created_at: datetime


class TrainingPracticeQuestion(BaseModel):
    question_index: int = Field(ge=0, le=9)
    question: TrainingQuizQuestion


class TrainingPracticeScenarioOut(BaseModel):
    scenario_id: uuid.UUID
    course_id: uuid.UUID
    questions: list[TrainingPracticeQuestion] = Field(..., min_length=1, max_length=5)
    created_at: datetime


class TrainingPracticeSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: list[StrictInt] = Field(..., min_length=1, max_length=5)


class TrainingPracticeResult(BaseModel):
    scenario_id: uuid.UUID
    attempt_id: int
    score: int
    max_score: int
    percentage: int
    passed: bool
    feedback: list[TrainingQuizFeedback]
    created_at: datetime


class TrainingPracticeHistoryItem(BaseModel):
    scenario_id: uuid.UUID
    question_indexes: list[int] = Field(..., min_length=1, max_length=5)
    created_at: datetime
    completed_at: datetime | None
    score: int | None
    max_score: int | None
    percentage: int | None


class TrainingPracticeHistoryResponse(BaseModel):
    scenarios: list[TrainingPracticeHistoryItem] = Field(..., max_length=50)


class TrainingQuizAttemptOut(BaseModel):
    attempt_id: int
    score: int
    max_score: int
    percentage: int
    passed: bool
    created_at: datetime


class TrainingQuizAttemptListResponse(BaseModel):
    attempts: list[TrainingQuizAttemptOut]


class TrainingReminderOut(BaseModel):
    id: uuid.UUID
    course_id: uuid.UUID
    topic: str
    reminder_type: Literal["due_soon", "overdue"]
    due_at: datetime
    created_at: datetime
    read_at: datetime | None


class TrainingReminderListResponse(BaseModel):
    reminders: list[TrainingReminderOut]


class TrainingAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    department_id: uuid.UUID
    user_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=100)
    due_at: datetime | None = None

    @field_validator("user_ids")
    @classmethod
    def unique_users(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(value) != len(set(value)):
            raise ValueError("An employee can only be selected once.")
        return value

    @field_validator("due_at")
    @classmethod
    def timezone_aware_due_date(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Due dates must include a timezone.")
        return value


class TrainingAssignmentOut(BaseModel):
    assignment_id: uuid.UUID
    course_id: uuid.UUID
    topic: str
    assignee_user_id: uuid.UUID
    assignee_email: str
    department_id: uuid.UUID
    status: Literal["assigned", "completed"]
    due_at: datetime | None
    created_at: datetime
    completed_at: datetime | None
    latest_score: int | None = None
    latest_max_score: int | None = None


class TrainingAssignmentListResponse(BaseModel):
    assignments: list[TrainingAssignmentOut]


class TrainingAssignmentCandidate(BaseModel):
    user_id: uuid.UUID
    email: str


class TrainingAssignmentCandidateListResponse(BaseModel):
    employees: list[TrainingAssignmentCandidate]


class TrainingAssignmentCreateResponse(BaseModel):
    created: int
    assignments: list[TrainingAssignmentOut]


class MarketResearchRequest(BaseModel):
    """Public-web research; the user explicitly confirms that the topic is sent to the configured Gemini service."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    topic: str = Field(..., min_length=4, max_length=3000)
    confirm_external_search: bool = False


class MarketResearchSource(BaseModel):
    title: str = Field(..., max_length=300)
    url: str = Field(..., max_length=2000)


class MarketResearchReport(BaseModel):
    topic: str
    answer: str
    sources: list[MarketResearchSource]
    generated_at: datetime
    model: str
    provider: str


class ExternalResearchStatus(BaseModel):
    enabled: bool
    web_search_available: bool
    gemini_available: bool
    message: str


class PageImageOut(BaseModel):
    document_id: uuid.UUID
    filename: str
    page_number: int
    url: str  # authenticated, same-origin; rendered on request


class ChartSeriesOut(BaseModel):
    name: str
    values: list[float | None]  # aligned with `categories`; None = no figure for that category
    sources: list[str | None]  # citation tag per value, e.g. "S1"


class ChartOut(BaseModel):
    title: str
    unit: str
    layout: Literal["grouped", "stacked"]
    kind: Literal["bar", "line", "pie", "combo"] = "bar"  # charts saved before chart types existed are bars
    line_series: list[str] = Field(default_factory=list)  # combo only: series drawn as a line on its own axis
    categories: list[str]
    series: list[ChartSeriesOut]


class RagChatResponse(BaseModel):
    answer: str
    insufficient_evidence: bool
    citations: list[Citation]
    retrieved_chunks: int
    strategy: Literal[
        "none",
        "small_talk",
        "whole_document",
        "semantic",
        "semantic_with_context",
        "focused_documents",
        "table_analysis",
        "library_overview",
        "page_images",
        "chart",
        "memory",
        "general",
        "web_research",
        "web_export",
        "hybrid_research",
        "image_generation",
        "image_edit",
        "web_image_search",
        "identity",
        "library_inventory",
        "upload_capability",
        "web_capability",
    ]
    chart: ChartOut | None = None
    page_images: list[PageImageOut] = Field(default_factory=list)
    model: str | None
    provider: str | None
    # How many of the user's personal memories shaped this answer (they are never cited as sources).
    memories_applied: int = 0
    # True when (part of) the answer comes from the model's general knowledge, not from the user's documents.
    general_knowledge: bool = False
    # Internal: every document whose text reached the LLM for this answer (cited or not). Persisted with
    # saved answers so access can be re-checked on every read; never returned by the API.
    source_document_ids: list[uuid.UUID] = Field(default_factory=list, exclude=True)
    # Internal: documents the question named by filename, title or date (document discovery). Persisted as the
    # turn's source state so a follow-up can resolve "it" or "aur August ka?" to them; never returned by the API.
    named_document_ids: list[uuid.UUID] = Field(default_factory=list, exclude=True)


class RagVoiceQuestionResponse(BaseModel):
    """A transient transcript and its answer; neither is saved as a conversation."""

    transcript: str = Field(..., min_length=1, max_length=4000)
    answer: RagChatResponse


class IndexStatus(BaseModel):
    vectors: int
    needs_rebuild: bool
    embedding_model: str
    dimension: int
    ingestion_mode: Literal["worker", "embedded"] = "worker"
    workers_online: int = 0
    documents_queued: int = 0
    documents_processing: int = 0
    documents_failed: int = 0


class VectorIndexOut(BaseModel):
    """One vector index (document chunks, or published organizational knowledge)."""

    name: Literal["documents", "knowledge"]
    status: Literal["healthy", "out_of_sync", "needs_rebuild", "rebuilding"]
    vectors: int
    expected_vectors: int  # what PostgreSQL, the source of truth, says the index should hold
    needs_rebuild: bool
    rebuilding: bool = False
    persisted: bool
    snapshot_bytes: int | None
    change_log_bytes: int | None
    memory_bytes: int | None


class VectorStoreStatus(BaseModel):
    backend: str
    supported_backends: list[str]
    index_type: str
    dimension: int
    embedding_model: str
    embedding_dimension: int
    status: Literal["healthy", "degraded"]
    issues: list[str]
    indexes: list[VectorIndexOut]


class ImageUnderstandingStatus(BaseModel):
    """Whether document images (pictures in PDFs, uploaded images) are described at ingestion, and why not."""

    state: Literal["no_provider", "unsupported", "not_authorized", "unknown", "supported"]
    available: bool
    provider: str | None
    model: str | None
    message: str


class IndexRebuildResponse(BaseModel):
    mode: str
    added: int
    removed: int
    vectors: int
