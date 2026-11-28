"""Step AE — fixed canned replies for WhatsApp auto-reply.

These are the default texts the decider's ``reply_template`` keys resolve
to. They are fixed strings for slices 2–3; slice 5 (policy surface) moves
them into owner-editable org settings. Nothing here is built from customer
message text: a template key selects one of these verbatim texts, so an
incoming message can never inject words into a reply.
"""

from __future__ import annotations

CANNED_REPLIES: dict[str, str] = {
    "greeting": (
        "Assalam-o-Alaikum! InfraSpine AI mein khush amdeed. "
        "Apna sawal likh kar bhej dein."
    ),
    "lead_ack": (
        "Shukriya! Aap ki maloomat mil gayi hai — hamari team jald rabta karegi."
    ),
    "ask_contact": (
        "Shukriya! Rabta ke liye apna phone number ya email bhej dein "
        "taake hamari team aap se rabta kar sake."
    ),
    "handoff_ack": (
        "Shukriya! Main aap ko hamari team se jod raha hoon — "
        "koi shakhs jald jawab dega."
    ),
    "complaint_ack": (
        "Afsoos hua. Aap ki shikayat hamari team tak pahuncha di gayi hai — "
        "koi shakhs jald rabta karega."
    ),
    "insufficient_evidence": (
        "Is baare mein hamare documents mein maloomat nahi hai. "
        "Kya main aap ko hamari team se jod dun?"
    ),
}

#: The fixed refusal sent when a Knowledge question cannot be answered
#: from the customer-visible documents. Never a guessed answer.
INSUFFICIENT_EVIDENCE_REPLY = CANNED_REPLIES["insufficient_evidence"]


def render(template: str) -> str:
    """Return the fixed canned text for a decider ``reply_template`` key.

    Raises ``KeyError`` for an unknown key: a reply is never improvised
    from message text.
    """
    return CANNED_REPLIES[template]
