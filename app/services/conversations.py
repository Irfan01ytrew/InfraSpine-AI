"""Persistent knowledge-chat conversations.

* Owner-only: every query filters by the signed-in user; other users (including admins) get 404.
* Provider-independent: messages are stored as text + JSON; provider/model are recorded per answer
  for audit, never needed to read or continue a conversation.
* Bounded context: only the last few turns are sent to the LLM; the full history is never replayed.
* Permissions are re-evaluated on every read and every new question: documents the user can no
  longer read are never retrieved again. A saved answer whose sources include such a document is
  withheld as a whole (text, chart, snippets and page images), because its text may restate them, and
  it is never sent to the LLM as context again. The stored row is kept, so restored access shows it again.
* Separate from the RAG knowledge index: chat text is never embedded or searched as knowledge.
"""

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError
from app.db.models import Conversation, ConversationMessage, Document, User
from app.rag import access
from app.rag.answer import NO_ANSWER_NOTE, Turn, is_contextual_followup, previous_question
from app.rag.charts import PreviousChart
from app.schemas.conversations import ConversationOut, MessageOut
from app.schemas.rag import Citation, RagChatResponse
from app.services import audit
from app.services.audit import AuditContext

DEFAULT_TITLE = "New chat"
_TITLE_CHARS = 60
_RESPONSE_PAYLOAD_FIELDS = {"citations", "retrieved_chunks", "chart", "page_images", "memories_applied", "general_knowledge"}
_HISTORY_LOOKBACK_TURNS = 100
_HISTORY_RELEVANT_TURNS = 3
_HISTORY_QUESTION_CHARS = 1000
_HISTORY_STOPWORDS = frozenset({
    "the", "and", "for", "you", "your", "are", "was", "were", "what", "which", "when", "where",
    "who", "how", "why", "did", "does", "do", "has", "have", "had", "can", "could", "would",
    "should", "will", "about", "with", "from", "into", "this", "that", "these", "those", "there",
    "then", "than", "also", "please", "tell", "give", "show", "explain", "detail", "details", "more",
    "btao", "batao", "bataye", "samjhao", "kya", "kia", "kiya", "ka", "ki", "ke", "ko", "se",
    "mein", "main", "mai", "me", "hai", "hain", "tha", "thi", "ho", "aur", "or", "phir", "uska",
    "uski", "uskay", "iska", "iski", "yeh", "ye", "woh", "wo", "mujhe", "mujai", "mera", "meri",
    "mere", "aap", "ap", "tum", "ab", "tak", "tk", "kaisa", "kaisi", "kon", "kaun", "konsi",
    "kitna", "kitni", "kitne",
    # Common Urdu-script particles and pronouns should not make every old Urdu turn look relevant.
    "کیا", "کیوں", "کس", "کہ", "کے", "کی", "کا", "کو", "میں", "پر", "سے", "ہے", "ہیں", "تھا", "تھی",
    "تھے", "ہوں", "ہو", "اور", "یا", "پھر", "اس", "یہ", "وہ", "اسے", "اسکا", "اسکی", "اسکے",
    "میرا", "میری", "میرے", "آپ", "تم", "ہم", "کون", "کتنا", "کتنی", "کتنے", "بتائیں", "بتاؤ",
    "بتاؤ", "ذرا", "تفصیل", "وضاحت", "پورا", "پوری", "مکمل", "مزید",
})


def _history_terms(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)
        if len(token) > 2 and token not in _HISTORY_STOPWORDS
    }


