from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, DbSession, get_llm_runtime
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.llm_runtime import LLMRuntime

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    db: DbSession,
    user: CurrentUser,
    runtime: Annotated[LLMRuntime, Depends(get_llm_runtime)],
) -> ChatResponse:
    """Send a single user message to the active LLM provider. Requires a signed-in user."""
    # Resolved per request from the database, so configuration changes apply without a restart.
    llm = await runtime.get_client(db)
    return await llm.chat(payload.message)
