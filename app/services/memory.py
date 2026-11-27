"""Personal memory (ADR-004 WP2).

* Owner-only: every query filters by the signed-in user; other users and admins get 404.
* Separate from organizational knowledge: memory is never embedded in the knowledge index, never
  cited and never used as evidence. It only shapes answers (format, terminology, personal context).
* No duplicates or conflicts: one active item per normalised content and per subject, enforced by
  partial unique indexes. Saving a new value for a subject supersedes the old one (kept as history);
  forgetting deletes the item together with its earlier versions.
* Bounded: at most `memory_max_items` active items per user, and at most `memory_context_chars` /
  `memory_context_items` of them are sent to the LLM per question (preferences first, then by relevance).
* Writes for one user are serialised with a transaction-scoped advisory lock.
* Logs and audit entries carry IDs and counts only, never memory content.
"""

import asyncio
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.db.models import MemoryItem, User
from app.rag.embeddings import Embedder
from app.services import audit
from app.services.audit import AuditContext

MAX_CONTENT_CHARS = 500
MAX_SUBJECT_CHARS = 100
ACTIVE, SUPERSEDED = "active", "superseded"
KINDS = ("preference", "terminology", "fact")
_KIND_ORDER = {"preference": 0, "terminology": 1, "fact": 2}
MEMORY_PAGE_HINT = "You can view, edit or delete your memories on the **Memory** page."
logger = logging.getLogger(__name__)

_AUTO_SENSITIVE = re.compile(
    r"\b(password|passcode|secret|api\s*key|token|otp|one.time.code|credit\s+card|bank\s+account|ssn|social\s+security)\b",
    re.IGNORECASE,
)
_AUTO_PREFERENCE = re.compile(
    r"^(?:i\s+(?:prefer|like|want|would\s+like)|please\s+(?:answer|reply|respond|write|use|keep)|"
    r"always\s+(?:answer|reply|respond|write|use)|from\s+now\s+on)\s+(.{5,180})[.!]*$",
    re.IGNORECASE,
)
_AUTO_DESIGNATION = re.compile(
    r"^(?:i\s+am|i'm|im|i\s+work\s+as|my\s+(?:job\s+title|designation|position)\s+is)\s+"
    r"(?:an?\s+)?(?P<value>[\w][\w .&/+\-]{2,90})[.!]*$",
    re.IGNORECASE,
)
_AUTO_DESIGNATION_CUE = re.compile(
    r"\b(engineer|developer|administrator|admin|manager|analyst|architect|consultant|designer|"
    r"technician|doctor|nurse|teacher|director|lead|specialist|officer|accountant|founder|student)\b",
    re.IGNORECASE,
)
_AUTO_PROJECT = re.compile(
    r"^(?:my\s+(?:current\s+)?project\s+is|i'm\s+(?:currently\s+)?(?:working\s+on|building)|"
    r"i\s+am\s+(?:currently\s+)?(?:working\s+on|building)|mera\s+project|meri\s+project)\s+"
    r"(?P<value>[^?.!]{3,140}?)(?:\s+hai|\s+he)?[.!]*$",
    re.IGNORECASE,
)
_AUTO_ROMAN_PROJECT = re.compile(
    r"^(?:main\s+)?(?P<value>[^?.!]{3,100}?)\s+project\s+(?:par|per|pe)\s+kaam\s+kar\s+raha\s+hoon[.!]*$",
    re.IGNORECASE,
)

# ----------------------------------------------------------------------------- chat commands

