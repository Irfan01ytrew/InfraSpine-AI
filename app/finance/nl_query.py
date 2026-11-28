"""Natural-language -> structured finance-report query (Step X, slice 1).

Pure stdlib. No database, no network, no app imports. The parser only
*describes* what the user asked for; execution (slice 2) runs the query
against the database and computes every number from real rows, so a number
can never be invented by model text.

The owner writes Roman Urdu (Latin script) and English, often mixed in one
sentence ("last month's invoices, unpaid ones" / "pichle mahine ke unpaid
invoices dikhao"). Both are understood.

Safety: the question text is untrusted. It can only select from a fixed
allow-list of entities, statuses, date ranges and aggregates. Unknown words
are ignored, never interpreted as filters. The parser never executes
anything, never touches a database, and never changes a payee or a scope.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

# FinInvoice.status values (app/db/finance_models.py).
INVOICE_STATUSES = ("pending", "verified", "exception", "approved", "paid", "rejected")

# "Unpaid" means the invoice is still open for payment: it has not been paid
# and it is not closed as rejected.
UNPAID_STATUSES = ("pending", "verified", "exception", "approved")

AGGREGATES = ("rows", "count", "sum", "sum_by_vendor", "sum_by_department", "sum_by_month")

_REPORT_WORDS = (
    "invoice", "invoices", "bill", "bills", "report", "spend", "kharcha",
    "kharch", "payment", "payments", "vendor", "vendors", "kitne", "kitna",
    "kitni", "adaigi", "payment",
)

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_STOPWORDS = {
    "in", "for", "of", "the", "a", "an", "last", "this", "unpaid", "paid",
    "pending", "approved", "rejected", "month", "week", "year", "days", "day",
    "mahina", "mahine", "hafta", "hafte", "saal", "din", "invoices", "invoice",
    "bills", "bill", "report", "spend", "kharcha", "kharch", "payments",
    "payment", "dikhao", "show", "list", "batao", "total", "kul", "se", "ke",
    "ki", "ka", "aur", "all", "sab",
}


@dataclass(frozen=True)
class ReportQuery:
    """A parsed report request. `date_to` is inclusive. Empty `statuses`
    means "all statuses". `warnings` must be shown to the user."""

    intent: str  # 'invoices' | 'unknown'
    date_from: date | None
    date_to: date | None
    statuses: tuple[str, ...]
    vendor: str | None
    department: str | None
    aggregate: str
    currency: str | None
    warnings: tuple[str, ...]
    raw: str


def _norm(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _has(text: str, *phrases: str) -> bool:
    return any(re.search(r"(?:^|\s)" + re.escape(p) + r"(?:\s|$)", text) for p in phrases)


def _month_range(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def _parse_dates(text: str, today: date) -> tuple[date | None, date | None, tuple[str, ...]]:
    warnings: list[str] = []

    # Named month, optional year: "november 2026", "nov 2026", "november".
    m = re.search(r"\b(" + "|".join(_MONTHS) + r")\s*(\d{4})?\b", text)
    if m:
        month = _MONTHS[m.group(1)]
        year = int(m.group(2)) if m.group(2) else today.year
        if m.group(2) is None and month > today.month:
            # "december" asked in January means last December.
            year -= 1
        return (*_month_range(year, month), tuple(warnings))

    if _has(text, "last month", "pichla mahina", "pichle mahine", "guzra mahina", "last mahina"):
        year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        return (*_month_range(year, month), tuple(warnings))
    if _has(text, "this month", "is mahina", "iss mahine", "is mahine"):
        return (*_month_range(today.year, today.month), tuple(warnings))

    if _has(text, "last week", "pichla hafta", "pichle hafte", "guzra hafta", "last hafta"):
        monday = today - timedelta(days=today.weekday() + 7)
        return monday, monday + timedelta(days=6), tuple(warnings)
    if _has(text, "this week", "is hafte", "iss hafte", "is hafta"):
        monday = today - timedelta(days=today.weekday())
        return monday, today, tuple(warnings)

    m = re.search(r"\b(?:last|pichle|akhri)\s+(\d{1,3})\s+(?:days|din)\b", text)
    if m:
        days = max(1, min(int(m.group(1)), 365))
        return today - timedelta(days=days - 1), today, tuple(warnings)

    if _has(text, "last year", "pichla saal", "pichle saal", "guzra saal", "last saal"):
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), tuple(warnings)
    if _has(text, "this year", "is saal", "iss saal"):
        return date(today.year, 1, 1), today, tuple(warnings)

    if _has(text, "today", "aaj"):
        return today, today, tuple(warnings)
    if _has(text, "yesterday") or _has(text, "kal"):
        # Roman Urdu "kal" means yesterday AND tomorrow. For a report about
        # the past we assume yesterday and warn the user.
        if _has(text, "kal"):
            warnings.append(
                "'kal' can mean yesterday or tomorrow; treated as yesterday."
            )
        return today - timedelta(days=1), today - timedelta(days=1), tuple(warnings)

    return None, None, tuple(warnings)


def _parse_statuses(text: str) -> tuple[str, ...]:
    # "unpaid" contains "paid" — check it first.
    if _has(text, "unpaid", "na ada", "na-ada", "baqi", "jo pay nahi hue", "pay nahi hue"):
        return UNPAID_STATUSES
    if _has(text, "paid", "ada", "pay ho gaye", "pay hogaye"):
        return ("paid",)
    if _has(text, "approved", "approve"):
        return ("approved",)
    if _has(text, "pending"):
        return ("pending",)
    if _has(text, "rejected", "reject"):
        return ("rejected",)
    if _has(text, "exception", "masla", "maslay"):
        return ("exception",)
    return ()


def _parse_aggregate(text: str) -> str:
    if _has(text, "by vendor", "vendor wise", "vendor ke hisab se", "vendor hisab se"):
        return "sum_by_vendor"
    if _has(text, "by department", "department wise", "department ke hisab se"):
        return "sum_by_department"
    if _has(text, "by month", "month wise", "mahine ke hisab se"):
        return "sum_by_month"
    if _has(text, "how many", "kitne", "kitni", "count", "gin"):
        return "count"
    if _has(text, "total", "kul", "kitna", "how much", "spend", "kharcha", "kharch", "sum"):
        return "sum"
    return "rows"


def _capture_after(text: str, *markers: str) -> str | None:
    """Capture up to 3 non-stopword words after a marker like 'from'/'vendor'."""
    for marker in markers:
        m = re.search(r"(?:^|\s)" + re.escape(marker) + r"\s+([a-z0-9][a-z0-9\s]{0,60})", text)
        if not m:
            continue
        words = [w for w in m.group(1).split() if w not in _STOPWORDS][:3]
        if words:
            return " ".join(words)
    return None


def parse_nl_report(text: str, *, today: date | None = None) -> ReportQuery:
    """Parse a plain-words report question into a structured query.

    Never raises on ordinary input, never touches a database, never invents
    numbers. Returns intent 'unknown' when the text is not a report question.
    """
    today = today or date.today()
    raw = text.strip()
    norm = _norm(raw)

    if not norm or not any(w in norm.split() for w in _REPORT_WORDS):
        return ReportQuery(
            intent="unknown", date_from=None, date_to=None, statuses=(),
            vendor=None, department=None, aggregate="rows", currency=None,
            warnings=("Not a report question — try e.g. 'last month unpaid invoices'.",),
            raw=raw,
        )

    date_from, date_to, warnings = _parse_dates(norm, today)
    warnings = list(warnings)
    if date_from is None:
        warnings.append("No date range given — all dates included.")

    currency = None
    m = re.search(r"\b(pkr|usd|eur|gbp|aed|sar|inr)\b", norm)
    if m:
        currency = m.group(1).upper()

    return ReportQuery(
        intent="invoices",
        date_from=date_from,
        date_to=date_to,
        statuses=_parse_statuses(norm),
        vendor=_capture_after(norm, "from", "vendor"),
        department=_capture_after(norm, "department", "dept"),
        aggregate=_parse_aggregate(norm),
        currency=currency,
        warnings=tuple(warnings),
        raw=raw,
    )
