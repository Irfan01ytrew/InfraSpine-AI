"""Finance anomaly detection (Step Y, slice 1): advisory flags over invoice records.

Pure functions, standard library only. The detector watches money flows and
flags unusual invoices, duplicate payments and odd amounts *before* they are
paid. Detection is advisory: every flag has severity ``warning`` and blocks
straight-through approval (the invoice goes to the exceptions queue for explicit
owner review), but a flag never auto-rejects a legitimate invoice, never changes
a payee or an amount, and never grants or changes an approval.

The caller loads history from the department's own records only; invoice and
document text is untrusted data and is never treated as an instruction.
Deterministic: the same inputs always produce the same flags in the same order.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from app.finance.verification import invoice_key, name_key

FLAGS = {
    'exact_duplicate': 'Same vendor and invoice number already exists',
    'near_duplicate': 'Same vendor, amount and date as another invoice with a different number',
    'amount_outlier': 'Amount is far above this vendor\'s usual amounts',
    'first_time_payee': 'Vendor has no previous invoice in this department',
    'rapid_repeat_payments': 'Many invoices from this vendor in a short window',
    'bank_account_changed': 'Bank account differs from this vendor\'s previously used account',
}

# Flags that hold a payment for owner verification of the payee (slice 3).
# A flagged invoice never reaches a payment: slice 2 routes it to the exceptions
# queue at verification, and the payment grouping below refuses it as well.
PAYMENT_HOLD_CODES = frozenset({'bank_account_changed', 'first_time_payee'})


@dataclass(frozen=True)
class InvoiceRecord:
    """One invoice as seen by the detector (a view, not a database row)."""

    id: object
    vendor_name: str | None
    invoice_number: str | None
    amount: Decimal | None
    currency: str | None
    invoice_date: date | None
    bank_account: str | None = None


@dataclass(frozen=True)
class AnomalyConfig:
    """Tunable thresholds; departments may override these later (slice 5)."""

    min_history: int = 5                 # prior invoices needed for outlier detection
    outlier_mad_multiplier: Decimal = Decimal('5')   # flag above median + k * MAD
    outlier_ratio_when_flat: Decimal = Decimal('3')  # flag above 3x median when MAD is 0
    near_duplicate_days: int = 7
    velocity_window_days: int = 7
    velocity_max_prior: int = 3          # flag at this many priors inside the window


@dataclass(frozen=True)
class AnomalyFlag:
    code: str
    message: str
    severity: str = 'warning'
    details: dict = field(default_factory=dict)


def _account_key(value: str | None) -> str:
    """Bank accounts compare ignoring spaces, dashes and case ('PK00 BETA 0002' == 'pk00beta0002')."""
    return ''.join(re.findall(r'[a-z0-9]', (value or '').casefold()))


# All anomaly flag codes (slice 4: the admin UI and the resolve flow use this set to
# tell advisory flags apart from deterministic verification reasons).
ANOMALY_CODES = frozenset(FLAGS)


def mask_account(value: str) -> str:
    """Last 4 characters only: full account numbers must never land in reasons, logs or API responses."""
    tail = value[-4:] if len(value) > 4 else '****'
    return '********' + tail if len(value) > 4 else '****'


def _mask_account(value: str) -> str:
    """Backwards-compatible alias for mask_account (slice 1 name)."""
    return mask_account(value)


def detect_bank_account_change(record: InvoiceRecord, history: list[InvoiceRecord]) -> AnomalyFlag | None:
    """Slice 3: the invoice's bank account differs from the account this vendor used before.

    This compares against the vendor's *history*, not the vendor master: deterministic
    check 15 already compares against the master, so it passes when the master itself
    was changed (for example a compromised master record). The most recent prior
    account is the baseline; an older account that was already superseded does not
    count. Returns None when the record has no account, or the vendor has no prior
    account on file — a first payment has nothing to compare against.
    """
    if not record.bank_account or not record.vendor_name:
        return None
    priors = [h for h in _vendor_history(record, history) if h.bank_account]
    if not priors:
        return None
    latest = max(priors, key=lambda h: (h.invoice_date or date.min, str(h.id)))
    if _account_key(record.bank_account) == _account_key(latest.bank_account):
        return None
    return AnomalyFlag(
        'bank_account_changed', FLAGS['bank_account_changed'],
        details={'previous_account': _mask_account(latest.bank_account),
                 'new_account': _mask_account(record.bank_account),
                 'prior_id': str(latest.id),
                 'prior_date': latest.invoice_date.isoformat() if latest.invoice_date else None})


def requires_payee_verification(flags: list[AnomalyFlag]) -> bool:
    """True when a flag holds the payment until the owner verifies the payee: a changed
    bank account, or a vendor the department has never paid before. Advisory only —
    the hold routes to owner review; it never approves, rejects or changes anything."""
    return any(f.code in PAYMENT_HOLD_CODES for f in flags)


def _same_vendor(a: InvoiceRecord, b: InvoiceRecord) -> bool:
    return bool(a.vendor_name and b.vendor_name and name_key(a.vendor_name) == name_key(b.vendor_name))


def _vendor_history(record: InvoiceRecord, history: list[InvoiceRecord]) -> list[InvoiceRecord]:
    return [h for h in history if h.id != record.id and _same_vendor(record, h)]


def _median_amounts(amounts: list[Decimal]) -> Decimal:
    return Decimal(str(statistics.median([float(a) for a in amounts])))


def _mad(amounts: list[Decimal], median: Decimal) -> Decimal:
    deviations = [abs(a - median) for a in amounts]
    return Decimal(str(statistics.median([float(d) for d in deviations])))


def detect_anomalies(record: InvoiceRecord, history: list[InvoiceRecord],
                     config: AnomalyConfig | None = None) -> list[AnomalyFlag]:
    """Return the advisory anomaly flags for ``record`` against ``history``.

    ``history`` is the department's own prior invoices (any status). The record
    itself is excluded by id. Rules that need data the record lacks (amount,
    date, invoice number) are skipped, never guessed.
    """
    cfg = config or AnomalyConfig()
    flags: list[AnomalyFlag] = []
    vendor_history = _vendor_history(record, history)

    # First-time payee: vendor never invoiced this department before.
    if record.vendor_name and not vendor_history:
        flags.append(AnomalyFlag('first_time_payee', FLAGS['first_time_payee'],
                                 details={'vendor': name_key(record.vendor_name)}))

    # Exact duplicate: same vendor + same invoice number (mirrors deterministic
    # check 14 so the advisory layer is complete on its own).
    if record.vendor_name and record.invoice_number:
        key = (name_key(record.vendor_name), invoice_key(record.invoice_number))
        for prior in vendor_history:
            if prior.invoice_number and (name_key(prior.vendor_name), invoice_key(prior.invoice_number)) == key:
                flags.append(AnomalyFlag(
                    'exact_duplicate', FLAGS['exact_duplicate'],
                    details={'invoice_number': record.invoice_number, 'prior_id': str(prior.id)}))
                break

    # Bank-account change (slice 3): the account on this invoice differs from the
    # account this vendor used most recently. A changed payee account holds the
    # payment for owner verification; see requires_payee_verification().
    changed = detect_bank_account_change(record, history)
    if changed is not None:
        flags.append(changed)

    # Near-duplicate: same vendor + same amount + dates within the window, but a
    # different invoice number (re-typed, suffixed, or re-issued).
    if (record.vendor_name and record.amount is not None and record.amount > 0
            and record.invoice_date is not None):
        for prior in vendor_history:
            if (prior.amount is not None and prior.amount == record.amount
                    and prior.invoice_date is not None
                    and abs((prior.invoice_date - record.invoice_date).days) <= cfg.near_duplicate_days
                    and invoice_key(prior.invoice_number) != invoice_key(record.invoice_number)):
                flags.append(AnomalyFlag(
                    'near_duplicate', FLAGS['near_duplicate'],
                    details={'amount': str(record.amount), 'prior_id': str(prior.id),
                             'prior_invoice_number': prior.invoice_number,
                             'prior_date': prior.invoice_date.isoformat()}))
                break

    # Amount outlier: robust median/MAD test against the vendor's same-currency
    # history. Needs enough history to mean anything.
    if record.amount is not None and record.amount > 0:
        past = [h.amount for h in vendor_history
                if h.amount is not None and h.amount > 0
                and (record.currency is None or h.currency is None or h.currency == record.currency)]
        if len(past) >= cfg.min_history:
            median = _median_amounts(past)
            mad = _mad(past, median)
            if mad > 0:
                threshold = median + cfg.outlier_mad_multiplier * mad
                method = 'median+mad'
            else:
                threshold = median * cfg.outlier_ratio_when_flat
                method = 'flat-median-ratio'
            if record.amount > threshold:
                flags.append(AnomalyFlag(
                    'amount_outlier', FLAGS['amount_outlier'],
                    details={'amount': str(record.amount), 'median': str(median),
                             'mad': str(mad), 'threshold': str(threshold),
                             'method': method, 'history_count': len(past)}))

    # Velocity: many prior invoices from the same vendor inside the window
    # ending on this invoice's date.
    if record.vendor_name and record.invoice_date is not None:
        start = record.invoice_date - timedelta(days=cfg.velocity_window_days)
        inside = [h for h in vendor_history
                  if h.invoice_date is not None and start <= h.invoice_date <= record.invoice_date]
        if len(inside) >= cfg.velocity_max_prior:
            flags.append(AnomalyFlag(
                'rapid_repeat_payments', FLAGS['rapid_repeat_payments'],
                details={'prior_count': len(inside),
                         'window_days': cfg.velocity_window_days,
                         'window_total': str(sum((h.amount or Decimal(0)) for h in inside))}))

    return flags


def blocks_straight_through(flags: list[AnomalyFlag]) -> bool:
    """True when any flag exists: the invoice must go to owner review."""
    return bool(flags)


# ---------------------------------------------------------------------------
# Slice 5: per-department threshold settings.
#
# Stored as a JSON object keyed by AnomalyConfig field name. A department without
# stored settings uses the slice-1 defaults, and a threshold change is never
# retroactive: it only affects verifications that run after the change.
# ---------------------------------------------------------------------------

# (field, kind, low, high) — the strict bounds the settings API enforces.
_SETTING_BOUNDS = {
    'min_history': ('int', 1, 100),
    'outlier_mad_multiplier': ('decimal', Decimal('0.1'), Decimal('100')),
    'outlier_ratio_when_flat': ('decimal', Decimal('0.1'), Decimal('100')),
    'near_duplicate_days': ('int', 0, 365),
    'velocity_window_days': ('int', 1, 365),
    'velocity_max_prior': ('int', 1, 100),
}

SETTING_FIELDS = tuple(_SETTING_BOUNDS)


def _coerce_int(value, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and value.strip().lstrip('-').isdigit():
        number = int(value.strip())
    else:
        return default
    return number if lo <= number <= hi else default


def _coerce_decimal(value, default: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    if isinstance(value, bool):
        return default
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception:
        return default
    if not number.is_finite():
        return default
    return number if lo <= number <= hi else default


def config_out(config: AnomalyConfig) -> dict:
    """An AnomalyConfig as a JSON-safe dict (Decimals become strings)."""
    return {'min_history': config.min_history,
            'outlier_mad_multiplier': str(config.outlier_mad_multiplier),
            'outlier_ratio_when_flat': str(config.outlier_ratio_when_flat),
            'near_duplicate_days': config.near_duplicate_days,
            'velocity_window_days': config.velocity_window_days,
            'velocity_max_prior': config.velocity_max_prior}


def config_from_settings(value) -> AnomalyConfig:
    """Build a config from stored per-department settings (a JSON object from the
    database). Lenient by design: unknown keys are ignored and any invalid value
    falls back to the default for that field — a bad stored value must never
    break verification."""
    defaults = AnomalyConfig()
    if not isinstance(value, dict):
        return defaults
    coerced = {}
    for field, (kind, lo, hi) in _SETTING_BOUNDS.items():
        current = getattr(defaults, field)
        if kind == 'int':
            coerced[field] = _coerce_int(value.get(field), current, lo, hi)
        else:
            coerced[field] = _coerce_decimal(value.get(field), current, lo, hi)
    return AnomalyConfig(**coerced)


def validate_config_patch(patch) -> dict:
    """Strict validation for the settings API. Returns the cleaned overrides as a
    JSON-safe dict (Decimals become strings); raises ValueError naming the first
    bad field. Unknown keys raise too — a typo must not silently do nothing.
    An empty patch is valid and changes nothing."""
    if not isinstance(patch, dict):
        raise ValueError('thresholds must be an object')
    unknown = sorted(set(patch) - set(_SETTING_BOUNDS))
    if unknown:
        raise ValueError(f"unknown threshold(s): {', '.join(unknown)}")
    defaults = AnomalyConfig()
    cleaned = {}
    for field, (kind, lo, hi) in _SETTING_BOUNDS.items():
        if field not in patch:
            continue
        current = getattr(defaults, field)
        if kind == 'int':
            value = _coerce_int(patch[field], None, lo, hi)  # type: ignore[arg-type]
        else:
            value = _coerce_decimal(patch[field], None, lo, hi)  # type: ignore[arg-type]
        if value is None:
            raise ValueError(f'{field} must be between {lo} and {hi}')
        cleaned[field] = str(value) if kind == 'decimal' else value
    return cleaned
