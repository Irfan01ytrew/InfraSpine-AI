"""Deterministic intent parsing for founder voice notes.

The transcript is **untrusted data**: spoken words (or a doctored audio file) can
never grant a permission, change a payee, change what an approval covers, or select
an arbitrary handler. This parser only ever returns one of an allowlisted set of
intent kinds and fills only whitelisted parameters. Anything else — including
instruction-like phrases — maps to ``unknown``: no proposal, no action.

The parser is deliberately rule-based (no LLM) so its behaviour is exact and
testable. The owner writes Roman Urdu, so triggers cover English and Roman Urdu
wording.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

VoiceIntentKind = Literal["meeting_summary", "invoice_approve", "status_overview", "unknown"]

# Phrases that must never produce an action, however they are phrased. Checked
# before any intent trigger, so "approve all invoices" can never reach the
# invoice-approve intent.
SUSPICIOUS_PHRASES = (
    "ignore previous instructions",
    "ignore all previous",
    "ignore your instructions",
    "disregard your instructions",
    "disregard all previous",
    "you are now",
    "grant admin",
    "make me admin",
    "give me admin",
    "grant me access",
    "approve all",
    "approve everything",
    "approve every invoice",
    "pay vendor",
    "pay the vendor",
    "transfer money",
    "send money",
    "change the payee",
    "change payee",
    "do not ask",
    "don't ask",
    "dont ask",
    "without approval",
    "skip approval",
    "no approval needed",
    "system prompt",
    "override",
)

_SUMMARY_WORDS = ("summary", "summarise", "summarize", "minutes", "khulasa")
_STATUS_WORDS = ("status", "progress", "update", "kya chal raha", "kia chal raha",
                 "kya ho raha", "kia ho raha", "kya scene hai")

_STOPWORDS = {"approve", "approved", "ko", "ka", "ki", "please"}


@dataclass(frozen=True)
class VoiceIntent:
    """What a voice note asks for. ``kind == 'unknown'`` means: do nothing."""

    kind: VoiceIntentKind
    subject: str | None = None
    params: dict = field(default_factory=dict)
    reason: str | None = None


def _normalize(transcript: str) -> str:
    return re.sub(r"\s+", " ", transcript.lower()).strip()


def _invoice_ref(normalized: str) -> str | None:
    """Extract an invoice reference, or ``None`` when there is none.

    The ``INV-…`` form is searched first: ``\\binv`` followed by a mandatory
    dash/space, so the ``inv`` inside the word ``invoice`` itself can never
    match (it would otherwise capture ``oice`` as a fake reference).
    """
    match = re.search(r"\binv[- ]([a-z0-9][a-z0-9\-]*)", normalized)
    if match:
        return match.group(1)
    match = re.search(r"\binvoice\s+(?:no\.?|number|#)?\s*([a-z0-9][a-z0-9\-]*)", normalized)
    if match is None:
        return None
    ref = match.group(1)
    if ref in _STOPWORDS:
        return None
    return ref


def parse_voice_intent(transcript: str) -> VoiceIntent:
    """Map a transcript to an allowlisted intent, or ``unknown``.

    Never raises for strange input: unrecognised or suspicious text is an
    ``unknown`` intent, which callers must treat as "propose nothing".
    """
    if transcript is None or not str(transcript).strip():
        return VoiceIntent(kind="unknown", reason="empty transcript")
    normalized = _normalize(str(transcript))

    for phrase in SUSPICIOUS_PHRASES:
        if phrase in normalized:
            return VoiceIntent(kind="unknown", reason="suspicious instruction-like phrase")

    if "invoice" in normalized and "approve" in normalized:
        ref = _invoice_ref(normalized)
        if ref is None:
            return VoiceIntent(kind="unknown", reason="no invoice reference")
        return VoiceIntent(
            kind="invoice_approve",
            subject=f"invoice:{ref}",
            params={"invoice_ref": ref},
        )
    if "meeting" in normalized and any(word in normalized for word in _SUMMARY_WORDS):
        return VoiceIntent(kind="meeting_summary", params={})
    if any(word in normalized for word in _STATUS_WORDS):
        return VoiceIntent(kind="status_overview", params={})
    return VoiceIntent(kind="unknown", reason="no recognised intent")