def _select_history_context(history: list[Turn], question: str, recent_limit: int) -> list[Turn]:
    """Keep recent dialogue plus a few relevant turns from the saved conversation's last 100 turns.

    This is private, bounded conversational context, never a RAG source. Older document-backed turns have already
    passed the service's current ACL check before reaching this selector.
    """
    if recent_limit <= 0 or not history:
        return []
    recent_start = max(0, len(history) - recent_limit)
    selected = set(range(recent_start, len(history)))
    older_capacity = min(_HISTORY_RELEVANT_TURNS, recent_limit)
    if recent_start == 0 or older_capacity == 0:
        return history[recent_start:]

    query_terms = _history_terms(question)
    candidates: list[tuple[int, int]] = []
    for index, turn in enumerate(history[:recent_start]):
        terms = _history_terms(turn.question + " " + turn.answer)
        if query_terms and terms:
            overlap = len(query_terms & terms)
            if overlap:
                candidates.append((overlap, index))

    # A short elaboration/formatting request has no search terms of its own. Retain the latest earlier question
    # that introduced the topic, even when several short follow-ups have since pushed it out of the recent window.
    anchor_question = previous_question(history) if is_contextual_followup(question) else None
    anchor_index = next(
        (i for i in range(recent_start - 1, -1, -1) if history[i].question == anchor_question),
        None,
    ) if anchor_question else None
    if anchor_index is not None:
        selected.add(anchor_index)

    ranked = [item[1] for item in sorted(candidates, key=lambda item: (-item[0], -item[1]))]
    for index in ranked:
        if len(selected) >= recent_limit + older_capacity:
            break
        selected.add(index)
    result: list[Turn] = []
    for i in sorted(selected):
        turn = history[i]
        if i < recent_start and len(turn.question) > _HISTORY_QUESTION_CHARS:
            turn = Turn(
                turn.question[:_HISTORY_QUESTION_CHARS], turn.answer,
                grounded=turn.grounded, documents=turn.documents, general=turn.general,
            )
        result.append(turn)
    return result
WITHHELD_ANSWER = "This answer is hidden because it used documents you can no longer access."


def answer_sources(message: ConversationMessage) -> set[uuid.UUID]:
    """Every document whose text went into a saved answer. Answers saved before source tracking
    fall back to the documents they cited or showed."""
    payload = message.payload
    sources = {uuid.UUID(i) for i in payload.get("source_document_ids", [])}
    for key in ("citations", "page_images"):
        sources.update(uuid.UUID(item["document_id"]) for item in payload.get(key, []))
    return sources


def named_sources(message: ConversationMessage) -> list[uuid.UUID]:
    """Documents the turn's question named (document discovery), in the order they were resolved. Answers saved
    before source tracking have none."""
    return [uuid.UUID(i) for i in message.payload.get("named_document_ids", [])]


def title_from_question(question: str) -> str:
    text = " ".join(question.split())
    return text if len(text) <= _TITLE_CHARS else text[: _TITLE_CHARS - 1].rstrip() + "…"


