"""Single-turn chat against the active LLM provider."""

import logging
import time
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.deps import AppSettings, CurrentUser, DbSession, get_llm_runtime
from app.core.errors import RoutingBudgetExceededError
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.llm_runtime import LLMRuntime, resolve_chat_client, routing_info
from app.services.routing_records import record_refused_routing, record_routing
from app.services.routing_policy import routing_context_for_user

router = APIRouter(prefix="/api/ai", tags=["ai"])
logger = logging.getLogger(__name__)


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    db: DbSession,
    user: CurrentUser,
    runtime: Annotated[LLMRuntime, Depends(get_llm_runtime)],
    settings: AppSettings,
    request: Request,
) -> ChatResponse:
    """Send a single user message to the active LLM provider. Requires a signed-in user."""
    # Resolved per request from the database, so configuration changes apply without a restart.
    # The smart router picks the cheapest capable model (chat has no model picker, so this
    # route always routes); the decision is exposed as response metadata and one routing
    # record is persisted per call. A telemetry failure never loses the answer.
    started = time.monotonic()
    decision = None
    # The caller's department and the monthly budget that applies to them
    # (their department's own budget, else the org-wide one), resolved once
    # per request from the stored routing policy.
    routing_ctx = await routing_context_for_user(db, user.id)
    try:
        llm, decision = await resolve_chat_client(
            runtime, db, payload.message,
            department_id=routing_ctx.department_id,
            monthly_budget_usd=routing_ctx.monthly_budget_usd,
            policy_config=routing_ctx.config,
        )
        response = await llm.chat(payload.message)
    except RoutingBudgetExceededError as exc:
        details = exc.details or {}
        try:
            await record_refused_routing(
                db,
                task_class=details.get("task_class") or "standard",
                provider=details.get("provider"),
                model=details.get("model"),
                estimated_cost_usd=(
                    Decimal(details["estimated_cost_usd"]) if details.get("estimated_cost_usd") else None
                ),
                request_id=getattr(request.state, "request_id", None),
                user_id=user.id,
                department_id=routing_ctx.department_id,
            )
            await db.commit()
        except Exception:
            logger.warning("Could not record refused routing", exc_info=True)
            await db.rollback()
        raise
    except BaseException:
        if decision is not None:
            try:
                await record_routing(
                    db,
                    decision,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    status="error",
                    request_id=getattr(request.state, "request_id", None),
                    user_id=user.id,
                    department_id=routing_ctx.department_id,
                    settings=settings,
                )
                await db.commit()
            except Exception:
                logger.warning("Could not record failed routing", exc_info=True)
                await db.rollback()
        raise
    latency_ms = int((time.monotonic() - started) * 1000)
    if decision is not None:
        response.routing = routing_info(decision)
        usage = response.usage
        try:
            await record_routing(
                db,
                decision,
                status="downgraded" if decision.budget_action == "downgrade" else "ok",
                prompt_tokens=usage.prompt_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
                latency_ms=latency_ms,
                request_id=getattr(request.state, "request_id", None),
                user_id=user.id,
                department_id=routing_ctx.department_id,
                settings=settings,
            )
            await db.commit()
        except Exception:
            logger.warning("Could not record routing", exc_info=True)
            await db.rollback()
    return response
