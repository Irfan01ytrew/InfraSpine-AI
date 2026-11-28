"""Voice reply text (Step O, slice 5): what the founder hears back.

The reply is built deterministically from the proposal's own stored state —
never from a model, never from the transcript. The text names exactly what
happened (or what is still needed) and nothing else:

* ``executed`` — the action ran; the text says what was done, using only the
  stored ``execution_result`` (counts for a status overview, the invoice
  number for an invoice approval).
* ``proposed`` — the request is waiting for someone else to approve it.
* ``approved`` — approved, execution in progress.
* ``rejected`` / ``expired`` / ``failed`` — nothing was done, and why.

All text is plain ASCII (it is also sent as the ``X-Voice-Reply-Text`` header
when audio is served), at most :data:`MAX_REPLY_CHARS` long. The transcript
excerpt is never included: the reply is about the outcome, not the words.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_REPLY_CHARS = 500
_MAX_REASON_CHARS = 200


@dataclass(frozen=True)
class VoiceReply:
    """The spoken (or shown) reply for one proposal."""

    text: str
    kind: str  # executed | proposed | approved | rejected | expired | failed


def _overview_sentence(overview: dict) -> str:
    runs = overview.get("workflow_runs") or {}
    batches = overview.get("invoice_batches") or {}
    pending = overview.get("pending_approvals") or 0
    total_runs = sum(int(v) for v in runs.values())
    total_batches = sum(int(v) for v in batches.values())
    return (f"Status overview: {total_runs} workflow runs, {total_batches} invoice "
            f"batches, {pending} approvals pending. Nothing was changed.")


def _human_reason(failure_reason: str | None) -> str:
    """The speakable part of a stored failure reason.

    The unified engine stores reasons as ``"<code>: <message>"`` (see
    ``execute_voice_proposal``), and the code is internal jargon a founder
    should never hear spoken. Strip one leading snake_case code prefix;
    anything else — including a plain human sentence that happens to contain
    a colon — is left untouched. Truncated to :data:`_MAX_REASON_CHARS`.
    """
    reason = (failure_reason or "").strip()
    if not reason:
        return ""
    head, sep, tail = reason.partition(": ")
    if sep and head and all(c.islower() or c.isdigit() or c == "_" for c in head):
        reason = tail.strip()
    return reason[:_MAX_REASON_CHARS]


def build_voice_reply(
    *,
    intent_kind: str,
    status: str,
    execution_result: dict | None = None,
    failure_reason: str | None = None,
) -> VoiceReply:
    """Build the reply for a proposal in the given display status.

    ``status`` is the derived display status from
    :func:`app.voice.records.display_status`. Unknown statuses fall back to
    the waiting message — a reply never claims an outcome it cannot prove.
    """
    result = execution_result or {}
    if status == "executed":
        if intent_kind == "status_overview":
            return VoiceReply(
                text="Done. " + _overview_sentence(result.get("overview") or {}),
                kind="executed")
        if intent_kind == "invoice_approve":
            number = str(result.get("invoice_number") or "").strip() or "the invoice"
            return VoiceReply(
                text=(f"Done. Your approval for invoice {number} is recorded. "
                      "The invoice status was not changed."),
                kind="executed")
        return VoiceReply(text="Done. Your voice request was executed.", kind="executed")
    if status == "approved":
        return VoiceReply(
            text="Approved. Executing your request now.", kind="approved")
    if status == "rejected":
        return VoiceReply(
            text="Your request was rejected. Nothing was done.", kind="rejected")
    if status == "expired":
        return VoiceReply(
            text="Your request expired before it was decided. "
                 "Please record a new voice note.",
            kind="expired")
    if status == "failed":
        reason = _human_reason(failure_reason)
        if reason:
            return VoiceReply(
                text=f"Could not do that: {reason}", kind="failed")
        return VoiceReply(
            text="Could not do that. Please record a new voice note.",
            kind="failed")
    return VoiceReply(
        text="Your request is waiting for approval by someone else. "
             "I will tell you when it is decided.",
        kind="proposed")
