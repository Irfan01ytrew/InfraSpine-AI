"""Cash-flow forecast (Step AC, slice 1): pure functions, stdlib only.

Projects the next N months from two honest inputs only:

- monthly actuals (history) — verified/approved/paid invoice totals, the same
  population `FinanceService.spend()` aggregates;
- pending invoices — unpaid invoices already in the system
  (`pending` / `verified` / `exception` / `approved`; never `paid`/`rejected`).

A forecast is a forecast, never a fact: every month carries its `basis`
(`scheduled` / `recurring` / `mixed` / `none`), a `confidence`
(`high` / `medium` / `low`) and a `kind` of ``forecast``. Nothing here touches a
database, a network, or a payment — by construction this module cannot move
money. Money is `Decimal` throughout; no floats.

Rules enforced here (see docs/step-ac-finance-plan.md):
- scheduled invoices are never double-counted in the recurring average
  (the average comes from past actuals; pending invoices are future
  obligations not present in history);
- an invoice with no usable date is bucketed into month 1 with a warning,
  never silently dropped; an overdue invoice lands in month 1 too;
- rows in another currency are skipped with a warning count;
- empty history yields `low` confidence and a warning, not a zero
  presented as certain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

CENT = Decimal('0.01')
DEFAULT_TERMS_DAYS = 30
DEFAULT_HISTORY_WINDOW = 6
MAX_MONTHS = 12


def _money(value) -> Decimal:
    """Coerce an int/str/Decimal to a 2-decimal Decimal, or raise ValueError."""
    if isinstance(value, bool) or value is None:
        raise ValueError('Amount is required.')
    try:
        amount = Decimal(str(value)) if not isinstance(value, Decimal) else value
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f'Invalid amount: {value!r}.') from exc
    return amount.quantize(CENT)


def _month_key(year: int, month: int) -> str:
    return f'{year:04d}-{month:02d}'


def _parse_month(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or len(value) != 7 or value[4] != '-':
        raise ValueError(f'Invalid month (expected YYYY-MM): {value!r}.')
    try:
        year, month = int(value[:4]), int(value[5:7])
    except ValueError as exc:
        raise ValueError(f'Invalid month (expected YYYY-MM): {value!r}.') from exc
    if not 1 <= month <= 12:
        raise ValueError(f'Invalid month (expected YYYY-MM): {value!r}.')
    return year, month


def _add_months(year: int, month: int, offset: int) -> tuple[int, int]:
    total = (year * 12 + (month - 1)) + offset
    return total // 12, total % 12 + 1


@dataclass(frozen=True)
class HistoryMonth:
    """One month of actuals. ``inflow`` defaults to zero (payables-only history)."""

    month: str  # 'YYYY-MM'
    outflow: Decimal = Decimal('0')
    inflow: Decimal = Decimal('0')

    def __post_init__(self):
        _parse_month(self.month)
        object.__setattr__(self, 'outflow', _money(self.outflow))
        object.__setattr__(self, 'inflow', _money(self.inflow))
        if self.outflow < 0 or self.inflow < 0:
            raise ValueError('History amounts must not be negative.')


@dataclass(frozen=True)
class PendingInvoice:
    """An unpaid invoice already in the system. ``due_date`` wins; otherwise
    ``invoice_date + payment_terms_days``; neither means month 1 + warning."""

    amount: Decimal
    currency: str
    due_date: date | None = None
    invoice_date: date | None = None

    def __post_init__(self):
        object.__setattr__(self, 'amount', _money(self.amount))
        if self.amount <= 0:
            raise ValueError('Pending invoice amount must be positive.')
        if not self.currency or len(self.currency.strip()) != 3:
            raise ValueError('Pending invoice currency must be a 3-letter code.')
        object.__setattr__(self, 'currency', self.currency.strip().upper())


@dataclass(frozen=True)
class ForecastMonth:
    month: str
    scheduled_outflow: Decimal
    recurring_estimate: Decimal
    expected_outflow: Decimal
    expected_inflow: Decimal
    net: Decimal
    closing_balance: Decimal
    basis: str  # 'scheduled' | 'recurring' | 'mixed' | 'none'
    confidence: str  # 'high' | 'medium' | 'low'


@dataclass(frozen=True)
class ForecastResult:
    kind: str  # always 'forecast'
    currency: str
    as_of: date
    months: list[ForecastMonth]
    opening_balance: Decimal
    closing_balance: Decimal
    total_expected_outflow: Decimal
    total_expected_inflow: Decimal
    history_months_used: int
    warnings: list[dict] = field(default_factory=list)


def forecast_cash_flow(
    *,
    history: list[HistoryMonth],
    pending: list[PendingInvoice],
    opening_balance=None,
    currency: str,
    months: int = 3,
    today: date | None = None,
    payment_terms_days: int = DEFAULT_TERMS_DAYS,
    history_window: int = DEFAULT_HISTORY_WINDOW,
) -> ForecastResult:
    """Project ``months`` future months starting with the current month.

    ``history`` may be in any order and may contain months after ``today``
    (ignored with a warning). ``pending`` invoices are bucketed by due month;
    ones due beyond the horizon are reported, not silently dropped.
    """
    if not currency or len(currency.strip()) != 3:
        raise ValueError('Currency must be a 3-letter code.')
    currency = currency.strip().upper()
    if not 1 <= months <= MAX_MONTHS:
        raise ValueError(f'Months must be between 1 and {MAX_MONTHS}.')
    if payment_terms_days < 0:
        raise ValueError('Payment terms must not be negative.')
    if history_window < 1:
        raise ValueError('History window must be at least 1.')
    today = today or date.today()

    warnings: list[dict] = []
    if opening_balance is None:
        opening = Decimal('0')
        warnings.append({'code': 'opening_balance_unknown',
                         'detail': 'Opening balance was not supplied; starting from 0.'})
    else:
        opening = _money(opening_balance)
        if opening < 0:
            raise ValueError('Opening balance must not be negative.')

    current_key = _month_key(today.year, today.month)

    # --- history: last `history_window` months up to and including this month.
    usable = sorted({h.month: h for h in history if h.month <= current_key}.values(),
                    key=lambda h: h.month)
    future_history = [h.month for h in history if h.month > current_key]
    if future_history:
        warnings.append({'code': 'future_history_month',
                         'detail': f'Ignored {len(future_history)} history month(s) after '
                                   f'{current_key}: {", ".join(sorted(future_history))}.'})
    window = usable[-history_window:]
    history_months_used = len(window)
    if history_months_used == 0:
        warnings.append({'code': 'no_history',
                         'detail': 'No history months available; recurring estimate is 0 '
                                   'and confidence is low.'})
        avg_outflow = avg_inflow = Decimal('0')
    else:
        avg_outflow = (sum((h.outflow for h in window), Decimal('0'))
                       / Decimal(history_months_used)).quantize(CENT)
        avg_inflow = (sum((h.inflow for h in window), Decimal('0'))
                      / Decimal(history_months_used)).quantize(CENT)

    if history_months_used >= 6:
        confidence = 'high'
    elif history_months_used >= 3:
        confidence = 'medium'
    else:
        confidence = 'low'

    # --- pending invoices: bucket by due month offset from the current month.
    scheduled: dict[int, Decimal] = {}
    skipped_currency = 0
    beyond_total = Decimal('0')
    for inv in pending:
        if inv.currency != currency:
            skipped_currency += 1
            continue
        due = inv.due_date
        if due is None and inv.invoice_date is not None:
            due = inv.invoice_date + timedelta(days=payment_terms_days)
        if due is None:
            warnings.append({'code': 'missing_due_date',
                             'detail': f'Invoice of {inv.amount} {currency} has no date; '
                                       'counted in month 1.'})
            offset = 0
        else:
            offset = (due.year - today.year) * 12 + (due.month - today.month)
            if offset < 0:
                warnings.append({'code': 'overdue_invoice',
                                 'detail': f'Invoice of {inv.amount} {currency} due '
                                           f'{due.isoformat()} is overdue; counted in month 1.'})
                offset = 0
        if offset >= months:
            beyond_total += inv.amount
            continue
        scheduled[offset] = scheduled.get(offset, Decimal('0')) + inv.amount
    if skipped_currency:
        warnings.append({'code': 'skipped_other_currency',
                         'detail': f'Skipped {skipped_currency} pending invoice(s) in another '
                                   f'currency.'})
    if beyond_total:
        warnings.append({'code': 'beyond_horizon',
                         'detail': f'{beyond_total} {currency} of pending invoices falls due '
                                   f'after the {months}-month horizon and is not in the '
                                   f'projection.'})

    # --- build the projection.
    result_months: list[ForecastMonth] = []
    balance = opening
    total_out = total_in = Decimal('0')
    for offset in range(months):
        year, month = _add_months(today.year, today.month, offset)
        sched = scheduled.get(offset, Decimal('0'))
        recurring = avg_outflow if history_months_used else Decimal('0')
        expected_out = (sched + recurring).quantize(CENT)
        expected_in = avg_inflow
        net = (expected_in - expected_out).quantize(CENT)
        balance = (balance + net).quantize(CENT)
        if sched and recurring:
            basis = 'mixed'
        elif sched:
            basis = 'scheduled'
        elif recurring:
            basis = 'recurring'
        else:
            basis = 'none'
        result_months.append(ForecastMonth(
            month=_month_key(year, month), scheduled_outflow=sched,
            recurring_estimate=recurring, expected_outflow=expected_out,
            expected_inflow=expected_in, net=net, closing_balance=balance,
            basis=basis, confidence=confidence))
        total_out += expected_out
        total_in += expected_in

    return ForecastResult(
        kind='forecast', currency=currency, as_of=today, months=result_months,
        opening_balance=opening, closing_balance=balance,
        total_expected_outflow=total_out.quantize(CENT),
        total_expected_inflow=total_in.quantize(CENT),
        history_months_used=history_months_used, warnings=warnings)
