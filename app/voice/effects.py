"""What a voice action *does* when its approval is consumed (Step O, slices 2-3).

Each intent has one defined, verifiable effect; anything else fails closed
(raises, never a best guess). The effects run inside :func:`app.platform.execution.execute_once`,
so a crash or a second worker replays the recorded result instead of acting twice.

Safety boundaries (deliberate, see docs/step-o-voice-plan.md):
* ``status_overview`` is read-only: it computes real counts for the department
  and stores them on the proposal. Nothing is changed.
* ``invoice_approve`` records the second-person approval decision against the
  exact invoice and verifies the invoice is in a payable state (``verified``,
  or already ``approved``/``paid``). It deliberately does NOT change the invoice
  status: that transition belongs to the payment-approval flow
  (``app/finance/handlers.py`` approves invoices only when their payment is
  approved), and flipping it here would bypass that flow and break payment
  grouping (which selects ``verified`` invoices). A future slice can wire this
  into payment approval when a payment exists for the invoice.
* ``meeting_summary`` cannot execute yet: the slice-1 parser captures no meeting
  reference, so no meeting can be identified without guessing. It fails closed
  with a clear reason until a meeting reference is captured (parser slice or
  meeting picker).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.db.finance_models import FinInvoice, FinInvoiceBatch
from app.db.voice_models import VoiceProposal
from app.db.workflow_engine_models import WorkflowRun
from app.platform.models import PlatformApprovalRequest

# Invoice numbers in the wild carry their prefix ("INV-42"); the voice parser
# stores the bare reference ("42"). Both spellings match the same invoice.
_INVOICE_PREFIX = "inv-"


async def compute_status_overview(db, *, group_id, scope_id) -> dict:
    """Real counts for one department: workflow runs, invoice batches, pending
    approvals. Read-only; safe to call from anywhere."""
    runs = (await db.execute(select(WorkflowRun.status, func.count()).where(
        WorkflowRun.scope_id == group_id).group_by(WorkflowRun.status))).all()
    batches = (await db.execute(select(FinInvoiceBatch.status, func.count()).where(
        FinInvoiceBatch.group_id == group_id).group_by(FinInvoiceBatch.status))).all()
    pending = await db.scalar(select(func.count()).select_from(PlatformApprovalRequest).where(
        PlatformApprovalRequest.scope_id == scope_id, PlatformApprovalRequest.status == "pending",
        PlatformApprovalRequest.expires_at > datetime.now(UTC)))
    return {
        "workflow_runs": {status: count for status, count in runs},
        "invoice_batches": {status: count for status, count in batches},
        "pending_approvals": pending or 0,
        "generated_at": datetime.now(UTC).isoformat(),
    }


async def find_invoice_for_voice(db, *, group_id, invoice_ref: str) -> FinInvoice | None:
    """The invoice a voice note names, in this department. Matches the bare
    reference and the ``INV-`` spelling; the newest match wins and is named in
    the result, so the record is never ambiguous about which invoice it means."""
    wanted = {invoice_ref.strip().lower(), f"{_INVOICE_PREFIX}{invoice_ref.strip().lower()}"}
    rows = (await db.scalars(select(FinInvoice).where(
        FinInvoice.group_id == group_id,
        func.lower(FinInvoice.invoice_number).in_(sorted(wanted)),
    ).order_by(FinInvoice.created_at.desc()))).all()
    return rows[0] if rows else None


def _money(amount: Decimal | None, currency: str | None) -> str | None:
    if amount is None:
        return None
    return f"{format(amount, 'f')} {currency or ''}".strip()


async def apply_voice_effect(db, proposal: VoiceProposal) -> tuple[str, dict]:
    """Perform the intent's effect. Returns ``(external_ref, result)``; the
    caller stores ``result`` on the proposal as ``execution_result`` and the
    read-back verifies it is there. Raises on any fail-closed case."""
    kind = proposal.intent_kind
    params = (proposal.action or {}).get("params") or {}
    if kind == "status_overview":
        overview = await compute_status_overview(db, group_id=proposal.group_id, scope_id=proposal.scope_id)
        return f"voice:{proposal.id}:overview", {
            "kind": kind, "overview": overview,
            "note": "Read-only status overview for the department; nothing was changed.",
        }
    if kind == "invoice_approve":
        ref = str(params.get("invoice_ref") or "")
        if not ref:
            raise InvalidInputError("This voice approval names no invoice reference.")
        invoice = await find_invoice_for_voice(db, group_id=proposal.group_id, invoice_ref=ref)
        if invoice is None:
            # The parser stores the bare reference ("99"); name the invoice
            # the way the founder said it ("INV-99") so the failure reason
            # is unambiguous in the audit trail.
            display = ref if ref.lower().startswith("inv-") else f"INV-{ref}"
            raise NotFoundError(f"No invoice {display} in this department.")
        if invoice.status in {"pending", "exception", "rejected"}:
            raise ConflictError(
                f"Invoice {invoice.invoice_number} is {invoice.status} and cannot be approved.")
        if invoice.status in {"approved", "paid"}:
            return f"voice:{proposal.id}:invoice:{invoice.id}", {
                "kind": kind, "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number, "vendor_name": invoice.vendor_name,
                "amount": _money(invoice.amount, invoice.currency),
                "status_before": invoice.status,
                "decision": "Invoice was already approved or paid; the voice approval changes nothing.",
            }
        return f"voice:{proposal.id}:invoice:{invoice.id}", {
            "kind": kind, "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number, "vendor_name": invoice.vendor_name,
            "amount": _money(invoice.amount, invoice.currency),
            "status_before": invoice.status,
            "decision": ("Voice approval recorded for this verified invoice. The invoice status "
                         "was NOT changed: invoice approval completes through the normal "
                         "payment-approval flow, which this voice approval does not bypass."),
        }
    if kind == "meeting_summary":
        raise InvalidInputError(
            "This voice note names no meeting to summarise. Record a new voice note with the "
            "meeting, or pick the meeting in the app; a summary is never guessed.")
    raise InvalidInputError(f"Voice intent {kind!r} has no executable effect.")
