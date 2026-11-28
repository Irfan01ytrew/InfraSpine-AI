"""Admin-only view of knowledge-chat traces: how questions were answered, how long they took and why they failed.

Traces are metadata only (see `app.rag.tracing`), so this view shows no question or answer text.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Date, Select, cast, func, select

from app.api.deps import DbSession, require_platform_admin
from app.core.errors import NotFoundError
from app.db.models import ConversationMessage, Document, RagTrace, User
from app.schemas.traces import DecisionCount, TraceChunkOut, TraceDay, TraceDetail, TraceListResponse, TraceOut, TraceSummary

router = APIRouter(prefix="/api/admin/traces", tags=["admin"], dependencies=[Depends(require_platform_admin)])


def _rows() -> Select:
    return (
        select(RagTrace, User.email, ConversationMessage.feedback)
        .outerjoin(User, User.id == RagTrace.user_id)
        .outerjoin(ConversationMessage, ConversationMessage.id == RagTrace.message_id)
    )


def _out(trace: RagTrace, email: str | None, feedback: int | None) -> dict:
    return {
        **{column: getattr(trace, column) for column in TraceOut.model_fields if hasattr(trace, column)},
        "user_email": email,
        "feedback": feedback,
        "retrieved_count": len(trace.retrieved),
        "cited_count": sum(1 for chunk in trace.retrieved if chunk.get("cited")),
    }


@router.get("", response_model=TraceListResponse)
async def list_traces(
    db: DbSession,
    days: Annotated[int, Query(ge=1, le=365)] = 7,
    status: Literal["all", "ok", "error"] = "all",
    outcome: Literal["all", "answered", "refused", "general"] = "all",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> TraceListResponse:
    since = datetime.now(UTC) - timedelta(days=days)
    window = RagTrace.created_at >= since
    query = _rows().where(window)
    if status != "all":
        query = query.where(RagTrace.status == status)
    if outcome == "refused":
        query = query.where(RagTrace.insufficient_evidence.is_(True))
    elif outcome == "general":
        query = query.where(RagTrace.general_knowledge.is_(True))
    elif outcome == "answered":
        query = query.where(RagTrace.insufficient_evidence.is_(False), RagTrace.general_knowledge.is_(False))
    rows = (await db.execute(query.order_by(RagTrace.id.desc()).limit(limit))).all()
    return TraceListResponse(summary=await _summary(db, window, days), traces=[TraceOut(**_out(*row)) for row in rows])


@router.get("/{trace_id}", response_model=TraceDetail)
async def get_trace(trace_id: int, db: DbSession) -> TraceDetail:
    row = (await db.execute(_rows().where(RagTrace.id == trace_id))).first()
    if row is None:
        raise NotFoundError("Trace not found.")
    trace = row[0]
    ids = {chunk["document_id"] for chunk in trace.retrieved}
    names = dict((await db.execute(select(Document.id, Document.filename).where(Document.id.in_(ids)))).all()) if ids else {}
    names = {str(key): value for key, value in names.items()}
    retrieved = [TraceChunkOut(**chunk, filename=names.get(chunk["document_id"])) for chunk in trace.retrieved]
    return TraceDetail(**_out(*row), retrieved=retrieved, error_stack=trace.error_stack)


async def _summary(db, window, days: int) -> TraceSummary:
    totals = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(RagTrace.status == "error"),
                func.count().filter(RagTrace.insufficient_evidence.is_(True)),
                func.count().filter(RagTrace.general_knowledge.is_(True)),
                func.percentile_cont(0.5).within_group(RagTrace.latency_ms),
                func.percentile_cont(0.95).within_group(RagTrace.latency_ms),
                func.coalesce(func.sum(RagTrace.llm_calls), 0),
                func.coalesce(func.sum(RagTrace.prompt_tokens), 0),
                func.coalesce(func.sum(RagTrace.completion_tokens), 0),
                func.count().filter(ConversationMessage.feedback == 1),
                func.count().filter(ConversationMessage.feedback == -1),
            )
            .select_from(RagTrace)
            .outerjoin(ConversationMessage, ConversationMessage.id == RagTrace.message_id)
            .where(window)
        )
    ).one()
    name = func.coalesce(RagTrace.decision, RagTrace.error_type, "unknown").label("name")
    decisions = (
        await db.execute(
            select(name, func.count()).where(window).group_by(name).order_by(func.count().desc()).limit(12)
        )
    ).all()
    day = cast(func.timezone("UTC", RagTrace.created_at), Date).label("day")
    daily = (await db.execute(
        select(
            day, func.count(),
            func.count().filter(RagTrace.status == "error"),
            func.count().filter(RagTrace.insufficient_evidence.is_(True)),
            func.count().filter(RagTrace.decision.in_(("refused_uncited", "refused_uncited_document_question"))),
        ).where(window).group_by(day).order_by(day.desc())
    )).all()
    (questions, errors, refused, general, p50, p95, calls, prompt, completion, helpful, not_helpful) = totals
    return TraceSummary(
        days=days,
        questions=questions,
        errors=errors,
        refused=refused,
        general=general,
        latency_p50_ms=round(p50, 1) if p50 is not None else None,
        latency_p95_ms=round(p95, 1) if p95 is not None else None,
        llm_calls=calls,
        prompt_tokens=prompt,
        completion_tokens=completion,
        helpful=helpful,
        not_helpful=not_helpful,
        decisions=[DecisionCount(decision=name, count=count) for name, count in decisions],
        daily=[TraceDay(day=day, questions=count, errors=errors, refused=refused, citation_refusals=uncited)
               for day, count, errors, refused, uncited in daily],
    )
