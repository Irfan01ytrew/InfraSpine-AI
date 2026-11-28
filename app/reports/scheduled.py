"""Scheduled reports + alerts (Step AA, slice 1): pure functions only.

The schedule model and the daily cash summary builder live here with no I/O:
no database access, no network, no sending. The worker (slice 3) will fetch
rows the recipient is allowed to read (``finance.read``, group-scoped) and hand
them to :func:`build_daily_cash_summary`; the delivery channels (slice 4) will
send :func:`render_daily_cash_summary_text` output.

Reports are read-only summaries. Nothing in this module executes a payment,
changes an invoice, or approves anything, and report text must be treated as
untrusted data by any downstream consumer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

# Statuses straight from app/db/finance_models.py check constraints.
INVOICE_STATUSES = ("pending", "verified", "exception", "approved", "paid", "rejected")
PAYMENT_STATUSES = (
    "awaiting_approval",
    "approved",
    "rejected",
    "expired",
    "dispatched",
    "verified",
    "failed",
)

ReportTemplate = Literal["daily_cash_summary", "weekly_invoices", "overdue_alert"]
ReportCadence = Literal["daily", "weekly"]
ReportChannel = Literal["whatsapp", "email", "in_app"]

TEMPLATES: tuple[ReportTemplate, ...] = ("daily_cash_summary", "weekly_invoices", "overdue_alert")
CADENCES: tuple[ReportCadence, ...] = ("daily", "weekly")
CHANNELS: tuple[ReportChannel, ...] = ("whatsapp", "email", "in_app")

DEFAULT_OVERDUE_DAYS = 7
DEFAULT_QUIET_START = time(22, 0)
DEFAULT_QUIET_END = time(7, 0)


@dataclass(frozen=True)
class ReportSchedule:
    """One recurring report delivery. Persisted in slice 2; pure data here."""

    template: ReportTemplate
    cadence: ReportCadence
    time_of_day: time
    timezone: str = "Asia/Karachi"
    # 0 = Monday ... 6 = Sunday; only used when cadence == "weekly".
    weekday: int = 0
    channels: tuple[ReportChannel, ...] = ("whatsapp",)
    # Opaque recipient references (user ids); the worker resolves them.
    recipients: tuple[str, ...] = ()
    quiet_start: time = DEFAULT_QUIET_START
    quiet_end: time = DEFAULT_QUIET_END
    overdue_days: int = DEFAULT_OVERDUE_DAYS
    enabled: bool = True
    last_sent_at: datetime | None = None


@dataclass(frozen=True)
class InvoiceRow:
    """Minimal invoice facts the builder needs. The caller filters by permission."""

    status: str
    amount: Decimal | None
    currency: str | None
    invoice_date: date | None = None
    # Extra facts the weekly/overdue builders need; the daily builder ignores them.
    created_at: datetime | None = None
    invoice_ref: str | None = None
    vendor_name: str | None = None


@dataclass(frozen=True)
class PaymentRow:
    """Minimal payment facts the builder needs. The caller filters by permission."""

    status: str
    amount: Decimal
    currency: str
    created_at: datetime | None = None
    verified_at: datetime | None = None
    vendor_name: str | None = None


@dataclass(frozen=True)
class StatusTotal:
    count: int = 0
    amount: Decimal = Decimal("0")


@dataclass(frozen=True)
class DailyCashSummary:
    """Totals for one day, grouped by currency. Amounts are never converted."""

    as_of: date
    currency: str
    invoices: dict[str, StatusTotal] = field(default_factory=dict)
    payments: dict[str, StatusTotal] = field(default_factory=dict)
    overdue_invoices: int = 0
    overdue_payments: int = 0
    total_invoiced: Decimal = Decimal("0")
    total_paid: Decimal = Decimal("0")


def validate_schedule(schedule: ReportSchedule) -> None:
    """Raise ValueError for a schedule that can never send correctly."""
    if schedule.template not in TEMPLATES:
        raise ValueError(f"unknown template: {schedule.template!r}")
    if schedule.cadence not in CADENCES:
        raise ValueError(f"unknown cadence: {schedule.cadence!r}")
    if not 0 <= schedule.weekday <= 6:
        raise ValueError(f"weekday must be 0-6, got {schedule.weekday}")
    if not schedule.channels:
        raise ValueError("at least one channel is required")
    for channel in schedule.channels:
        if channel not in CHANNELS:
            raise ValueError(f"unknown channel: {channel!r}")
    if not schedule.recipients:
        raise ValueError("at least one recipient is required")
    if schedule.overdue_days < 0:
        raise ValueError("overdue_days must be >= 0")
    try:
        ZoneInfo(schedule.timezone)
    except Exception as exc:
        raise ValueError(f"unknown timezone: {schedule.timezone!r}") from exc
    if schedule.quiet_start == schedule.quiet_end:
        raise ValueError("quiet hours must not cover the whole day")


def _tz(schedule: ReportSchedule) -> ZoneInfo:
    return ZoneInfo(schedule.timezone)


def next_due(schedule: ReportSchedule, now: datetime) -> datetime:
    """Next wall-clock occurrence of the schedule after ``now`` (tz-aware)."""
    validate_schedule(schedule)
    tz = _tz(schedule)
    local_now = now.astimezone(tz)
    candidate = datetime.combine(local_now.date(), schedule.time_of_day, tzinfo=tz)
    if schedule.cadence == "daily":
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate
    # Weekly: advance to the next matching weekday (strictly after now).
    days_ahead = (schedule.weekday - local_now.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= local_now:
        candidate += timedelta(days=7)
    return candidate


def is_quiet_hour(schedule: ReportSchedule, now: datetime) -> bool:
    """True when ``now`` falls inside the schedule's quiet window.

    Handles overnight windows (e.g. 22:00-07:00) as well as daytime ones.
    """
    local = now.astimezone(_tz(schedule)).time()
    start, end = schedule.quiet_start, schedule.quiet_end
    if start < end:
        return start <= local < end
    return local >= start or local < end


def previous_due(schedule: ReportSchedule, now: datetime) -> datetime:
    """Most recent scheduled occurrence at or before ``now``.

    This is the delivery period the worker records, so a report for one
    occurrence is never sent twice.
    """
    return _previous_due(schedule, now)


def _previous_due(schedule: ReportSchedule, now: datetime) -> datetime:
    """Most recent scheduled occurrence at or before ``now``."""
    tz = _tz(schedule)
    local_now = now.astimezone(tz)
    candidate = datetime.combine(local_now.date(), schedule.time_of_day, tzinfo=tz)
    if schedule.cadence == "daily":
        if candidate > local_now:
            candidate -= timedelta(days=1)
        return candidate
    days_back = (local_now.weekday() - schedule.weekday) % 7
    candidate -= timedelta(days=days_back)
    if candidate > local_now:
        candidate -= timedelta(days=7)
    return candidate


# A missed occurrence is skipped rather than sent late: the worker polls
# every few minutes, so a 1-hour grace window is generous.
_SEND_GRACE = timedelta(hours=1)


def should_send(schedule: ReportSchedule, now: datetime) -> bool:
    """True when an enabled schedule has an unsent occurrence right now.

    Due = the most recent occurrence is within the grace window, it has not
    been sent yet (``last_sent_at`` is before it), and we are outside quiet
    hours. A long-missed occurrence is skipped; the next one sends normally.
    """
    if not schedule.enabled:
        return False
    if is_quiet_hour(schedule, now):
        return False
    previous = _previous_due(schedule, now)
    if now - previous > _SEND_GRACE:
        return False
    if schedule.last_sent_at is not None:
        last = schedule.last_sent_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=ZoneInfo("UTC"))
        if last >= previous:
            return False
    return True


def build_daily_cash_summary(
    invoices: list[InvoiceRow],
    payments: list[PaymentRow],
    *,
    as_of: date,
    currency: str,
    overdue_days: int = DEFAULT_OVERDUE_DAYS,
) -> DailyCashSummary:
    """Summarise one currency's invoices/payments for ``as_of``.

    Rows of other currencies are ignored (amounts are never converted).
    ``overdue`` means a pending/exception invoice, or a payment still awaiting
    approval, older than ``overdue_days``. Unknown statuses raise ValueError
    so a typo can never silently drop money from the report.
    """
    inv_totals: dict[str, StatusTotal] = {s: StatusTotal() for s in INVOICE_STATUSES}
    pay_totals: dict[str, StatusTotal] = {s: StatusTotal() for s in PAYMENT_STATUSES}
    overdue_invoices = 0
    overdue_payments = 0
    total_invoiced = Decimal("0")
    total_paid = Decimal("0")

    for row in invoices:
        if row.status not in INVOICE_STATUSES:
            raise ValueError(f"unknown invoice status: {row.status!r}")
        if row.currency != currency:
            continue
        amount = row.amount or Decimal("0")
        current = inv_totals[row.status]
        inv_totals[row.status] = StatusTotal(current.count + 1, current.amount + amount)
        total_invoiced += amount
        if row.status in ("pending", "exception") and row.invoice_date is not None:
            if (as_of - row.invoice_date).days > overdue_days:
                overdue_invoices += 1

    for row in payments:
        if row.status not in PAYMENT_STATUSES:
            raise ValueError(f"unknown payment status: {row.status!r}")
        if row.currency != currency:
            continue
        current = pay_totals[row.status]
        pay_totals[row.status] = StatusTotal(current.count + 1, current.amount + row.amount)
        if row.status == "verified":
            total_paid += row.amount
        if row.status == "awaiting_approval" and row.created_at is not None:
            if (as_of - row.created_at.date()).days > overdue_days:
                overdue_payments += 1

    return DailyCashSummary(
        as_of=as_of,
        currency=currency,
        invoices={k: v for k, v in inv_totals.items() if v.count},
        payments={k: v for k, v in pay_totals.items() if v.count},
        overdue_invoices=overdue_invoices,
        overdue_payments=overdue_payments,
        total_invoiced=total_invoiced,
        total_paid=total_paid,
    )


def render_daily_cash_summary_text(summary: DailyCashSummary) -> str:
    """Plain-text body for WhatsApp/email. Contains totals only — no links that
    execute anything and no account numbers."""
    lines = [
        f"Daily cash summary — {summary.as_of.isoformat()} ({summary.currency})",
        f"Total invoiced: {summary.total_invoiced} {summary.currency}",
        f"Total paid (verified): {summary.total_paid} {summary.currency}",
    ]
    if summary.invoices:
        lines.append("Invoices:")
        for status, total in sorted(summary.invoices.items()):
            lines.append(f"  {status}: {total.count} — {total.amount} {summary.currency}")
    if summary.payments:
        lines.append("Payments:")
        for status, total in sorted(summary.payments.items()):
            lines.append(f"  {status}: {total.count} — {total.amount} {summary.currency}")
    if summary.overdue_invoices or summary.overdue_payments:
        lines.append(
            f"Overdue: {summary.overdue_invoices} invoice(s), "
            f"{summary.overdue_payments} payment(s) awaiting approval"
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class VendorTotal:
    vendor_name: str
    count: int = 0
    amount: Decimal = Decimal("0")


@dataclass(frozen=True)
class ExceptionItem:
    ref: str
    vendor_name: str
    amount: Decimal
    currency: str
    age_days: int


@dataclass(frozen=True)
class WeeklyInvoicesSummary:
    """One week of invoices for one currency. Amounts are never converted."""

    week_start: date
    week_end: date
    currency: str
    # Invoices created in the week, grouped by their *current* status. There
    # is no status-change timestamp in the data, so "verified this week" can
    # only mean "created this week and currently verified" — the docstring
    # says so instead of pretending otherwise.
    new_by_status: dict[str, StatusTotal] = field(default_factory=dict)
    new_count: int = 0
    total_new: Decimal = Decimal("0")
    top_vendors: tuple[VendorTotal, ...] = ()
    # Exceptions needing a decision: every current exception, any age.
    exceptions_open: tuple[ExceptionItem, ...] = ()
    exceptions_open_count: int = 0
    # Payments verified inside the week (by verified_at).
    paid_count: int = 0
    total_paid: Decimal = Decimal("0")


def build_weekly_invoices_summary(
    invoices: list[InvoiceRow],
    payments: list[PaymentRow],
    *,
    week_start: date,
    week_end: date,
    currency: str,
    max_exceptions_listed: int = 10,
    max_vendors_listed: int = 5,
) -> WeeklyInvoicesSummary:
    """Summarise one currency's week.

    Rows of other currencies are ignored. Unknown statuses raise ValueError
    so a typo can never silently drop money from the report.
    """
    new_by_status: dict[str, StatusTotal] = {s: StatusTotal() for s in INVOICE_STATUSES}
    vendor_sums: dict[str, VendorTotal] = {}
    exceptions: list[ExceptionItem] = []
    new_count = 0
    total_new = Decimal("0")
    paid_count = 0
    total_paid = Decimal("0")

    for row in invoices:
        if row.status not in INVOICE_STATUSES:
            raise ValueError(f"unknown invoice status: {row.status!r}")
        if row.currency != currency:
            continue
        if row.status == "exception":
            ref = row.invoice_ref or "unnumbered"
            created = row.created_at.date() if row.created_at is not None else row.invoice_date
            age = (week_end - created).days if created is not None else 0
            exceptions.append(ExceptionItem(
                ref=ref, vendor_name=row.vendor_name or "Unknown vendor",
                amount=row.amount or Decimal("0"), currency=currency, age_days=max(0, age)))
        created = row.created_at.date() if row.created_at is not None else row.invoice_date
        if created is None or not (week_start <= created <= week_end):
            continue
        amount = row.amount or Decimal("0")
        current = new_by_status[row.status]
        new_by_status[row.status] = StatusTotal(current.count + 1, current.amount + amount)
        new_count += 1
        total_new += amount
        vendor = row.vendor_name or "Unknown vendor"
        prior = vendor_sums.get(vendor, VendorTotal(vendor))
        vendor_sums[vendor] = VendorTotal(vendor, prior.count + 1, prior.amount + amount)

    for row in payments:
        if row.status not in PAYMENT_STATUSES:
            raise ValueError(f"unknown payment status: {row.status!r}")
        if row.currency != currency or row.status != "verified" or row.verified_at is None:
            continue
        if week_start <= row.verified_at.date() <= week_end:
            paid_count += 1
            total_paid += row.amount

    exceptions.sort(key=lambda item: (-item.amount, item.ref))
    top = sorted(vendor_sums.values(), key=lambda total: (-total.amount, total.vendor_name))
    return WeeklyInvoicesSummary(
        week_start=week_start,
        week_end=week_end,
        currency=currency,
        new_by_status={k: v for k, v in new_by_status.items() if v.count},
        new_count=new_count,
        total_new=total_new,
        top_vendors=tuple(top[:max_vendors_listed]),
        exceptions_open=tuple(exceptions[:max_exceptions_listed]),
        exceptions_open_count=len(exceptions),
        paid_count=paid_count,
        total_paid=total_paid,
    )


def render_weekly_invoices_text(summary: WeeklyInvoicesSummary) -> str:
    """Plain-text body for WhatsApp/email. Contains totals only — no links that
    execute anything and no account numbers."""
    lines = [
        f"Weekly invoices — {summary.week_start.isoformat()} to {summary.week_end.isoformat()} "
        f"({summary.currency})",
        f"New invoices: {summary.new_count} — {summary.total_new} {summary.currency}",
        f"Paid this week (verified): {summary.paid_count} — {summary.total_paid} {summary.currency}",
    ]
    if summary.new_by_status:
        lines.append("New by status:")
        for status, total in sorted(summary.new_by_status.items()):
            lines.append(f"  {status}: {total.count} — {total.amount} {summary.currency}")
    if summary.top_vendors:
        lines.append("Top vendors:")
        for total in summary.top_vendors:
            lines.append(f"  {total.vendor_name}: {total.count} — {total.amount} {summary.currency}")
    if summary.exceptions_open_count:
        lines.append(f"Exceptions needing a decision: {summary.exceptions_open_count}")
        for item in summary.exceptions_open:
            lines.append(
                f"  {item.ref} — {item.vendor_name}: {item.amount} {item.currency} "
                f"({item.age_days}d old)")
        if summary.exceptions_open_count > len(summary.exceptions_open):
            lines.append(
                f"  …and {summary.exceptions_open_count - len(summary.exceptions_open)} more — "
                "open Finance to review.")
    return "\n".join(lines)


@dataclass(frozen=True)
class OverdueItem:
    ref: str
    vendor_name: str
    amount: Decimal
    currency: str
    age_days: int
    kind: str  # "invoice" | "payment"


@dataclass(frozen=True)
class OverdueAlert:
    """Overdue invoices/payments for one currency. Empty means nothing is overdue."""

    as_of: date
    currency: str
    items: tuple[OverdueItem, ...] = ()
    total_count: int = 0
    total_amount: Decimal = Decimal("0")


def build_overdue_alert(
    invoices: list[InvoiceRow],
    payments: list[PaymentRow],
    *,
    as_of: date,
    currency: str,
    overdue_days: int = DEFAULT_OVERDUE_DAYS,
    max_items_listed: int = 20,
) -> OverdueAlert:
    """Collect overdue items for one currency, oldest first.

    "Overdue" is the v1 definition from the plan: a pending/exception
    invoice, or a payment still awaiting approval, older than
    ``overdue_days``. Unknown statuses raise ValueError.
    """
    items: list[OverdueItem] = []
    total_amount = Decimal("0")
    for row in invoices:
        if row.status not in INVOICE_STATUSES:
            raise ValueError(f"unknown invoice status: {row.status!r}")
        if row.currency != currency or row.status not in ("pending", "exception"):
            continue
        if row.invoice_date is None:
            continue
        age = (as_of - row.invoice_date).days
        if age > overdue_days:
            amount = row.amount or Decimal("0")
            items.append(OverdueItem(
                ref=row.invoice_ref or "unnumbered",
                vendor_name=row.vendor_name or "Unknown vendor",
                amount=amount, currency=currency, age_days=age, kind="invoice"))
            total_amount += amount
    for row in payments:
        if row.status not in PAYMENT_STATUSES:
            raise ValueError(f"unknown payment status: {row.status!r}")
        if row.currency != currency or row.status != "awaiting_approval":
            continue
        if row.created_at is None:
            continue
        age = (as_of - row.created_at.date()).days
        if age > overdue_days:
            items.append(OverdueItem(
                ref=row.vendor_name or "payment",
                vendor_name=row.vendor_name or "Unknown vendor",
                amount=row.amount, currency=currency, age_days=age, kind="payment"))
            total_amount += row.amount
    items.sort(key=lambda item: (-item.age_days, item.ref))
    return OverdueAlert(
        as_of=as_of,
        currency=currency,
        items=tuple(items[:max_items_listed]),
        total_count=len(items),
        total_amount=total_amount,
    )


def render_overdue_alert_text(alert: OverdueAlert) -> str:
    """Plain-text body for WhatsApp/email. Lists the stuck items, oldest first."""
    lines = [
        f"Overdue alert — {alert.as_of.isoformat()} ({alert.currency})",
        f"Overdue: {alert.total_count} item(s) — {alert.total_amount} {alert.currency}",
    ]
    for item in alert.items:
        lines.append(
            f"  {item.kind}: {item.ref} — {item.vendor_name}: {item.amount} {item.currency} "
            f"({item.age_days}d overdue)")
    if alert.total_count > len(alert.items):
        lines.append(
            f"  …and {alert.total_count - len(alert.items)} more — open Finance to review.")
    return "\n".join(lines)
