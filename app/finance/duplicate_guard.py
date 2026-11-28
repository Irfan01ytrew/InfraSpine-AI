"""Duplicate-payment guard report (Step R slice 5): nightly reconciliation of
connector transfers against ``FinPayment`` rows.

Read-only by construction: it reads payment rows and calls only
``connector.find(idempotency_key)`` — the same read-back the execute handler
uses after an ambiguous submit. It creates no payment, requests or decides no
approval, submits nothing, and changes no payment, approval or run. Each run
writes one audit event (``finance.duplicate_guard``) with the counts and the
mismatch kinds, so a finance manager can see the result and act on it.

What it catches:
* ``missing_transfer`` — a dispatched/verified payment the connector has no
  transfer for (paid in our books, nothing at the bank).
* ``amount_mismatch`` / ``payee_mismatch`` / ``currency_mismatch`` — the
  transfer exists but its amount, payee account or currency differs from the
  approved payment row.
* ``failed_transfer`` — the connector holds the transfer as failed while our
  row still says dispatched/verified.
* ``duplicate_payment`` — two dispatched/verified payment rows covering the
  same invoice(s): the same invoice would be paid twice.
* ``read_failed`` — the connector could not be read for a payment (reported
  separately, not as a mismatch, so the run can be retried).

Payments that have not been submitted yet (``awaiting_approval``,
``approved``) and payments that failed before submit (``failed``,
``rejected``, ``expired``) are not checked: no transfer is expected for them.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.db.finance_models import FinPayment
from app.finance.connectors import resolve_connector
from app.finance.service import FinanceService
from app.platform.events import audit

logger = logging.getLogger(__name__)

# Payments that should exist at the connector. Anything else has no transfer yet.
CHECKED_STATUSES = ('dispatched', 'verified')


def payment_idempotency_key(payment_id: uuid.UUID) -> str:
    """The key the execute handler submits under (``payment:<payment_id>``)."""
    return f'payment:{payment_id}'


@dataclass(frozen=True)
class GuardMismatch:
    kind: str  # missing_transfer | amount_mismatch | payee_mismatch | currency_mismatch
               # | failed_transfer | duplicate_payment
    payment_id: uuid.UUID
    batch_id: uuid.UUID
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {'kind': self.kind, 'payment_id': str(self.payment_id),
                'batch_id': str(self.batch_id), 'detail': self.detail}


@dataclass(frozen=True)
class GuardUnread:
    payment_id: uuid.UUID
    batch_id: uuid.UUID
    reason: str

    def to_dict(self) -> dict:
        return {'payment_id': str(self.payment_id), 'batch_id': str(self.batch_id),
                'reason': self.reason}


@dataclass
class DuplicateGuardReport:
    checked: int = 0
    matched: int = 0
    mismatches: list[GuardMismatch] = field(default_factory=list)
    unread: list[GuardUnread] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def has_problems(self) -> bool:
        return bool(self.mismatches or self.unread)

    def to_dict(self) -> dict:
        return {'checked': self.checked, 'matched': self.matched,
                'mismatches': [m.to_dict() for m in self.mismatches],
                'unread': [u.to_dict() for u in self.unread],
                'has_problems': self.has_problems,
                'generated_at': self.generated_at.isoformat()}


def _duplicate_pairs(payments: list[FinPayment]) -> list[GuardMismatch]:
    """Pairs of checked payments whose invoice sets overlap: the same invoice
    would be paid twice. Pure function over the loaded rows."""
    by_invoice: dict[str, list[FinPayment]] = {}
    for pay in payments:
        for invoice_id in (pay.invoice_ids or []):
            by_invoice.setdefault(str(invoice_id), []).append(pay)
    seen: set[frozenset] = set()
    out: list[GuardMismatch] = []
    for invoice_id, rows in sorted(by_invoice.items()):
        for i, first in enumerate(rows):
            for second in rows[i + 1:]:
                key = frozenset({first.id, second.id})
                if key in seen:
                    continue
                seen.add(key)
                shared = sorted({str(x) for x in (first.invoice_ids or [])}
                                & {str(x) for x in (second.invoice_ids or [])})
                later = first if first.created_at >= second.created_at else second
                out.append(GuardMismatch(
                    'duplicate_payment', later.id, later.batch_id,
                    {'payment_ids': sorted(str(p.id) for p in (first, second)),
                     'shared_invoice_ids': shared}))
    return out


async def run_duplicate_guard(db, user, settings, *, cipher=None, connector=None) -> DuplicateGuardReport:
    """Reconcile dispatched/verified payments against the connector (read-back).

    Only payments in departments the caller may read (``finance.read``) are
    checked. The report is returned and one audit event is written; nothing
    else is created or changed. ``connector`` is injectable for tests; the
    administrator's configured connector is used otherwise.
    """
    service = FinanceService(db, user, settings, cipher)
    groups = await service.readable_groups('finance.read')
    report = DuplicateGuardReport()
    if not groups:
        return report
    payments = list((await db.scalars(select(FinPayment).where(
        FinPayment.group_id.in_(groups), FinPayment.status.in_(CHECKED_STATUSES))
        .order_by(FinPayment.created_at, FinPayment.id))).all())
    report.checked = len(payments)
    if connector is None:
        connector = await resolve_connector(db, settings)
    for pay in payments:
        try:
            found = await connector.find(payment_idempotency_key(pay.id))
        except Exception as exc:  # one unreadable payment must not sink the report
            report.unread.append(GuardUnread(pay.id, pay.batch_id, type(exc).__name__))
            continue
        if found is None:
            report.mismatches.append(GuardMismatch(
                'missing_transfer', pay.id, pay.batch_id,
                {'expected_amount': str(pay.amount), 'currency': pay.currency,
                 'external_ref': pay.external_ref}))
            continue
        if found.status == 'failed':
            report.mismatches.append(GuardMismatch(
                'failed_transfer', pay.id, pay.batch_id,
                {'external_ref': pay.external_ref, 'transfer_ref': found.ref}))
            continue
        mismatch = None
        if Decimal(found.amount) != Decimal(pay.amount):
            mismatch = ('amount_mismatch', {'expected_amount': str(pay.amount),
                                            'found_amount': str(found.amount),
                                            'currency': pay.currency})
        elif found.currency != pay.currency:
            mismatch = ('currency_mismatch', {'expected_currency': pay.currency,
                                              'found_currency': found.currency})
        elif found.payee_account != pay.payee_account:
            mismatch = ('payee_mismatch', {'external_ref': pay.external_ref,
                                           'transfer_ref': found.ref})
        if mismatch is not None:
            report.mismatches.append(GuardMismatch(mismatch[0], pay.id, pay.batch_id, mismatch[1]))
        else:
            report.matched += 1
    report.mismatches.extend(_duplicate_pairs(payments))
    # Payee account numbers stay out of the audit event; the API report carries the details.
    audit(db, service.p, 'finance.duplicate_guard', target='duplicate-guard', details={
        'checked': report.checked, 'matched': report.matched,
        'mismatches': [{'kind': m.kind, 'payment_id': str(m.payment_id)} for m in report.mismatches],
        'unread': [str(u.payment_id) for u in report.unread]})
    await db.commit()
    if report.has_problems:
        logger.warning('Duplicate-payment guard: %s mismatch(es), %s unread of %s payments checked',
                       len(report.mismatches), len(report.unread), report.checked)
    return report


__all__ = ['CHECKED_STATUSES', 'DuplicateGuardReport', 'GuardMismatch', 'GuardUnread',
           'payment_idempotency_key', 'run_duplicate_guard']
