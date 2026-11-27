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


class TokenUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatResponse(BaseModel):
    reply: str
    model: str
    provider: str
    usage: TokenUsage | None = None
