import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.rag import ModelChoice, RagChatResponse


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int


class ConversationListResponse(BaseModel):
    conversations: list[ConversationOut]


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, max_length=200)


class RenameConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=200)


class MessageOut(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    # Assistant only: the full RAG response (citations, chart, page images, provider/model).
    response: RagChatResponse | None = None
    # How many source documents of this answer the user can no longer read.
    sources_unavailable: int = 0
    # True when the answer used a document the user can no longer read: its text, chart, snippets and
    # page images are replaced by a notice (the saved row is kept and reappears if access is restored).
    withheld: bool = False
    # Assistant only: the owner's rating, 1 helpful or -1 not helpful.
    feedback: Literal[-1, 1] | None = None


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Literal[-1, 1] | None  # null clears the rating


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class SendMessageRequest(ModelChoice):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(..., min_length=1, max_length=4000)
    document_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)


class SendMessageResponse(BaseModel):
    conversation: ConversationOut
    user_message: MessageOut
    assistant_message: MessageOut
