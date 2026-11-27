import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


class TraceChunkOut(BaseModel):
    rank: int
    chunk_id: int
    document_id: uuid.UUID
    filename: str | None = None  # None when the document has since been deleted
    score: float
    cited: bool


class TraceOut(BaseModel):
    id: int
    created_at: datetime
    request_id: str | None
    user_email: str | None
    conversation_id: uuid.UUID | None
    message_id: int | None
    route: str
    status: str
    strategy: str | None
    decision: str | None
    provider: str | None
    model: str | None
    insufficient_evidence: bool | None
    general_knowledge: bool | None
    question_chars: int
    history_turns: int
    retrieved_count: int
    cited_count: int
    timings: dict[str, float]
    llm_calls: int
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int
    error_type: str | None
    feedback: int | None


class TraceDetail(TraceOut):
    retrieved: list[TraceChunkOut]
    error_stack: str | None


class DecisionCount(BaseModel):
    decision: str
    count: int


class TraceDay(BaseModel):
    day: date
    questions: int
    errors: int
    refused: int
    citation_refusals: int


class TraceSummary(BaseModel):
    days: int
    questions: int
    errors: int
    refused: int
    general: int
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    helpful: int
    not_helpful: int
    decisions: list[DecisionCount]
    daily: list[TraceDay] = Field(default_factory=list)


class TraceListResponse(BaseModel):
    summary: TraceSummary
    traces: list[TraceOut]
