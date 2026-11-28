"""Roman Urdu founder voice-note intake: parsing + safety validation (Step H prep).

A voice-note transcript is **untrusted input**. It can describe what the
founder wants, but it can never grant a permission, change a payee, change
what an approval covers, or skip approval. Parsing therefore produces a
*request only* — ``FounderVoiceCommand`` — which must travel through the
unified workflow engine with an exact-action approval and ``execute_once``
before anything happens.

The parser is deliberately small and keyword-based for Roman Urdu + English.
It is not an authority: identity always comes from ``app/platform``
``resolve_principal``; permissions always come from ``authorize``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# Intent keyword sets (lowercased transcript, Roman Urdu + English).
_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "finance_spend_chart": ("kharcha", "kharacha", "spend", "chart", "graph", "kharch"),
    "invoice_verification": ("invoice", "verify", "verified", "tasdeeq", "verification"),
    "continuity_replacement": (
        "replacement", "badal", "tabdeeli", "continuity", "chhutti",
        "unavailable", "jagah", "replacement",
    ),
    "approval_request": ("approve", "approval", "manzoori", "ijazat", "payment", "transfer"),
}

# Permissions each intent needs. Verify / approve-payment / transfer are three
# SEPARATE permissions on purpose (separation of duties, handover §5 rule 4).
# These strings must exist in the platform catalog: finance role map
# (app/finance/service.py) and SEPARATED_DUTIES (app/platform/access.py).
# Slice 1 named permissions that do not exist ('finance.invoices.verify',
# 'finance.payments.approve', 'finance.payments.transfer',
# 'continuity.replacement.request', 'governance.approve') — an adapter can
# never authorize against those, so they are corrected here. Continuity has
# no platform permission at all: it is authorized by the continuity tenant
# scope (owner/planner + department) inside the handler.
REQUIRED_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "finance_spend_chart": ("finance.read",),
    "invoice_verification": ("invoice.verify",),
    "continuity_replacement": (),
    "approval_request": ("payment.approve",),
    "unknown": (),
}

# Language that must never be honoured from a transcript. Any match rejects
# the whole command — the transcript cannot talk its way past approval, into a
# role, or into changing a payee.
_BLOCKED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bbina approval\b", "skip-approval"),
    (r"\bwithout approval\b", "skip-approval"),
    (r"\bskip approval\b", "skip-approval"),
    (r"\bapproval ke baghair\b", "skip-approval"),
    (r"\bbypass\b", "bypass"),
    (r"\badmin bana\b", "grant-role"),
    (r"\bmake me admin\b", "grant-role"),
    (r"\bpermission de\b", "grant-permission"),
    (r"\bgrant (me )?(a )?permission\b", "grant-permission"),
    (r"\bpayee (change|badal)", "change-payee"),
    (r"\baccount (change|badal)", "change-payee"),
    (r"\bignore (safety|rules|policy)\b", "ignore-safety"),
    (r"\bmain admin (hoon|hun)\b", "claim-admin"),
    (r"\bi am (the )?admin\b", "claim-admin"),
)


@dataclass(frozen=True)
class FounderVoiceCommand:
    """A parsed founder voice-note request. Never executable on its own."""

    intent: str
    action_text: str
    action_hash: str  # sha256 of the normalised transcript: binds any later
    # approval to this EXACT action; a different action has a different hash.
    required_permissions: tuple[str, ...] = ()
    requires_approval: bool = True
    trusted: bool = False  # transcripts are never trusted
    source: str = "voice_note"


@dataclass(frozen=True)
class VoiceValidation:
    status: str  # "requires_approval" | "rejected"
    reason: str | None = None
    blocked_kind: str | None = None


def _normalise(transcript: str) -> str:
    return re.sub(r"\s+", " ", transcript.strip().lower())


def _action_hash(normalised: str) -> str:
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _detect_intent(normalised: str) -> str:
    # Approval language wins only when no more specific intent matches first:
    # a spend-chart or invoice request that mentions "approve" is still that
    # request; a bare approval request is an approval.
    for intent in ("finance_spend_chart", "invoice_verification", "continuity_replacement"):
        if any(kw in normalised for kw in _INTENT_KEYWORDS[intent]):
            return intent
    if any(kw in normalised for kw in _INTENT_KEYWORDS["approval_request"]):
        return "approval_request"
    return "unknown"


def parse_founder_voice_command(transcript: str) -> FounderVoiceCommand:
    """Parse a (mocked or real) transcript into a request. Raises ValueError
    on an empty transcript — silence is not a command."""
    normalised = _normalise(transcript)
    if not normalised:
        raise ValueError("empty transcript is not a command")
    intent = _detect_intent(normalised)
    permissions = REQUIRED_PERMISSIONS[intent]
    if intent == "approval_request":
        if "transfer" in normalised:
            permissions = ("funds.transfer",)
        elif "payment" in normalised:
            permissions = ("payment.approve",)
    return FounderVoiceCommand(
        intent=intent,
        action_text=normalised,
        action_hash=_action_hash(normalised),
        required_permissions=permissions,
        requires_approval=True,
        trusted=False,
    )


def validate_voice_command(command: FounderVoiceCommand) -> VoiceValidation:
    """Safety gate: reject any command whose transcript tries to bypass
    approval, claim a role, or change a payee/account. Everything else still
    requires approval — validation never approves anything."""
    for pattern, kind in _BLOCKED_PATTERNS:
        if re.search(pattern, command.action_text):
            return VoiceValidation(
                status="rejected",
                reason=f"transcript contains blocked instruction ({kind})",
                blocked_kind=kind,
            )
    return VoiceValidation(status="requires_approval", reason="approval required")


def check_separation_of_duties(
    verifier: str, approver: str, transferrer: str
) -> tuple[bool, str | None]:
    """Separation of duties for one invoice batch: verify, approve-payment and
    transfer must be three different people. Takes opaque user ids (never
    names from a transcript)."""
    holders = {"verify": verifier, "approve-payment": approver, "transfer": transferrer}
    seen: dict[str, str] = {}
    for role, user_id in holders.items():
        if user_id in seen:
            return False, (
                f"separation of duties violated: {user_id} holds both "
                f"{seen[user_id]} and {role}"
            )
        seen[user_id] = role
    return True, None