_VERB_SUFFIX = r"(?:o|na|en|ein|iye|ye)"
_REMEMBER = (
    re.compile(
        r"^(?:please\s+|plz\s+|pls\s+)?(?:remember|memori[sz]e|keep\s+in\s+mind|note\s+that|yaad\s+rakh" + _VERB_SUFFIX + r")"
        r"\b\s*[:,\-]?\s*(?:that\s+|ke\s+|ki\s+|to\s+)?(?P<text>.+)$",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"^(?:update|change|correct)\s+(?:my\s+)?memory\b\s*[:,\-]?\s*(?:that\s+|to\s+)?(?P<text>.+)$",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"^(?P<text>.+?)\s*[,.]?\s*yaad\s+rakh" + _VERB_SUFFIX + r"\s*[.!]*$", re.IGNORECASE | re.DOTALL),
)
_FROM_NOW_ON = re.compile(r"^(?:please\s+)?from\s+now\s+on\b", re.IGNORECASE)
_FORGET = (
    re.compile(
        r"^(?:please\s+)?(?:forget|bhool\s+ja" + _VERB_SUFFIX + r"|(?:delete|remove)\s+(?:the\s+|my\s+)?memory)"
        r"\b\s*[:,\-]?\s*(?:that\s+|about\s+|ke\s+|ki\s+)?(?P<text>.+)$",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"^(?P<text>.+?)\s*[,.]?\s*bhool\s+ja" + _VERB_SUFFIX + r"\s*[.!]*$", re.IGNORECASE | re.DOTALL),
)
_LIST = re.compile(
    r"^(?:what\s+do\s+you\s+(?:remember|know)(?:\s+about\s+me)?|(?:show|list)\s+(?:me\s+)?(?:all\s+)?my\s+memor(?:y|ies)"
    r"|my\s+memor(?:y|ies)|(?:tumhe|tumhein|aap\s+ko|apko)\s+mere\s+ba+re\s+m(?:ein|e|ain)\s+kya\s+yaad\s+hai)\s*[?.!]*$",
    re.IGNORECASE,
)
_SUBJECT = re.compile(
    r"^(?:my|mera|meri|mere)\s+(?P<subject>[^\W\d_][\w\s'\-]{0,58}?)\s+(?:is|are|=|hai|hain)\s+\S", re.IGNORECASE
)
_CALL_ME = re.compile(r"^(?:please\s+)?call\s+me\s+\S", re.IGNORECASE)
_SUBJECT_ONLY = re.compile(r"^(?:my|mera|meri|mere)\s+(?P<subject>[^\W\d_][\w\s'\-]{0,58}?)\s*[.!]*$", re.IGNORECASE)
_TERMINOLOGY = re.compile(r"\b(?:means|stands\s+for|refers\s+to|ka\s+matlab|when\s+i\s+say)\b", re.IGNORECASE)
_PREFERENCE = re.compile(
    r"^(?:i\s+(?:prefer|like|want|would\s+like|don'?t\s+like|do\s+not\s+like)|always|never|please|answer|reply|respond"
    r"|use|call\s+me|keep|write|give|show|don'?t|do\s+not|avoid|from\s+now\s+on|mujhe)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MemoryCommand:
    action: Literal["remember", "forget", "list"]
    text: str = ""


def parse_memory_command(question: str) -> MemoryCommand | None:
    """Explicit remember / update / forget / list requests. Questions ("remember the policy?") are not commands."""
    message = question.strip()
    if _LIST.match(message):
        return MemoryCommand("list")
    if message.endswith("?"):
        return None
    if _FROM_NOW_ON.match(message):
        return MemoryCommand("remember", message)
    for pattern in _FORGET:
        if match := pattern.match(message):
            return MemoryCommand("forget", match.group("text").strip())
    for pattern in _REMEMBER:
        if match := pattern.match(message):
            return MemoryCommand("remember", match.group("text").strip())
    return None


def clean_text(value: str) -> str:
    return " ".join(value.split()).strip()


def normalise(value: str) -> str:
    return re.sub(r"[\s.!]+$", "", clean_text(value).lower())


def content_key(content: str) -> str:
    return hashlib.sha256(normalise(content).encode()).hexdigest()


def infer_subject(content: str) -> str | None:
    if _CALL_ME.match(content):
        return "preferred name"
    match = _SUBJECT.match(content)
    return clean_text(match.group("subject")).lower() if match else None


def infer_kind(content: str) -> str:
    if _TERMINOLOGY.search(content):
        return "terminology"
    if _PREFERENCE.match(content):
        return "preference"
    return "fact"


def _tokens(value: str) -> set[str]:
    return {w for w in re.findall(r"\w+", value.lower()) if len(w) >= 3}


# ----------------------------------------------------------------------------- service


@dataclass(frozen=True)
class SaveResult:
    item: MemoryItem
    outcome: Literal["created", "duplicate", "superseded", "updated"]
    replaced: MemoryItem | None = None


class MemoryService:
    def __init__(self, db: AsyncSession, settings: Settings, user: User, ctx: AuditContext) -> None:
        self.db = db
        self.settings = settings
        self.user = user
        self.ctx = ctx

    # ----------------------------------------------------------------- reads

    async def list_items(self, include_superseded: bool = False) -> list[MemoryItem]:
        query = select(MemoryItem).where(MemoryItem.owner_user_id == self.user.id)
        if not include_superseded:
            query = query.where(MemoryItem.status == ACTIVE)
        rows = await self.db.execute(query.order_by(MemoryItem.status, MemoryItem.created_at.desc()))
        return list(rows.scalars())

    async def get(self, item_id: uuid.UUID) -> MemoryItem:
        item = (
            await self.db.execute(
                select(MemoryItem).where(MemoryItem.id == item_id, MemoryItem.owner_user_id == self.user.id)
            )
        ).scalar_one_or_none()
        if item is None:  # also for other users' items: never reveal they exist
            raise NotFoundError("Memory not found.")
        return item

    async def context(self, question: str, embedder: Embedder | None = None) -> list[str]:
        """The memories sent to the LLM for this question, within the item and character budgets."""
        items = [i for i in await self.list_items() if len(i.content) <= self.settings.memory_context_chars]
        items.sort(key=lambda i: _KIND_ORDER[i.kind])  # stable: newest first within each kind
        budget, limit = self.settings.memory_context_chars, self.settings.memory_context_items
        if sum(len(i.content) for i in items) > budget or len(items) > limit:
            preferences = [i for i in items if i.kind == "preference"]
            others = await self._rank(question, [i for i in items if i.kind != "preference"], embedder)
            items = preferences + others
        selected, used = [], 0
        for item in items:
            if len(selected) == limit or used + len(item.content) > budget:
                continue
            selected.append(item.content)
            used += len(item.content)
        return selected

    async def auto_capture(self, message: str, conversation_id: uuid.UUID | None = None) -> bool:
        """Save only explicit, stable first-person preferences, job titles and current project statements.

        This deliberately uses narrow patterns rather than summarizing arbitrary chat with another model: it avoids
        retaining secrets or inferred traits and keeps capture transparent through the existing Memory page.
        """
        text = clean_text(message)
        if not text or text.endswith("?") or _AUTO_SENSITIVE.search(text):
            return False
        subject = None
        kind = "fact"
        content = None
        if match := _AUTO_PREFERENCE.match(text):
            subject, kind, content = "response preferences", "preference", text
        elif match := _AUTO_DESIGNATION.match(text):
            value = clean_text(match.group("value"))
            if _AUTO_DESIGNATION_CUE.search(value) or re.search(r"\b(?:job\s+title|designation|position)\b", text, re.I):
                subject, content = "designation", f"The user's designation is {value}."
        elif match := _AUTO_PROJECT.match(text) or _AUTO_ROMAN_PROJECT.match(text):
            value = clean_text(match.group("value"))
            if value:
                subject, content = "current project", f"The user's current project is {value}."
        if not content:
            return False
        try:
            result = await self.remember(
                content, subject=subject, kind=kind, source="chat", conversation_id=conversation_id
            )
            return result.outcome != "duplicate"
        except (InvalidInputError, ConflictError) as exc:
            logger.info("Automatic memory skipped reason=%s", exc.code)
            return False

    async def _rank(self, question: str, items: list[MemoryItem], embedder: Embedder | None) -> list[MemoryItem]:
        if not items:
            return items
        if embedder is not None:
            try:
                vectors = await asyncio.to_thread(embedder.embed, [question] + [i.content for i in items])
                scores = vectors[1:] @ vectors[0]
                return [item for _, item in sorted(zip(scores, items, strict=True), key=lambda p: -float(p[0]))]
            except Exception:  # the embedding model is optional here: fall back to word overlap
                pass
        words = _tokens(question)
        return sorted(items, key=lambda i: -len(words & _tokens(i.content)))

    # ------------------------------------------------------------- mutations

    async def remember(
        self,
        content: str,
        *,
        subject: str | None = None,
        kind: str | None = None,
        source: Literal["chat", "manual"] = "manual",
        conversation_id: uuid.UUID | None = None,
    ) -> SaveResult:
        content, subject = self._validated(content, subject)
        subject = subject or infer_subject(content)
        kind = kind or infer_kind(content)
        await self._lock()
        active = await self.list_items()
        key = content_key(content)
        if duplicate := next((i for i in active if i.content_key == key), None):
            await self.db.commit()  # nothing to write; release the lock
            return SaveResult(duplicate, "duplicate")
        same_subject = next((i for i in active if subject and i.subject_key == _subject_key(subject)), None)
        if same_subject is None and len(active) >= self.settings.memory_max_items:
            raise ConflictError(
                f"You have reached the limit of {self.settings.memory_max_items} memories. Delete some first."
            )
        item = self._new(content, subject, kind, source, conversation_id, supersedes=same_subject)
        outcome = "superseded" if same_subject else "created"
        await self._save(item, outcome, replaced=same_subject)
        return SaveResult(item, outcome, same_subject)

    async def update(
        self, item_id: uuid.UUID, *, content: str | None, subject: str | None, kind: str | None, clear_subject: bool
    ) -> SaveResult:
        """Correct a memory: the edit becomes a new version that supersedes the old one."""
        await self._lock()
        old = await self.get(item_id)
        if old.status != ACTIVE:
            raise ConflictError("Only current memories can be edited.")
        new_content, new_subject = self._validated(
            content if content is not None else old.content,
            None if clear_subject else (subject if subject is not None else old.subject),
        )
        new_kind = kind or old.kind
        if (content_key(new_content), _subject_key(new_subject), new_kind) == (old.content_key, old.subject_key, old.kind):
            await self.db.commit()  # unchanged; release the lock
            return SaveResult(old, "duplicate")
        others = [i for i in await self.list_items() if i.id != old.id]
        if any(i.content_key == content_key(new_content) for i in others):
            raise ConflictError("You already have a memory with this content.")
        if new_subject and any(i.subject_key == _subject_key(new_subject) for i in others):
            raise ConflictError("Another memory already covers this subject. Edit or delete that one instead.")
        item = self._new(new_content, new_subject, new_kind, old.source, old.source_conversation_id, supersedes=old)
        await self._save(item, "updated", replaced=old)
        return SaveResult(item, "updated", old)

    async def delete(self, item_id: uuid.UUID) -> int:
        """Forget a memory together with its earlier versions."""
        await self._lock()
        item = await self.get(item_id)
        chain = text(
            "WITH RECURSIVE chain(id, supersedes_id) AS ("
            " SELECT id, supersedes_id FROM memory_items WHERE id = :id AND owner_user_id = :owner"
            " UNION SELECT m.id, m.supersedes_id FROM memory_items m JOIN chain c ON m.id = c.supersedes_id"
            " WHERE m.owner_user_id = :owner) SELECT id FROM chain"
        )
        ids = list((await self.db.execute(chain, {"id": item.id, "owner": self.user.id})).scalars())
        await self.db.execute(
            delete(MemoryItem).where(MemoryItem.id.in_(ids), MemoryItem.owner_user_id == self.user.id)
        )
        audit.record(self.db, self.ctx, "memory.deleted", target=str(item.id), details={"versions": len(ids)})
        await self.db.commit()
        return len(ids)

    async def delete_all(self) -> int:
        await self._lock()
        result = await self.db.execute(delete(MemoryItem).where(MemoryItem.owner_user_id == self.user.id))
        audit.record(self.db, self.ctx, "memory.cleared", details={"items": result.rowcount})
        await self.db.commit()
        return result.rowcount

    async def forget(self, description: str) -> tuple[MemoryItem | None, list[MemoryItem]]:
        """(forgotten item, ambiguous candidates). Matches by subject, then content, then word overlap."""
        active = await self.list_items()
        matches: list[MemoryItem] = []
        if match := _SUBJECT_ONLY.match(clean_text(description)):
            matches = [i for i in active if i.subject_key == _subject_key(match.group("subject"))]
        if not matches:
            wanted = normalise(description)
            matches = [i for i in active if i.content_key == content_key(description)] or [
                i for i in active if len(wanted) >= 4 and (wanted in normalise(i.content) or normalise(i.content) in wanted)
            ]
        if not matches:
            words = _tokens(description)
            scored = [(len(words & _tokens(i.content)) / max(1, len(words | _tokens(i.content))), i) for i in active]
            matches = [i for score, i in scored if score >= 0.5]
        if len(matches) != 1:
            return None, matches
        await self.delete(matches[0].id)
        return matches[0], []

    # ----------------------------------------------------------- chat commands

    async def handle_command(self, command: MemoryCommand, conversation_id: uuid.UUID | None = None) -> str:
        if command.action == "list":
            items = await self.list_items()
            if not items:
                return 'I don\'t have any memories about you yet. Say "remember that …" to add one.'
            lines = "\n".join(f"- {i.content}" for i in items)
            return f"Here is what I remember about you:\n\n{lines}\n\n{MEMORY_PAGE_HINT}"
        if command.action == "forget":
            forgotten, candidates = await self.forget(command.text)
            if forgotten is not None:
                return f'Done. I\'ve forgotten: "{forgotten.content}"'
            if candidates:
                lines = "\n".join(f"- {i.content}" for i in candidates[:10])
                return f"Several memories match. Please be more specific:\n\n{lines}\n\n{MEMORY_PAGE_HINT}"
            return f"I don't have a memory matching that. {MEMORY_PAGE_HINT}"
        try:
            result = await self.remember(command.text, source="chat", conversation_id=conversation_id)
        except (InvalidInputError, ConflictError) as exc:
            return exc.message
        if result.outcome == "duplicate":
            return f'I already remember that: "{result.item.content}"'
        if result.outcome == "superseded" and result.replaced is not None:
            return f'Updated. I\'ll now remember: "{result.item.content}" (instead of "{result.replaced.content}").'
        return f'Got it. I\'ll remember: "{result.item.content}"\n\n{MEMORY_PAGE_HINT}'

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _validated(content: str, subject: str | None) -> tuple[str, str | None]:
        content = clean_text(content)
        if not content:
            raise InvalidInputError("A memory cannot be empty.")
        if len(content) > MAX_CONTENT_CHARS:
            raise InvalidInputError(f"That is too long to remember (maximum {MAX_CONTENT_CHARS} characters).")
        subject = clean_text(subject).lower() if subject else None
        if subject and len(subject) > MAX_SUBJECT_CHARS:
            raise InvalidInputError(f"The subject is too long (maximum {MAX_SUBJECT_CHARS} characters).")
        return content, subject or None

    def _new(
        self, content: str, subject: str | None, kind: str, source: str,
        conversation_id: uuid.UUID | None, *, supersedes: MemoryItem | None,
    ) -> MemoryItem:
        if kind not in KINDS:
            raise InvalidInputError("kind must be 'preference', 'terminology' or 'fact'.")
        return MemoryItem(
            owner_user_id=self.user.id, kind=kind, subject=subject, subject_key=_subject_key(subject),
            content=content, content_key=content_key(content), status=ACTIVE, source=source,
            source_conversation_id=conversation_id, supersedes_id=supersedes.id if supersedes else None,
        )

    async def _save(self, item: MemoryItem, outcome: str, *, replaced: MemoryItem | None) -> None:
        if replaced is not None:
            replaced.status, replaced.superseded_at = SUPERSEDED, datetime.now(UTC)
            await self.db.flush()  # free the unique "active" slot before inserting the new version
        self.db.add(item)
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("This memory conflicts with another one. Reload and try again.") from None
        audit.record(
            self.db, self.ctx, f"memory.{outcome}", target=str(item.id),
            details={"kind": item.kind, "source": item.source, "replaced": str(replaced.id) if replaced else None},
        )
        await self.db.commit()
        await self.db.refresh(item)

    async def _lock(self) -> None:
        await self.db.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"memory:{self.user.id}", 0)))
        )


def _subject_key(subject: str | None) -> str | None:
    return normalise(subject)[:MAX_SUBJECT_CHARS] if subject else None
