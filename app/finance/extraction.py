"""Invoice field extraction for the invoice flow. Reuses the deterministic finance-form extractor
(`app.rag.financial_forms`, including the merged "Label: value" paragraph fix) and the document parsers.

Everything extracted here is *data*. Invoice text cannot choose a payee account (the vendor master does), cannot
grant a permission and cannot change an approval target; an instruction written inside an invoice is just text.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.core.errors import InvalidInputError
from app.rag.financial_forms import _label_values, _norm, _pick, detect_form_profile, extract_financial_form

MAX_TEXT = 200_000
_DEPARTMENT_LABELS = ('department', 'cost center', 'cost centre', 'business unit')
_BANK_LABELS = ('bank account', 'account number', 'account no', 'iban', 'bank account number', 'pay to account')


def digest_of(value: Any) -> str:
    data = value if isinstance(value, (bytes, bytearray)) else json.dumps(value, sort_keys=True, default=str).encode()
    return hashlib.sha256(data).hexdigest()


def _first(labels, names):
    for name in names:
        item = labels.get(_norm(name))
        if item and item[0]:
            return item[0][:200]
    return None


def normalise_account(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = ''.join(ch for ch in value.upper() if ch.isalnum())
    return cleaned[:64] or None


def facts_from_text(text: str, *, date_order: str = 'dmy', filename: str = 'invoice.txt') -> dict:
    if not isinstance(text, str) or not text.strip():
        raise InvalidInputError('Invoice text is empty.')
    text = text[:MAX_TEXT]
    sections = [(text, {'kind': 'text', 'position': 1})]
    extraction = extract_financial_form('invoice', sections, [], date_order=date_order)
    labels = _label_values(sections, [])
    fields = extraction.fields
    po = _pick(labels, 'purchase_order_number')
    total = fields.get('total')
    currency = fields.get('currency')
    issued = fields.get('document_date')
    return {
        'invoice_number': (fields['invoice_number'].value or None) if fields.get('invoice_number') else None,
        'vendor_name': (fields['vendor'].value or None) if fields.get('vendor') else None,
        'po_number': po[0][:64] if po else None,
        'amount': total.value if total and total.valid else None,
        'currency': currency.value if currency and currency.valid else None,
        'invoice_date': issued.value if issued and issued.valid else None,
        'department': _first(labels, _DEPARTMENT_LABELS),
        'bank_account_on_invoice': normalise_account(_first(labels, _BANK_LABELS)),
        'extraction': {'profile': detect_form_profile(filename, text) or 'invoice', **extraction.to_dict()},
    }


def facts_from_file(filename: str, content: bytes, *, settings, date_order: str = 'dmy') -> dict:
    """Parse an uploaded invoice (PDF, DOCX, TXT, …) with the same parsers as knowledge ingestion."""
    from app.rag.parsers.registry import format_for_filename, limits_from_settings, parse_document
    if not content:
        raise InvalidInputError('The invoice file is empty.')
    if len(content) > 10 * 1024 * 1024:
        raise InvalidInputError('Invoice files are limited to 10 MB.')
    file_format = format_for_filename(filename)
    with tempfile.TemporaryDirectory(prefix='infraspine-invoice-') as folder:
        path = Path(folder) / ('upload' + Path(filename).suffix.lower())
        path.write_bytes(content)
        sections = parse_document(path, file_format, limits_from_settings(settings), use_docling=False)
    text = '\n'.join(section.text for section in sections)
    facts = facts_from_text(text, date_order=date_order, filename=filename)
    return facts


def facts_from_json(item: dict) -> dict:
    """Structured invoice rows (ERP export, API). Values are validated, never trusted."""
    if not isinstance(item, dict):
        raise InvalidInputError('Each invoice must be an object.')
    allowed = {'invoice_number', 'vendor_name', 'po_number', 'amount', 'currency', 'invoice_date', 'department',
               'bank_account_on_invoice'}
    unknown = set(item) - allowed
    if unknown:
        raise InvalidInputError(f'Unknown invoice fields: {", ".join(sorted(unknown))[:200]}.')
    out = {key: item.get(key) for key in allowed}
    for key in ('invoice_number', 'po_number', 'currency'):
        if out[key] is not None:
            out[key] = str(out[key]).strip()[:64] or None
    for key in ('vendor_name', 'department'):
        if out[key] is not None:
            out[key] = str(out[key]).strip()[:200] or None
    out['bank_account_on_invoice'] = normalise_account(out['bank_account_on_invoice'])
    out['extraction'] = None
    return out


def coerce(facts: dict) -> dict:
    """Typed values for storage. Invalid values become None (the verifier then reports a missing field)."""
    amount = facts.get('amount')
    try:
        amount = Decimal(str(amount)).quantize(Decimal('0.01')) if amount not in (None, '') else None
        if amount is not None and not amount.is_finite():
            amount = None
    except (InvalidOperation, ValueError):
        amount = None
    issued = facts.get('invoice_date')
    if isinstance(issued, str):
        try:
            issued = date.fromisoformat(issued)
        except ValueError:
            issued = None
    elif not isinstance(issued, date):
        issued = None
    currency = (facts.get('currency') or '').upper()[:3] or None
    return {**facts, 'amount': amount, 'invoice_date': issued, 'currency': currency}
