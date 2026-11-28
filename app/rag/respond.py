"""Builds the knowledge-chat response for one question. Shared by the stateless `/api/rag/chat`
endpoint and persistent conversations, so both behave identically."""

import logging
import uuid
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import User
from app.rag.answer import Turn, requested_export_format, small_talk_reply
from app.rag.charts import PreviousChart
from app.rag.chat import run_chat
from app.rag import tracing
from app.rag.components import RagComponents
from app.rag.sufficiency import credential_values_in, redact_credentials
from app.schemas.rag import ChartOut, ChartSeriesOut, Citation, PageImageOut, RagChatResponse
from app.services.llm import LLMClient
from app.services.memory import MemoryService, parse_memory_command

logger = logging.getLogger(__name__)

SNIPPET_CHARS = 320


async def rag_respond(
    db: AsyncSession,
    user: User,
    rag: RagComponents,
    settings: Settings,
    get_llm: Callable[[], Awaitable[LLMClient]],
    question: str,
    *,
    history: list[Turn],
    document_ids: list[uuid.UUID] | None,
    focus_document_ids: list[uuid.UUID] | None,
    memory: MemoryService | None = None,
    conversation_id: uuid.UUID | None = None,
    previous_charts: list[PreviousChart] | None = None,
    department_group_id: uuid.UUID | None = None,
    structured_source_output: bool = False,
    system_prompt_addendum: str | None = None,
    on_token: Callable[[str], Awaitable[None]] | None = None,
    get_web_llm: Callable[[], Awaitable[LLMClient]] | None = None,
    route_web: bool = False,
) -> RagChatResponse:
    trace = tracing.current()
    if trace is not None:
        trace.history_turns = len(history)
    # Explicit "remember / forget / what do you remember" requests are handled here, without the LLM.
    if memory is not None and (command := parse_memory_command(question)) is not None:
        tracing.decide("memory_command")
        return RagChatResponse(
            answer=await memory.handle_command(command, conversation_id), insufficient_evidence=False,
            citations=[], retrieved_chunks=0, strategy="memory", model=None, provider=None,
        )
    if (reply := small_talk_reply(question)) is not None:
        tracing.decide("small_talk")
        return RagChatResponse(
            answer=reply, insufficient_evidence=False, citations=[], retrieved_chunks=0,
            strategy="small_talk", model=None, provider=None,
        )
    if memory is not None:
        await memory.auto_capture(question, conversation_id)
    with tracing.stage("memory"):
        personal = await memory.context(question, rag.embedder) if memory is not None else []
    result = await run_chat(
        db, user, rag, settings, tracing.traced_llm(get_llm), question,
        history=history, document_ids=document_ids, focus_document_ids=focus_document_ids, memory=personal,
        previous_charts=previous_charts, department_group_id=department_group_id,
        structured_source_output=structured_source_output,
        system_prompt_addendum=system_prompt_addendum,
        on_token=on_token,
        get_web_llm=get_web_llm,
        route_web=route_web,
    )
    answer = result.answer
    secrets = credential_values_in(chunk.content for chunk in result.passages)
    safe_answer = redact_credentials(answer.answer, secrets)
    if safe_answer != answer.answer:
        answer = replace(answer, answer=safe_answer)
    export_format = requested_export_format(question)
    if export_format and not answer.insufficient_evidence and answer.answer.strip():
        try:
            from app.services.chat_exports import create_chat_export

            export_content = answer.answer
            if answer.cited:
                sources = list(dict.fromkeys(
                    f"[S{number}] {chunk.filename}, {chunk.location_label}" for number, chunk in answer.cited
                ))
                export_content += "\n\n## Sources\n" + "\n".join(f"- {source}" for source in sources)
            if answer.freshness_warning:
                export_content += f"\n\n{answer.freshness_warning}"
            token, _filename = await asyncio.to_thread(
                create_chat_export,
                export_content,
                export_format,
                settings.rag_data_dir / "exports",
                user.id,
                settings.master_key_bytes,
            )
            link = f"[Download {export_format.upper()}](/api/exports/download/{token}?format={export_format})"
            # Keep the generated material in the file and the chat response focused on
            # the deliverable. LLMs often append copy/paste instructions even though
            # this path created an actual downloadable file.
            answer = replace(answer, answer=f"Your {export_format.upper()} file is ready.\n\n{link}")
        except Exception as exc:
            logger.warning("Chat export failed format=%s error_type=%s", export_format, type(exc).__name__)
            answer = replace(answer, answer=f"{answer.answer.rstrip()}\n\nThe downloadable file could not be created. Please try again.")
    if trace is not None:
        trace.record_result(result)
    # Credentials in any retrieved passage never leave in the answer text or a citation snippet, whatever the
    # model wrote (it only ever saw them redacted).
    # Counts only: questions and document content are never logged.
    logger.info(
        "RAG chat strategy=%s passages=%s cited=%s insufficient=%s",
        result.strategy, len(result.passages), len(answer.cited), answer.insufficient_evidence,
    )
    # Step AI slice 4: per-citation freshness rides on the citation it labels.
    freshness_by_source = {entry.source_number: entry for entry in answer.citation_freshness}
    return RagChatResponse(
        answer=redact_credentials(answer.answer, secrets),
        insufficient_evidence=answer.insufficient_evidence,
        citations=[
            Citation(
                source_id=f"S{number}",
                document_id=chunk.document_id,
                filename=chunk.filename,
                page_number=chunk.page_number,
                locator=chunk.location,
                location=chunk.location_label,
                chunk_index=chunk.chunk_index,
                score=round(chunk.score, 4),
                snippet=redact_credentials(chunk.content, secrets)[:SNIPPET_CHARS],
                freshness_status=(freshness_by_source[number].status if number in freshness_by_source else None),
                freshness_score=(freshness_by_source[number].score if number in freshness_by_source else None),
                freshness_note=(freshness_by_source[number].note if number in freshness_by_source else None),
            )
            for number, chunk in answer.cited
        ],
        freshness_warning=answer.freshness_warning,
        retrieved_chunks=len(result.passages),
        strategy=result.strategy,
        chart=_chart_out(result.chart),
        page_images=[
            PageImageOut(
                document_id=img.document_id,
                filename=img.filename,
                page_number=img.page_number,
                url=f"/api/rag/documents/{img.document_id}/pages/{img.page_number}/image",
            )
            for img in result.page_images
        ],
        model=answer.model,
        provider=answer.provider,
        memories_applied=result.memories_applied,
        general_knowledge=answer.general,
        source_document_ids=list(
            dict.fromkeys([c.document_id for c in result.passages] + [img.document_id for img in result.page_images])
        ),
        named_document_ids=list(result.named_documents),
    )


def _chart_out(chart) -> ChartOut | None:
    if chart is None:
        return None
    by_key = {(p.category, p.series): p for p in chart.points}
    return ChartOut(
        title=chart.title,
        unit=chart.unit,
        layout=chart.layout,
        kind=chart.kind,
        line_series=chart.line_series,
        categories=chart.categories,
        series=[
            ChartSeriesOut(
                name=name,
                values=[by_key[(c, name)].value if (c, name) in by_key else None for c in chart.categories],
                sources=[f"S{by_key[(c, name)].source}" if (c, name) in by_key else None for c in chart.categories],
            )
            for name in chart.series
        ],
    )
