"""Deterministic invoice verification: vendor master, purchase order, goods receipt (3-way match), amount
tolerance, currency, duplicates, payee bank details and dates. Pure functions, unit tested; the caller loads the
reference data from the department's own records only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

REASONS = {
    'missing_fields': 'Required invoice fields are missing',
    'invalid_amount': 'Invoice amount must be positive',
    'unknown_vendor': 'Vendor is not in the vendor master',
    'vendor_blocked': 'Vendor is blocked in the vendor master',
    'currency_mismatch': 'Invoice currency differs from the purchase order',
    'po_missing': 'No purchase order number on the invoice',
    'po_not_found': 'Purchase order not found',
    'po_closed': 'Purchase order is closed or cancelled',
    'po_vendor_mismatch': 'Purchase order belongs to a different vendor',
    'receipt_missing': 'No goods receipt recorded for the purchase order',
    'amount_exceeds_po': 'Amount exceeds the purchase order beyond tolerance',
    'amount_exceeds_receipt': 'Amount exceeds the goods received beyond tolerance',
    'po_overbilled': 'Invoices against this purchase order exceed it beyond tolerance',
    'duplicate_invoice': 'Same vendor and invoice number already exists',
    'bank_details_mismatch': 'Bank account on the invoice differs from the vendor master',
    'future_date': 'Invoice date is in the future',
    'stale_date': 'Invoice is older than the allowed age',
}


def name_key(value: str | None) -> str:
    return ' '.join(re.findall(r'[a-z0-9]+', (value or '').casefold()))[:200]


def invoice_key(value: str | None) -> str:
    return ''.join(re.findall(r'[A-Z0-9]', (value or '').upper()))[:64]


@dataclass(frozen=True)
class Vendor:
    id: object
    name: str
    status: str
    bank_account: str
    currency: str


@dataclass(frozen=True)
class PurchaseOrder:
    id: object
    number: str
    vendor_id: object
    amount: Decimal
    currency: str
    status: str
    received: Decimal          # sum of goods receipts
    already_billed: Decimal    # verified/approved/paid invoices against this PO from earlier checks


@dataclass(frozen=True)
class InvoiceFacts:
    id: object
    invoice_number: str | None
    vendor_name: str | None
    po_number: str | None
    amount: Decimal | None
    currency: str | None
    invoice_date: date | None
    bank_account_on_invoice: str | None


@dataclass
class Reference:
    vendors: dict[str, Vendor]                    # name_key -> vendor
    purchase_orders: dict[str, PurchaseOrder]     # PO number (upper) -> PO
    existing_invoices: set[tuple[object, str]]    # (vendor_id, invoice_key) already recorded elsewhere
    today: date
    tolerance_pct: Decimal = Decimal('2')
    tolerance_abs: Decimal = Decimal('0')
    max_age_days: int = 365
    billed_in_run: dict[str, Decimal] = field(default_factory=dict)
    seen_in_run: set[tuple[object, str]] = field(default_factory=set)


@dataclass(frozen=True)
class Result:
    verified: bool
    vendor_id: object | None
    reasons: list[dict]


def _allowance(base: Decimal, ref: Reference) -> Decimal:
    return max(ref.tolerance_abs, (base * ref.tolerance_pct / Decimal(100)).quantize(Decimal('0.01')))


def verify_invoice(inv: InvoiceFacts, ref: Reference) -> Result:
    reasons: list[str] = []
    missing = [name for name, value in (('invoice_number', inv.invoice_number), ('vendor', inv.vendor_name),
                                        ('amount', inv.amount), ('currency', inv.currency),
                                        ('invoice_date', inv.invoice_date)) if value in (None, '')]
    detail: dict[str, dict] = {}
    if missing:
        reasons.append('missing_fields')
        detail['missing_fields'] = {'fields': missing}
    if inv.amount is not None and inv.amount <= 0:
        reasons.append('invalid_amount')
    vendor = ref.vendors.get(name_key(inv.vendor_name)) if inv.vendor_name else None
    if inv.vendor_name and vendor is None:
        reasons.append('unknown_vendor')
    elif vendor is not None and vendor.status != 'active':
        reasons.append('vendor_blocked')
    if vendor is not None and inv.bank_account_on_invoice and inv.bank_account_on_invoice != vendor.bank_account:
        reasons.append('bank_details_mismatch')
    if inv.invoice_date is not None:
        if inv.invoice_date > ref.today:
            reasons.append('future_date')
        elif inv.invoice_date < ref.today - timedelta(days=ref.max_age_days):
            reasons.append('stale_date')
    if vendor is not None and inv.invoice_number:
        key = (vendor.id, invoice_key(inv.invoice_number))
        if key in ref.existing_invoices or key in ref.seen_in_run:
            reasons.append('duplicate_invoice')
    po = None
    if not inv.po_number:
        reasons.append('po_missing')
    else:
        po = ref.purchase_orders.get(inv.po_number.strip().upper())
        if po is None:
            reasons.append('po_not_found')
        else:
            if po.status != 'open':
                reasons.append('po_closed')
            if vendor is not None and po.vendor_id != vendor.id:
                reasons.append('po_vendor_mismatch')
            if inv.currency and po.currency != inv.currency:
                reasons.append('currency_mismatch')
            if inv.amount is not None and inv.amount > 0:
                if inv.amount > po.amount + _allowance(po.amount, ref):
                    reasons.append('amount_exceeds_po')
                if po.received <= 0:
                    reasons.append('receipt_missing')
                elif inv.amount > po.received + _allowance(po.received, ref):
                    reasons.append('amount_exceeds_receipt')
                billed = po.already_billed + ref.billed_in_run.get(po.number, Decimal(0)) + inv.amount
                if 'amount_exceeds_po' not in reasons and billed > po.amount + _allowance(po.amount, ref):
                    reasons.append('po_overbilled')
    verified = not reasons
    if verified:
        # Later invoices in the same run see this one (duplicates, cumulative PO billing).
        ref.seen_in_run.add((vendor.id, invoice_key(inv.invoice_number)))
        ref.billed_in_run[po.number] = ref.billed_in_run.get(po.number, Decimal(0)) + inv.amount
    elif vendor is not None and inv.invoice_number:
        ref.seen_in_run.add((vendor.id, invoice_key(inv.invoice_number)))
    unique = list(dict.fromkeys(reasons))
    return Result(verified, vendor.id if vendor else None,
                  [{'code': code, 'message': REASONS[code], **detail.get(code, {})} for code in unique])
