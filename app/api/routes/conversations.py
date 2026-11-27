"""Persistent knowledge-chat conversations. Signed-in users see and change only their own."""

import asyncio
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import StreamingResponse

from app.api.deps import AppSettings, CurrentUser, DbSession, audit_context, get_current_user
from app.core.errors import AppError
from app.rag import tracing
from app.rag.components import RagComponents
from app.rag.respond import rag_respond
from app.schemas.conversations import (
    ConversationDetail,
    ConversationListResponse,
    ConversationOut,
    CreateConversationRequest,
    FeedbackRequest,
    MessageOut,
    RenameConversationRequest,
    SendMessageRequest,
    SendMessageResponse,
)
from app.services.conversations import ConversationService
from app.services.chat_stream import conversation_events
from app.services.memory import MemoryService

router = APIRouter(prefix="/api/conversations", tags=["conversations"], dependencies=[Depends(get_current_user)])
logger = logging.getLogger(__name__)


def get_conversations(request: Request, db: DbSession, settings: AppSettings, user: CurrentUser) -> ConversationService:
    return ConversationService(db, settings, user, audit_context(request, user))


Conversations = Annotated[ConversationService, Depends(get_conversations)]


@router.get("", response_model=ConversationListResponse)
async def list_conversations(service: Conversations) -> ConversationListResponse:
    return ConversationListResponse(conversations=await service.list_all())


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(payload: CreateConversationRequest, service: Conversations) -> ConversationOut:
    conversation = await service.create(payload.title)
    return service.summary(conversation, 0)


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: uuid.UUID, service: Conversations) -> ConversationDetail:
    conversation = await service.get(conversation_id)
    messages = await service.messages(conversation)
    summary = service.summary(conversation, len(messages))
    return ConversationDetail(**summary.model_dump(), messages=await service.messages_out(messages))


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def rename_conversation(
    conversation_id: uuid.UUID, payload: RenameConversationRequest, service: Conversations
) -> ConversationOut:
    conversation = await service.rename(conversation_id, payload.title)
    return service.summary(conversation, await service.count(conversation))


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation_id: uuid.UUID, service: Conversations) -> Response:
    await service.delete(conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{conversation_id}/messages/{message_id}/feedback", response_model=MessageOut)
async def rate_message(
    conversation_id: uuid.UUID, message_id: int, payload: FeedbackRequest, service: Conversations
) -> MessageOut:
    return await service.set_feedback(conversation_id, message_id, payload.rating)


@router.post("/{conversation_id}/messages", response_model=SendMessageResponse)
async def send_message(
    conversation_id: uuid.UUID,
    payload: SendMessageRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
    service: Conversations,
) -> SendMessageResponse:
    """Answer a question in a saved conversation. Context comes from the stored conversation (bounded),
    never from the client; retrieval uses the user's *current* document permissions."""
    return await _answer_message(conversation_id, payload, request, db, user, settings, service)


@router.post("/{conversation_id}/messages/stream")
async def stream_message(
    conversation_id: uuid.UUID,
    payload: SendMessageRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
    service: Conversations,
) -> StreamingResponse:
    """Stream provider text deltas as SSE, then send the authoritative, evidence-checked saved response."""
    queue: asyncio.Queue[tuple[str, object] | None] = asyncio.Queue(maxsize=64)

    async def emit(delta: str) -> None:
        await queue.put(("delta", {"delta": delta}))

    async def produce() -> None:
        try:
            # Stream provider tokens while the message is still being composed. The UI treats
            # these as provisional and swaps to the single, persisted answer at `done`; after
            # that point neither hydration nor a background poll may replace the completed row.
            result = await _answer_message(conversation_id, payload, request, db, user, settings, service, on_token=emit)
            await queue.put(("done", result.model_dump(mode="json")))
        except asyncio.CancelledError:
            raise
        except AppError as exc:
            await queue.put(("error", {"code": exc.code, "message": exc.message, "status": exc.status_code}))
        except Exception as exc:
            logger.error("Chat stream failed error_type=%s", type(exc).__name__)
            await queue.put(("error", {"code": "internal_error", "message": "The assistant could not complete this reply.", "status": 500}))
        finally:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                # A disconnected client may stop consuming while the bounded producer queue is full.
                # Avoid pinning the request task during cancellation; the stream consumer is already gone.
                pass

    return StreamingResponse(
        conversation_events(produce, queue),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


async def _answer_message(
    conversation_id: uuid.UUID,
    payload: SendMessageRequest,
    request: Request,
    db,
    user,
    settings,
    service: ConversationService,
    *,
    on_token=None,
) -> SendMessageResponse:
    conversation = await service.get(conversation_id)
    rag: RagComponents = request.app.state.rag
    async with tracing.traced(
        request.app.state.sessionmaker, settings, route="conversation", question=payload.question,
        user_id=user.id, request_id=request.state.request_id, conversation_id=conversation.id,
    ) as trace:
        try:
            history, focus = await service.context(conversation, payload.question)
            response = await rag_respond(
                db,
                user,
                rag,
                settings,
                lambda: request.app.state.llm_runtime.get_client(db, payload.model_choice()),
                payload.question,
                history=history,
                document_ids=payload.document_ids,
                focus_document_ids=focus or None,
                memory=MemoryService(db, settings, user, audit_context(request, user)),
                conversation_id=conversation.id,
                previous_charts=await service.previous_charts(conversation),
                on_token=on_token,
                get_web_llm=lambda: request.app.state.llm_runtime.get_web_research_client(db),
                route_web=True,
            )
            conversation, user_message, assistant_message = await service.append(
                conversation_id, payload.question, response
            )
            if trace is not None:
                trace.message_id = assistant_message.id
            # append commits, but commit here also releases any read-only transaction opened after it.
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    user_out, assistant_out = await service.messages_out([user_message, assistant_message])
    return SendMessageResponse(
        conversation=service.summary(conversation, await service.count(conversation)),
        user_message=user_out,
        assistant_message=assistant_out,
    )
