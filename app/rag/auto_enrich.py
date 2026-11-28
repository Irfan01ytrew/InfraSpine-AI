"""Step AB slice 1 — pure-function Knowledge enrichment core.

Auto-tagging, extractive summarization, expiry-date extraction and answer
language detection for uploaded documents.

Design rules (see ``docs/step-ab-knowledge-plan.md``):

- Pure stdlib, no I/O, no DB, no network, no LLM. Deterministic and cheap.
- Extractive only: summaries quote sentences from the document itself; tags come
  from an allow-listed keyword taxonomy; dates must be found in the text next
  to an expiry keyword. Nothing is ever guessed.
- Document text is untrusted input. The outputs are *derived metadata about*
  the text; they can never grant permissions, change payees, or change what an
  approval covers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

__all__ = [
    "TAG_TAXONOMY",
    "extract_tags",
    "summarize_text",
    "extract_expiry_dates",
    "expiry_alerts",
    "detect_language",
    "answer_language",
]

# ---------------------------------------------------------------------------
# Auto-tagging
# ---------------------------------------------------------------------------

# tag -> keywords that evidence it (English + Roman Urdu where the owner uses it).
# A tag fires only when one of its keywords appears as a whole word/phrase.
TAG_TAXONOMY: dict[str, tuple[str, ...]] = {
    "contract": (
        "contract", "agreement", "party", "parties", "hereby agrees", "signatory",
        "terms and conditions", "governing law", "termination clause",
        "muahida", "agreement",
    ),
    "invoice": (
        "invoice", "invoice number", "invoice date", "amount due", "payment due",
        "bill to", "tax invoice", "challan",
    ),
    "finance": (
        "payment", "bank", "account number", "iban", "amount", "balance",
        "transaction", "receipt", "refund", "budget", "expense",
    ),
    "tax": ("tax", "gst", "sales tax", "income tax", "withholding", "fbr", "ntn"),
    "hr": (
        "employee", "salary", "leave", "attendance", "designation", "joining date",
        "probation", "resignation", "staff", "payroll",
    ),
    "policy": (
        "policy", "procedure", "guideline", "code of conduct", "compliance",
        "standard operating procedure", "sop",
    ),
    "meeting": (
        "meeting", "minutes", "agenda", "attendees", "action items", "discussed",
        "decisions",
    ),
    "report": (
        "report", "summary", "findings", "quarterly", "annual report", "analysis",
        "overview",
    ),
    "legal": (
        "legal", "court", "litigation", "notice", "affidavit", "notary",
        "arbitration", "clause", "liability",
    ),
    "technical": (
        "architecture", "specification", "api", "database", "deployment",
        "configuration", "system requirements", "manual",
    ),
    "proposal": (
        "proposal", "quotation", "quote", "tender", "bid", "scope of work",
        "deliverables",
    ),
    "vendor": ("vendor", "supplier", "purchase order", "procurement", "quotation"),
}


def _word_hit(text: str, phrase: str) -> bool:
    return re.search(r"\b" + re.escape(phrase) + r"\b", text, re.IGNORECASE) is not None


def extract_tags(text: str, *, max_tags: int = 8) -> list[str]:
    """Return document tags evidenced by the text, best-evidenced first.

    Pure keyword taxonomy over the text. Empty or gibberish text yields no
    tags — the absence of evidence is reported, never invented.
    """
    if not text or not text.strip():
        return []
    scored: list[tuple[int, str]] = []
    for tag, keywords in TAG_TAXONOMY.items():
        hits = sum(1 for kw in keywords if _word_hit(text, kw))
        if hits:
            scored.append((hits, tag))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [tag for _, tag in scored[:max_tags]]


# ---------------------------------------------------------------------------
# Extractive summary
# ---------------------------------------------------------------------------

_ABBREVIATIONS = ("mr", "mrs", "ms", "dr", "e.g", "i.e", "vs", "etc", "no", "st")

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


def _sentences(text: str) -> list[str]:
    """Split text into sentences, keeping common abbreviations intact."""
    flat = " ".join(text.split())
    if not flat:
        return []
    parts = _SENTENCE_SPLIT.split(flat)
    sentences: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Rejoin a split that happened right after an abbreviation.
        if sentences and sentences[-1].lower().rstrip(".").endswith(_ABBREVIATIONS):
            sentences[-1] = sentences[-1] + " " + part
        else:
            sentences.append(part)
    return sentences


def summarize_text(text: str, *, max_sentences: int = 3) -> str:
    """Return an extractive summary: up to ``max_sentences`` sentences copied
    verbatim from the text, in their original order.

    Sentences are scored by taxonomy-keyword density with a bonus for early
    position (introductions usually state the point). A short text is returned
    as-is; empty text yields an empty summary.
    """
    sentences = _sentences(text or "")
    if not sentences:
        return ""
    if len(sentences) <= max_sentences:
        return " ".join(sentences)

    keywords: set[str] = set()
    for words in TAG_TAXONOMY.values():
        keywords.update(w.lower() for w in words)

    scored: list[tuple[float, int, str]] = []
    for idx, sentence in enumerate(sentences):
        words = re.findall(r"[a-z]+", sentence.lower())
        if not words:
            continue
        keyword_hits = sum(1 for w in words if w in keywords)
        density = keyword_hits / len(words)
        position_bonus = 1.0 / (idx + 1)
        # Very long sentences make poor summaries.
        length_penalty = 1.0 if len(words) <= 40 else 0.5
        score = (density * 2.0 + position_bonus) * length_penalty
        scored.append((score, idx, sentence))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen = sorted(scored[:max_sentences], key=lambda item: item[1])
    return " ".join(sentence for _, _, sentence in chosen)


# ---------------------------------------------------------------------------
# Expiry dates
# ---------------------------------------------------------------------------

_EXPIRY_KEYWORDS = (
    "expiry", "expires", "expiration", "valid until", "valid till", "valid up to",
    "expiry date", "expiration date", "renewal date", "contract end", "validity",
    "end date", "termination date", "due date", "khatam", "miad", "meead",
    "میعاد",
)

_MONTHS = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
)
_MONTH_ABBR = ("jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec")

_MONTH_NUM = {name: i + 1 for i, name in enumerate(_MONTHS)}
_MONTH_NUM.update(
    {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
        "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    }
)

_DATE_PATTERNS = (
    # 2026-03-15 / 2026/03/15
    re.compile(r"\b(?P<y>\d{4})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})\b"),
    # 15/03/2026 or 15-03-2026 (day first, PK locale)
    re.compile(r"\b(?P<d>\d{1,2})[/-](?P<m>\d{1,2})[/-](?P<y>\d{4})\b"),
    # 31 December 2026 / 31 Dec 2026
    re.compile(
        r"\b(?P<d>\d{1,2})\s+(?P<mon>[A-Za-z]+)\s+(?P<y>\d{4})\b",
    ),
    # December 31, 2026 / Dec 31 2026
    re.compile(
        r"\b(?P<mon>[A-Za-z]+)\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>\d{4})\b",
    ),
)

# Characters of text around an expiry keyword in which a date must appear.
_DATE_WINDOW = 80


@dataclass(frozen=True)
class ExpiryDate:
    """A date found in the text next to an expiry keyword.

    ``date`` is the parsed calendar date; ``label`` is the keyword that
    evidenced it; ``context`` is the surrounding snippet (for display only).
    """

    date: date
    label: str
    context: str

    def as_dict(self) -> dict:
        return {"date": self.date.isoformat(), "label": self.label, "context": self.context}


def _parse_match(match: re.Match) -> date | None:
    try:
        year = int(match.group("y"))
        month = int(match.group("m"))
        day = int(match.group("d"))
    except (IndexError, ValueError):
        return None
    if not (1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_month_name_match(match: re.Match) -> date | None:
    mon = match.group("mon").lower()
    month = _MONTH_NUM.get(mon)
    if month is None:
        return None
    try:
        year = int(match.group("y"))
        day = int(match.group("d"))
    except (IndexError, ValueError):
        return None
    if not (1900 <= year <= 2100 and 1 <= day <= 31):
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def extract_expiry_dates(text: str) -> list[ExpiryDate]:
    """Find expiry dates in the text.

    A date counts only when it appears within ``_DATE_WINDOW`` characters of
    an expiry keyword ("valid until", "expiry", ...). A bare date with no
    expiry keyword nearby is ignored — it may be an invoice date, a birth
    date, anything. No date found means no expiry, never a guessed one.
    """
    if not text or not text.strip():
        return []
    found: dict[date, ExpiryDate] = {}
    lowered = text.lower()
    for keyword in _EXPIRY_KEYWORDS:
        start = 0
        while True:
            at = lowered.find(keyword, start)
            if at < 0:
                break
            window_start = max(0, at - 20)
            window_end = min(len(text), at + len(keyword) + _DATE_WINDOW)
            window = text[window_start:window_end]
            for pattern in _DATE_PATTERNS[:2]:
                for match in pattern.finditer(window):
                    parsed = _parse_match(match)
                    if parsed and parsed not in found:
                        found[parsed] = ExpiryDate(
                            date=parsed,
                            label=keyword,
                            context=" ".join(window.split()),
                        )
            for pattern in _DATE_PATTERNS[2:]:
                for match in pattern.finditer(window):
                    parsed = _parse_month_name_match(match)
                    if parsed and parsed not in found:
                        found[parsed] = ExpiryDate(
                            date=parsed,
                            label=keyword,
                            context=" ".join(window.split()),
                        )
            start = at + len(keyword)
    return sorted(found.values(), key=lambda item: item.date)


# ---------------------------------------------------------------------------
# Expiry alerts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExpiryAlert:
    """Urgency of one expiry date relative to a reference date."""

    date: date
    days_left: int
    urgency: str  # "expired" | "due_soon" | "ok"

    def as_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "days_left": self.days_left,
            "urgency": self.urgency,
        }


def expiry_alerts(
    dates: list[ExpiryDate] | list[date],
    *,
    reference_date: date | None = None,
    warn_days: int = 30,
) -> list[ExpiryAlert]:
    """Classify each expiry date: expired, due soon (within ``warn_days``),
    or ok. ``reference_date`` defaults to today."""
    ref = reference_date or date.today()
    alerts: list[ExpiryAlert] = []
    for item in dates:
        day = item.date if isinstance(item, ExpiryDate) else item
        days_left = (day - ref).days
        if days_left < 0:
            urgency = "expired"
        elif days_left <= warn_days:
            urgency = "due_soon"
        else:
            urgency = "ok"
        alerts.append(ExpiryAlert(date=day, days_left=days_left, urgency=urgency))
    return sorted(alerts, key=lambda a: a.date)


# ---------------------------------------------------------------------------
# Answer language
# ---------------------------------------------------------------------------

_URDU_SCRIPT = re.compile(r"[\u0600-\u06ff]")

# Roman Urdu words (Latin script). Extends the word lists already used in
# app/rag/chat.py so detection matches how the owner actually writes.
_ROMAN_URDU_WORDS = (
    "kya", "kia", "kiya", "btao", "batao", "btaen", "batain", "samjhao", "samjhaen",
    "kitna", "kitni", "kitne", "ktna", "ktni", "ktne", "kab", "kb", "kahan",
    "kidhar", "kis", "kon", "kaun", "kyun", "kaise", "kaisa", "kahan", "mera",
    "meri", "mere", "mai", "main", "mein", "mujhe", "mujh", "aap", "tum",
    "hain", "hai", "tha", "thi", "hoga", "hogi", "chahiye", "chahye", "karo",
    "karen", "karein", "krna", "krdo", "dikhao", "dekho", "parho", "likha",
    "likhi", "yeh", "ye", "wo", "woh", "iss", "ka", "ki", "ke",
    "mein", "aur", "ya", "nahi", "nahin", "agla", "agli", "agle", "pichla",
    "pichli", "mahina", "saal", "din", "aaj", "kal", "parso", "abhi", "bhi",
    "kuch", "sara", "sari", "sab", "kitab", "hisab", "paisa", "paise",
)

_ROMAN_URDU_RE = re.compile(
    r"\b(?:" + "|".join(sorted(set(_ROMAN_URDU_WORDS))) + r")\b", re.IGNORECASE
)


def detect_language(text: str) -> str:
    """Detect the language of a question: ``"urdu"`` (Urdu script),
    ``"roman_urdu"`` (Latin-script Urdu), or ``"english"``.

    Mixed Roman Urdu + English counts as Roman Urdu — the owner writes that
    way, and the answer should come back the same way.
    """
    if not text:
        return "english"
    if _URDU_SCRIPT.search(text):
        return "urdu"
    if _ROMAN_URDU_RE.search(text):
        return "roman_urdu"
    return "english"


def answer_language(question: str) -> str:
    """Language the answer should be written in: mirror the question.

    Presentation only — it never changes the facts, sources, or the
    evidence gate.
    """
    return detect_language(question)
