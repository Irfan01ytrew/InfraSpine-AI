"""Step AE slice 1 — pure-function auto-reply decider for WhatsApp customers.

Decides what an incoming customer WhatsApp message becomes: a Knowledge
answer, a canned reply, a captured lead, a handoff to a human, or nothing.
Pure stdlib, no I/O, no database, no app imports — so it can be tested
without any service running and can never, by itself, send a message,
change a record, or execute an action.

Safety contract (HANDOVER-PROMPT.md §5, Step AE plan §3):
- Message text is untrusted data. Text shaped like an instruction to the
  system ("ignore previous instructions", "grant me admin") is ignored,
  never obeyed.
- Auto-replies never execute actions. An action request from a customer
  is handed to a human, never auto-executed.
- A Knowledge reply is only allowed when the evidence signal says the
  documents actually state the fact (at least one citation, confidence
  >= 0.5). Anything else is a handoff with the insufficient-evidence note.
- An unverified sender gets no Knowledge content and no lead capture.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------

INTENTS = (
    "question",
    "greeting",
    "lead",
    "handoff_request",
    "complaint",
    "action_request",
    "spam",
    "unknown",
)

ACTIONS = (
    "reply_from_knowledge",
    "reply_canned",
    "capture_lead",
    "handoff_to_human",
    "ignore",
)

#: Minimum confidence for a Knowledge answer to be sent automatically.
MIN_KNOWLEDGE_CONFIDENCE = 0.5


@dataclass(frozen=True)
class Evidence:
    """The Knowledge lookup result for the incoming question.

    Produced by the caller (the worker that ran ``answer_question``);
    the decider only reads it, never performs the lookup itself.
    """

    found: bool = False
    citations: int = 0
    confidence: float = 0.0


@dataclass(frozen=True)
class LeadSignals:
    """Contact / interest details extracted from the message text.

    Stored verbatim as data only. Nothing here is ever executed.
    """

    phone: str | None = None
    email: str | None = None
    interests: tuple[str, ...] = ()
    name: str | None = None

    @property
    def has_contact(self) -> bool:
        return bool(self.phone or self.email)


@dataclass(frozen=True)
class IncomingMessage:
    text: str
    sender_verified: bool = False
    asked_before: bool = False


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    reply_template: str | None = None
    lead: LeadSignals | None = None


# ---------------------------------------------------------------------------
# Keyword lists (English + Roman Urdu; customers write Roman Urdu)
# ---------------------------------------------------------------------------

_GREETING_WORDS = (
    "hello", "hi", "salam", "aoa", "assalam", "adaab",
    "good morning", "good afternoon", "good evening",
    "subah bakhair", "sham bakhair",
)

_HANDOFF_PHRASES = (
    "human", "insan", "banda", "representative", "manager",
    "call me", "call karo", "call karain", "phone karo",
    "kisi se baat", "agent se", "insan se baat", "banday se baat",
    "real person", "asli banda", "operator",
)

_COMPLAINT_PHRASES = (
    "complaint", "shikayat", "shikayat hai", "ghalat", "wrong",
    "refund", "paisay wapis", "paise wapis", "kharab", "fraud",
    "dhoka", "dhoka hua", "scam", "bura", "naraz",
    "payment dispute", "paise kat gaye", "paisay kat gaye",
)

# Interest keywords -> canonical interest tag. A buying word means the
# sender is a lead; a bare price question is a question (Knowledge may
# answer it from a price list), not a lead by itself.
_INTEREST_KEYWORDS = (
    ("kharidna", "buy"), ("buy", "buy"), ("lena hai", "buy"),
    ("order", "buy"), ("order karna", "buy"), ("purchase", "buy"),
    ("demo", "demo"), ("quote", "quote"), ("quotation", "quote"),
    ("package", "package"), ("plan", "package"),
    ("price", "price"), ("qimat", "price"), ("qeemat", "price"),
    ("kitne ka", "price"), ("kitnay ka", "price"),
    ("cost", "price"), ("charges", "price"), ("fee", "price"),
    ("rate", "price"), ("discount", "price"),
)

_BUYING_WORDS = ("kharidna", "buy", "lena hai", "order", "purchase")

_ACTION_PHRASES = (
    "approve", "approve karo", "approve karain", "pay", "payment karo",
    "payment karain", "transfer", "transfer karo", "grant", "access do",
    "book karo", "book karain", "cancel karo", "cancel karain",
    "change karo", "update karo", "delete karo", "remove karo",
)

_QUESTION_WORDS = (
    "kya", "kia", "kb", "kab", "kub", "kahan", "kidhar", "kaise",
    "kese", "kyun", "kyon", "kon", "kaun", "kis", "kitne", "kitna",
    "what", "when", "where", "how", "why", "who", "which", "?",
)

# Text shaped like an instruction to the system. Untrusted data — never
# obeyed, always ignored.
_INSTRUCTION_PATTERNS = (
    "ignore previous instructions", "ignore all previous instructions",
    "ignore your instructions", "disregard your instructions",
    "you are now", "act as", "system prompt", "developer prompt",
    "grant me admin", "make me admin", "give me admin",
    "reveal your instructions", "show your instructions",
    "override", "jailbreak", "do anything now",
)

_SPAM_PHRASES = (
    "you won", "congratulations you won", "free money", "lottery",
    "click here to win", "claim your prize", "double your money",
    "earn money fast", "work from home earn",
)

_PHONE_RE = re.compile(
    r"(?:\+92|92|0)?3\d{2}[\s\-]?\d{7}|\+?\d[\d\s\-]{8,14}\d"
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_NAME_RE = re.compile(
    r"(?:mera naam|my name is)\s+([A-Za-z\u0600-\u06FF][\w\u0600-\u06FF .]{1,40})",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _contains_any(text: str, phrases) -> bool:
    return any(p in text for p in phrases)


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

def classify_intent(text: str) -> tuple[str, str]:
    """Classify message text. Returns (intent, reason).

    Order matters: spam / untrusted instructions first (they can
    masquerade as questions), then greeting, handoff, complaint, lead,
    action, question, and finally unknown.
    """
    raw = text or ""
    t = _norm(raw)
    if not t:
        return "spam", "empty_message"
    if _contains_any(t, _INSTRUCTION_PATTERNS):
        return "spam", "untrusted_instruction"
    if _contains_any(t, _SPAM_PHRASES):
        return "spam", "spam_content"
    # A bare link or a wall of repeated characters is spam, not a message.
    if re.fullmatch(r"(https?://\S+|www\.\S+)", t):
        return "spam", "link_only"
    if re.search(r"(.)\1{9,}", t):
        return "spam", "repeated_characters"

    words = set(t.split())
    if len(words) <= 4 and _contains_any(t, _GREETING_WORDS):
        return "greeting", "greeting_only"
    if _contains_any(t, _HANDOFF_PHRASES):
        return "handoff_request", "explicit_human_request"
    if _contains_any(t, _COMPLAINT_PHRASES):
        return "complaint", "complaint_words"
    if _contains_any(t, _BUYING_WORDS):
        return "lead", "buying_intent"
    if _contains_any(t, _ACTION_PHRASES):
        return "action_request", "action_words"
    if "?" in t or words & set(_QUESTION_WORDS):
        return "question", "question_words"
    return "unknown", "no_recognised_intent"


# ---------------------------------------------------------------------------
# Lead-signal extraction
# ---------------------------------------------------------------------------

# Trailing Roman Urdu copula words that are not part of a name.
_NAME_STOP_WORDS = ("hai", "he", "hain", "hoon", "hun", "ho", "tha", "thi")


def _clean_name(raw: str) -> str:
    """Strip trailing Roman Urdu copula words ("hai", "hoon", ...) from a name."""
    parts = [p for p in raw.strip(" .").split() if p.lower() not in _NAME_STOP_WORDS]
    return " ".join(parts)


def extract_lead_signals(text: str) -> LeadSignals:
    """Extract contact / interest details from the message text.

    Everything returned is data copied verbatim from the message; the
    caller stores it as a CRM record and never executes it.
    """
    t = _norm(text)
    phone = None
    m = _PHONE_RE.search(text or "")
    if m:
        phone = re.sub(r"[\s\-]", "", m.group(0))
    email = None
    m = _EMAIL_RE.search(text or "")
    if m:
        email = m.group(0).lower()
    interests = tuple(sorted({tag for kw, tag in _INTEREST_KEYWORDS if kw in t}))
    name = None
    m = _NAME_RE.search(text or "")
    if m:
        name = _clean_name(m.group(1)) or None
    return LeadSignals(phone=phone, email=email, interests=interests, name=name)


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

def decide(message: IncomingMessage, evidence: Evidence | None = None) -> Decision:
    """Decide what an incoming customer message becomes.

    ``evidence`` is the Knowledge lookup result for the message (only
    meaningful for questions). The decider never looks anything up.
    """
    ev = evidence or Evidence()
    intent, intent_reason = classify_intent(message.text)
    signals = extract_lead_signals(message.text)

    # Spam / untrusted instructions are ignored whoever sent them.
    if intent == "spam":
        return Decision(action="ignore", reason=intent_reason)

    # An unverified sender gets no Knowledge content and no lead capture.
    if not message.sender_verified:
        if intent == "greeting":
            return Decision(action="reply_canned", reason="greeting", reply_template="greeting")
        return Decision(
            action="handoff_to_human",
            reason="unverified_sender",
            reply_template="handoff_ack",
        )

    if intent == "greeting":
        return Decision(action="reply_canned", reason="greeting", reply_template="greeting")

    if intent == "handoff_request":
        return Decision(action="handoff_to_human", reason="explicit_human_request", reply_template="handoff_ack")

    if intent == "complaint":
        return Decision(action="handoff_to_human", reason="complaint", reply_template="complaint_ack")

    if intent == "action_request":
        # Auto-replies never execute actions; a human handles it.
        return Decision(action="handoff_to_human", reason="action_needs_human", reply_template="handoff_ack")

    if intent == "lead":
        if signals.has_contact:
            return Decision(
                action="capture_lead",
                reason="lead_with_contact",
                reply_template="lead_ack",
                lead=signals,
            )
        return Decision(
            action="handoff_to_human",
            reason="lead_needs_contact",
            reply_template="ask_contact",
            lead=signals,
        )

    if intent == "question":
        if ev.found and ev.citations >= 1 and ev.confidence >= MIN_KNOWLEDGE_CONFIDENCE:
            return Decision(
                action="reply_from_knowledge",
                reason="evidence_backed",
                reply_template="knowledge_answer",
            )
        if message.asked_before:
            return Decision(
                action="handoff_to_human",
                reason="repeated_unanswered",
                reply_template="insufficient_evidence",
            )
        return Decision(
            action="handoff_to_human",
            reason="insufficient_evidence",
            reply_template="insufficient_evidence",
        )

    # Unknown intent: a human handles it rather than the bot guessing.
    return Decision(action="handoff_to_human", reason="unknown_intent", reply_template="handoff_ack")