class ConversationService:
    def __init__(self, db: AsyncSession, settings: Settings, user: User, ctx: AuditContext) -> None:
        self.db = db
        self.settings = settings
        self.user = user
        self.ctx = ctx

    # ----------------------------------------------------------------- reads

    async def list_all(self, limit: int = 200) -> list[ConversationOut]:
        counts = (
            select(ConversationMessage.conversation_id, func.count().label("n"))
            .group_by(ConversationMessage.conversation_id)
            .subquery()
        )
        rows = await self.db.execute(
            select(Conversation, func.coalesce(counts.c.n, 0))
            .outerjoin(counts, counts.c.conversation_id == Conversation.id)
            .where(Conversation.user_id == self.user.id)
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
        )
        return [self.summary(c, n) for c, n in rows]

    async def get(self, conversation_id: uuid.UUID, *, lock: bool = False) -> Conversation:
        query = select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == self.user.id)
        if lock:
            query = query.with_for_update()
        conversation = (await self.db.execute(query)).scalar_one_or_none()
        if conversation is None:  # also for other users' conversations: never reveal they exist
            raise NotFoundError("Conversation not found.")
        return conversation

    async def messages(self, conversation: Conversation) -> list[ConversationMessage]:
        rows = await self.db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation.id)
            .order_by(ConversationMessage.id)
        )
        return list(rows.scalars())

    async def count(self, conversation: Conversation) -> int:
        return await self.db.scalar(
            select(func.count()).where(ConversationMessage.conversation_id == conversation.id)
        ) or 0

    async def previous_charts(self, conversation: Conversation, limit: int = 3) -> list[PreviousChart]:
        """Charts already shown in this conversation, oldest first, only from sources the user can still read
        (a chart's figures come from its documents, so a revoked source keeps them out of the prompt)."""
        rows = list(
            (
                await self.db.execute(
                    select(ConversationMessage)
                    .where(
                        ConversationMessage.conversation_id == conversation.id,
                        ConversationMessage.role == "assistant",
                        ConversationMessage.strategy == "chart",
                    )
                    .order_by(ConversationMessage.id.desc())
                    .limit(limit)
                )
            ).scalars()
        )[::-1]
        readable = await self._readable(set().union(*(answer_sources(m) for m in rows)))
        charts = []
        for message in rows:
            chart = message.payload.get("chart")
            if chart and not (answer_sources(message) - readable):
                charts.append(
                    PreviousChart(
                        chart.get("title", ""), list(chart.get("categories", [])),
                        [s.get("name", "") for s in chart.get("series", [])],
                    )
                )
        return charts

    async def context(
        self, conversation: Conversation, question: str | None = None
    ) -> tuple[list[Turn], list[uuid.UUID]]:
        """Bounded LLM context: recent turns plus relevant turns from up to 100 saved turns.

        The conversation remains private to its owner and is never searched as enterprise knowledge. Document source
        access is rechecked for every row before its text can be selected as context.
        """
        turns = self.settings.chat_context_turns
        lookback_turns = max(turns, _HISTORY_LOOKBACK_TURNS) if turns else 1
        rows = list(
            (
                await self.db.execute(
                    select(ConversationMessage)
                    .where(ConversationMessage.conversation_id == conversation.id)
                    .order_by(ConversationMessage.id.desc())
                    .limit(max(2, lookback_turns * 2 + 2))
                )
            ).scalars()
        )[::-1]
        assistant = [m for m in rows if m.role == "assistant"]
        readable = await self._readable(set().union(*(answer_sources(m) for m in assistant)))
        readable_named = await self._readable(set().union(*(named_sources(m) for m in assistant)))
        history: list[Turn] = []
        focus: list[uuid.UUID] = []
        for question_message, answer in zip(rows, rows[1:], strict=False):
            if question_message.role != "user" or answer.role != "assistant":
                continue
            if answer_sources(answer) - readable:  # withheld answers never reach the LLM again
                focus = []
                continue
            if answer.strategy == "memory":  # memory commands are not document-scoped turns
                focus = []
                continue
            cited = list(dict.fromkeys(uuid.UUID(c["document_id"]) for c in answer.payload.get("citations", [])))
            named = [i for i in named_sources(answer) if i in readable_named]
            # A withheld reply may still have searched relevant passages. Keep those IDs as follow-up scope; every
            # source is re-authorized here and retrieval checks permissions again before using it next turn.
            retrieved = (
                [i for i in answer_sources(answer) if i in readable]
                if not answer.payload.get("general_knowledge")
                else []
            )
            # Prefer what the assistant cited, then what the user named, then retrieved passages for a refused turn.
            documents = cited or named or list(dict.fromkeys(retrieved))
            # A general answer clears the prior document scope, so a later follow-up does not
            # jump back to an older cited document.
            focus = documents
            # A question that got no answer still sets the topic for "why?" or "?" that follows it.
            history.append(Turn(
                question_message.content,
                NO_ANSWER_NOTE if answer.insufficient_evidence else answer.content,
                grounded=bool(cited) and not answer.insufficient_evidence,
                documents=tuple(documents),
                general=bool(answer.payload.get("general_knowledge")) and not answer.insufficient_evidence,
            ))
        # Focus IDs are only hints: retrieval re-authorises every document with current permissions.
        return _select_history_context(history, question or "", turns), focus

    # ------------------------------------------------------------- mutations

    async def create(self, title: str | None = None) -> Conversation:
        conversation = Conversation(user_id=self.user.id, title=title or DEFAULT_TITLE)
        self.db.add(conversation)
        await self.db.commit()
        await self.db.refresh(conversation)
        return conversation

    async def rename(self, conversation_id: uuid.UUID, title: str) -> Conversation:
        conversation = await self.get(conversation_id, lock=True)
        conversation.title = title
        await self.db.commit()
        await self.db.refresh(conversation)
        return conversation

    async def delete(self, conversation_id: uuid.UUID) -> None:
        conversation = await self.get(conversation_id, lock=True)
        await self.db.delete(conversation)  # messages go with it (ON DELETE CASCADE)
        audit.record(self.db, self.ctx, "conversation.deleted", target=str(conversation_id))  # no content
        await self.db.commit()

    async def append(
        self, conversation_id: uuid.UUID, question: str, response: RagChatResponse
    ) -> tuple[Conversation, ConversationMessage, ConversationMessage]:
        """Store a completed question/answer pair (nothing is stored if answering failed)."""
        conversation = await self.get(conversation_id, lock=True)
        if await self.count(conversation) + 2 > self.settings.chat_max_messages_per_conversation:
            raise ConflictError("This conversation is full. Start a new chat to continue.")
        user_message = ConversationMessage(conversation_id=conversation.id, role="user", content=question)
        assistant_message = ConversationMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=response.answer,
            provider=response.provider,
            model=response.model,
            strategy=response.strategy,
            insufficient_evidence=response.insufficient_evidence,
            payload=response.model_dump(mode="json", include=_RESPONSE_PAYLOAD_FIELDS)
            | {"source_document_ids": [str(i) for i in response.source_document_ids]}
            | {"named_document_ids": [str(i) for i in response.named_document_ids]},
        )
        self.db.add(user_message)
        await self.db.flush()  # user message gets the lower ID, so ordering by ID is chronological
        self.db.add(assistant_message)
        if conversation.title == DEFAULT_TITLE:
            conversation.title = title_from_question(question)
        conversation.updated_at = datetime.now(UTC)
        await self.db.commit()
        await self.db.refresh(conversation)
        return conversation, user_message, assistant_message

    async def set_feedback(self, conversation_id: uuid.UUID, message_id: int, rating: int | None) -> MessageOut:
        """The owner rates one of their assistant answers. The trace of that answer is linked by message ID."""
        conversation = await self.get(conversation_id)
        message = (
            await self.db.execute(
                select(ConversationMessage).where(
                    ConversationMessage.id == message_id,
                    ConversationMessage.conversation_id == conversation.id,
                    ConversationMessage.role == "assistant",
                )
            )
        ).scalar_one_or_none()
        if message is None:
            raise NotFoundError("Message not found.")
        message.feedback = rating
        await self.db.commit()
        return (await self.messages_out([message]))[0]

    # ----------------------------------------------------------- serialising

    @staticmethod
    def summary(conversation: Conversation, message_count: int) -> ConversationOut:
        return ConversationOut(
            id=conversation.id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            message_count=message_count,
        )

    async def _readable(self, document_ids: set[uuid.UUID]) -> set[uuid.UUID]:
        if not document_ids:
            return set()
        rows = await self.db.execute(
            select(Document.id).where(Document.id.in_(document_ids), access.readable_by(self.user))
        )
        return set(rows.scalars())

    async def messages_out(self, messages: list[ConversationMessage]) -> list[MessageOut]:
        """Serialise messages with the user's current permissions (see the module docstring)."""
        sources = set().union(*(answer_sources(m) for m in messages if m.role == "assistant"))
        readable = await self._readable(sources)
        return [self._message_out(m, readable) for m in messages]

    @staticmethod
    def _message_out(message: ConversationMessage, readable: set[uuid.UUID]) -> MessageOut:
        if message.role == "user":
            return MessageOut(id=message.id, role="user", content=message.content, created_at=message.created_at)
        payload = message.payload
        unavailable = answer_sources(message) - readable
        withheld = bool(unavailable)
        citations = []
        for raw in payload.get("citations", []):
            citation = Citation(**raw)
            if citation.document_id not in readable:
                citation = citation.model_copy(
                    update={"available": False, "filename": "Unavailable document", "snippet": ""}
                )
            citations.append(citation)
        page_images = [img for img in payload.get("page_images", []) if uuid.UUID(img["document_id"]) in readable]
        answer = WITHHELD_ANSWER if withheld else message.content
        response = RagChatResponse(
            answer=answer,
            insufficient_evidence=bool(message.insufficient_evidence),
            citations=citations,
            retrieved_chunks=payload.get("retrieved_chunks", 0),
            strategy=message.strategy or "none",
            chart=None if withheld else payload.get("chart"),
            page_images=page_images,
            model=message.model,
            provider=message.provider,
            memories_applied=payload.get("memories_applied", 0),
            general_knowledge=payload.get("general_knowledge", False),
        )
        return MessageOut(
            id=message.id,
            role="assistant",
            content=answer,
            created_at=message.created_at,
            response=response,
            sources_unavailable=len(unavailable),
            withheld=withheld,
            feedback=message.feedback,
        )
