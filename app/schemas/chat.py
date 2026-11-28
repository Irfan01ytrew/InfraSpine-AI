"""Request/response contracts for the chat API."""

from pydantic import BaseModel, ConfigDict, Field

MAX_MESSAGE_LENGTH = 100_000


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(
        ...,
        min_length=1,
        max_length=MAX_MESSAGE_LENGTH,
        description="The user's message to the assistant.",
        examples=["Summarise the benefits of infrastructure as code."],
    )


class RoutingInfo(BaseModel):
    """Public "why this model?" metadata for one routed chat/RAG answer.
    Ids, cost estimate and reason only -- never prompt text, keys or endpoints."""

    provider: str
    model: str
    task_class: str
    policy: str
    estimated_cost_usd: float | None = None
    reason: str
    alternates: list[str] = Field(default_factory=list)
    budget_action: str = "allow"


class TokenUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatResponse(BaseModel):
    reply: str
    model: str
    provider: str
    usage: TokenUsage | None = None
    routing: RoutingInfo | None = None
