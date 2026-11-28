"""Step AE slice 2 — Knowledge answer path for WhatsApp customers.

Wires the slice-1 decider to Knowledge RAG so a customer question can be
answered from documents, and only from documents:

* The answer source is the **customer-visible subset**: the document ids
  the owner flagged in ``org_settings.whatsapp_customer_visible_document_ids``.
  The default is empty — with no flagged documents the lookup never runs
  and every question hands off to a human. A document outside the subset
  is never quoted, because the subset is passed as ``document_ids`` to
  retrieval, which restricts both the vector and the approved-knowledge
  paths to it (on top of the live ``documents.read`` ACL for the owner).
* An answer is sent only when ``answer_question`` returns grounded text
  with at least one citation (``general_answers=False``). Anything else —
  no chunks, insufficient evidence, a general-knowledge answer — is the
  fixed refusal text, never a guessed answer.
* Citations (``[S#]``) are stripped for the plain-text WhatsApp reply;
  the cited document ids are kept on the returned record and in the audit
  event so the owner can review what the bot quoted.
* ``try_auto_reply`` is the worker entry point: it runs the triage and
  returns a reply string, or ``None`` when the message should fall through
  to the existing Agent Studio task path. A failed Knowledge lookup never
  fails the inbound — it falls through. Lead capture (slice 3) raises on
  a database failure so the worker retries; its idempotency key makes a
  retry safe.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.platform.models import OrgSettings
from app.rag import answer as _answer
from app.rag import retrieval as _retrieval
from app.services.llm import LLMClient
from app.whatsapp_auto.decider import (
    Decision,
    Evidence,
    IncomingMessage,
    decide,
    classify_intent,
)
from app.whatsapp_auto.replies import INSUFFICIENT_EVIDENCE_REPLY, render

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.db.agent_studio_whatsapp_models import AgentStudioWhatsAppInbound
    from app.rag.components import RagComponents
    from app.services.llm_runtime import LLMRuntime

__all__ = [
    "KnowledgeReply",
    "answer_customer_question",
    "customer_visible_document_ids",
    "load_customer_visible_document_ids",
    "strip_citations",
    "triage_customer_message",
    "try_auto_reply",
]

_CITATION_RE = re.compile(r"\s*\[S\d+\]")


@dataclass(frozen=True)
class KnowledgeReply:
    """The outcome of a Knowledge lookup for one customer question."""

    found: bool
    reply_text: str
    citations: tuple[uuid.UUID, ...] = ()
    reason: str = ""
    confidence: float = 0.0


def customer_visible_document_ids(settings_row: OrgSettings | None) -> list[uuid.UUID]:
    """Parse the owner-flagged customer-visible document ids.

    Invalid entries are dropped, duplicates removed (order kept). A
    missing row or column (pre-migration) means the empty subset: no
    document answers until the owner opts in.
    """
    raw = getattr(settings_row, "whatsapp_customer_visible_document_ids", None) or []
    ids: list[uuid.UUID] = []
    for value in raw:
        try:
            parsed = uuid.UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            continue
        if parsed not in ids:
            ids.append(parsed)
    return ids


async def load_customer_visible_document_ids(db: AsyncSession) -> list[uuid.UUID]:
    """Read the customer-visible subset from the single org-settings row."""
    row = await db.scalar(select(OrgSettings).where(OrgSettings.id == 1))
    return customer_visible_document_ids(row)


def strip_citations(answer: str) -> str:
    """Remove ``[S#]`` citation markers for a plain-text WhatsApp reply."""
    return re.sub(r"\s+", " ", _CITATION_RE.sub("", answer or "")).strip()


def _refusal(reason: str) -> KnowledgeReply:
    return KnowledgeReply(
        found=False,
        reply_text=INSUFFICIENT_EVIDENCE_REPLY,
        reason=reason,
    )


async def answer_customer_question(
    db: AsyncSession,
    user: User,
    rag: RagComponents,
    settings: Settings,
    get_llm: Callable[[], Awaitable[LLMClient]],
    question: str,
    *,
    document_ids: list[uuid.UUID] | None = None,
) -> KnowledgeReply:
    """Answer one customer question from the customer-visible documents.

    ``document_ids`` is the subset to search; when omitted it is loaded
    from org settings. An empty subset answers nothing and never calls
    retrieval or the LLM.
    """
    ids = document_ids if document_ids is not None else await load_customer_visible_document_ids(db)
    if not ids:
        return _refusal("no_customer_visible_documents")
    chunks = await _retrieval.retrieve(
        db,
        user,
        rag.embedder,
        rag.vector_store,
        question,
        top_k=settings.rag_top_k,
        min_similarity=settings.rag_min_similarity,
        max_context_chars=settings.rag_max_context_chars,
        document_ids=ids,
    )
    if not chunks:
        return _refusal("no_matching_documents")
    llm = await get_llm()
    result = await _answer.answer_question(llm, question, chunks, general_answers=False)
    if result.insufficient_evidence or result.general or not result.cited:
        return _refusal("insufficient_evidence")
    reply_text = strip_citations(result.answer)
    if not reply_text:
        return _refusal("insufficient_evidence")
    citations = tuple(dict.fromkeys(chunk.document_id for _, chunk in result.cited))
    return KnowledgeReply(
        found=True,
        reply_text=reply_text,
        citations=citations,
        reason="evidence_backed",
        confidence=1.0,
    )


async def triage_customer_message(
    db: AsyncSession,
    user: User,
    rag: RagComponents,
    settings: Settings,
    get_llm: Callable[[], Awaitable[LLMClient]],
    text: str,
    *,
    sender_verified: bool,
    asked_before: bool = False,
) -> tuple[Decision, KnowledgeReply | None]:
    """Classify one customer message and, for questions, look up Knowledge.

    Returns the slice-1 decision plus the Knowledge reply when (and only
    when) the decision is ``reply_from_knowledge``. A question with no
    usable evidence still gets its decision (handoff), so the caller can
    fall through to the task path with the reason recorded.
    """
    intent, _ = classify_intent(text)
    if intent != "question" or not sender_verified:
        # Non-questions never need a lookup, and an unverified sender gets
        # no Knowledge content at all — decide without calling retrieval
        # or the LLM.
        decision = decide(IncomingMessage(text, sender_verified, asked_before), Evidence())
        return decision, None
    knowledge = await answer_customer_question(db, user, rag, settings, get_llm, text)
    evidence = Evidence(
        found=knowledge.found,
        citations=len(knowledge.citations),
        confidence=knowledge.confidence,
    )
    decision = decide(IncomingMessage(text, sender_verified, asked_before), evidence)
    if decision.action == "reply_from_knowledge":
        return decision, knowledge
    return decision, None


async def try_auto_reply(
    db: AsyncSession,
    user: User,
    settings: Settings,
    runtime: LLMRuntime,
    rag: RagComponents,
    inbound: AgentStudioWhatsAppInbound,
    message: str,
    *,
    sender_verified: bool = True,
) -> str | None:
    """Try to auto-reply to one inbound customer message.

    Returns the reply text for ``reply_from_knowledge`` and for a
    captured lead, or ``None`` when the message must fall through to the
    existing Agent Studio task path. A failed Knowledge lookup falls
    through instead of failing the inbound; the decision, reason and
    citations are audited (no message text, no secrets).
    """
    from app.services import audit
    from app.services.audit import AuditContext
    from app.whatsapp_auto.leads import capture_lead

    try:
        decision, knowledge = await triage_customer_message(
            db,
            user,
            rag,
            settings,
            lambda: runtime.get_client(db),
            message,
            sender_verified=sender_verified,
        )
    except Exception:
        # Knowledge lookup failed (no provider, retrieval error, ...):
        # the agent task path is the safe fallback for this message.
        return None
    if decision.action == "reply_from_knowledge" and knowledge is not None:
        audit.record(
            db,
            AuditContext(inbound.owner_id, None, None, None),
            "integration.whatsapp_auto_reply",
            target=str(inbound.id),
            details={
                "decision": decision.action,
                "reason": decision.reason,
                "citations": [str(doc_id) for doc_id in knowledge.citations],
            },
        )
        return knowledge.reply_text
    if decision.action == "capture_lead" and decision.lead is not None:
        lead = await capture_lead(
            db,
            owner_id=user.id,
            wa_id=inbound.from_wa_id,
            inbound_id=inbound.id,
            signals=decision.lead,
        )
        audit.record(
            db,
            AuditContext(inbound.owner_id, None, None, None),
            "integration.whatsapp_auto_reply",
            target=str(inbound.id),
            details={
                "decision": decision.action,
                "reason": decision.reason,
                "lead_id": str(lead.id),
            },
        )
        return render("lead_ack")
    return None
