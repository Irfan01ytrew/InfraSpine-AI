"""Locale-aware report parsing and formatting (Step Z slice 5).

Pure functions only: no imports from ``app.platform``, no DB, no network.
Built on the slice-1 helpers (``app.language.normalize`` / ``detect``).

Two directions:

- *Parsing* — a report question written in Roman Urdu or Urdu script
  (``"pichle mahine ka kharcha dikhao"``, ``"do sau se zyada"``) is
  turned into structured values a report query can use: an amount
  (``Decimal``) and a date range. ``"kal"`` is deliberately unparsed —
  it means yesterday *and* tomorrow, and only the caller (which sees
  verb tense) may resolve it.
- *Formatting* — amounts and dates are rendered per language. English
  uses western digit grouping (``1,250,000``); Roman Urdu and Urdu use
  South Asian grouping (``12,50,000`` — lakh/crore grouping, how the
  owner reads large numbers); Urdu script also uses Urdu digits.
  A preference never changes authorization, permissions, approvals, or
  what a user may read — it only changes how numbers and dates are shown.

No new dependency: Python's ``locale`` module needs OS locales that
this VM does not ship, so grouping and month names are small explicit
tables, covered by tests.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .detect import detect_language
from .normalize import normalize_date_words, normalize_digits, normalize_number_words

# ---------------------------------------------------------------------------
# Month names (short form, as reports show them)
# ---------------------------------------------------------------------------

EN_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

ROMAN_URDU_MONTHS = (
    "Janvari", "Farvari", "March", "April", "Mai", "June",
    "Julai", "Agast", "Sitambar", "Aktobar", "Navambar", "Disambar",
)

URDU_MONTHS = (
    "جنوری", "فروری", "مارچ", "اپریل", "مئی", "جون",
    "جولائی", "اگست", "ستمبر", "اکتوبر", "نومبر", "دسمبر",
)

_ASCII_TO_URDU_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

# Currency words stripped before amount parsing (case-insensitive).
_CURRENCY_WORDS = (
    "pkr", "rs", "rs.", "rupee", "rupees", "روپے", "روپیہ", "روپے",
)

# Multiplier words understood next to a digit amount ("2.5 lakh").
_DIGIT_MULTIPLIERS = {
    "sau": 100, "so": 100, "hazaar": 1000, "hazar": 1000,
    "lakh": 100_000, "crore": 10_000_000, "karor": 10_000_000,
    "سو": 100, "ہزار": 1_000, "لاکھ": 100_000, "کروڑ": 10_000_000,
}

_NUMBER_RE = re.compile(r"\d[\d,]*(\.\d+)?")

# Canonical date tokens (after normalize_date_words) → range kind.
# Longest first so "last month" wins over "last".
_DATE_TOKENS = (
    "last month", "this month", "next month",
    "last week", "this week", "next week",
    "last year", "this year", "next year",
    "today",
)


def _to_urdu_digits(text: str) -> str:
    return text.translate(_ASCII_TO_URDU_DIGITS)


def _group_digits(digits: str, south_asian: bool) -> str:
    """Group an integer digit string: western (1,250,000) or South Asian (12,50,000)."""
    if len(digits) <= 3:
        return digits
    if not south_asian:
        parts = []
        while len(digits) > 3:
            parts.append(digits[-3:])
            digits = digits[:-3]
        parts.append(digits)
        return ",".join(reversed(parts))
    head, tail = digits[:-3], digits[-3:]
    pairs = []
    while len(head) > 2:
        pairs.append(head[-2:])
        head = head[:-2]
    pairs.append(head)
    return ",".join(reversed(pairs)) + "," + tail


def format_report_number(value: int | Decimal, language: str) -> str:
    """Format an integer amount with locale grouping and digits.

    ``1250000`` → ``"1,250,000"`` (en), ``"12,50,000"`` (roman_urdu),
    ``"۱۲,۵۰,۰۰۰"`` (urdu). Unknown languages behave as English.
    """
    south_asian = language in ("roman_urdu", "urdu")
    text = _group_digits(str(abs(int(value))), south_asian)
    if value < 0:
        text = "-" + text
    return _to_urdu_digits(text) if language == "urdu" else text


def format_report_money(
    amount: int | Decimal | float | str,
    currency: str | None,
    language: str,
) -> str:
    """Format money for a report line: ``"PKR 12,50,000.00"``.

    Two decimals, currency code first (codes, not symbols, so no
    invented glyphs), South Asian grouping for Roman Urdu / Urdu,
    Urdu-script digits for Urdu. ``currency`` may be ``None``.
    """
    try:
        value = Decimal(str(amount))
    except InvalidOperation:
        value = Decimal("0")
    quantized = value.quantize(Decimal("0.01"))
    sign = "-" if quantized < 0 else ""
    whole, _, frac = f"{abs(quantized):.2f}".partition(".")
    grouped = format_report_number(int(whole), language)
    rendered = f"{sign}{grouped}.{frac}"
    if language == "urdu":
        # The fraction's digits are still Latin; convert the whole string.
        rendered = _to_urdu_digits(rendered)
    if currency:
        return f"{currency} {rendered}"
    return rendered


def format_report_date(day: date, language: str) -> str:
    """Format a date for a report line: ``"30 Oct 2026"``.

    Roman Urdu: ``"30 Aktobar 2026"``; Urdu script:
    ``"۳۰ اکتوبر ۲۰۲۶"`` (Urdu digits, Urdu month name).
    """
    if language == "urdu":
        months = URDU_MONTHS
        return f"{_to_urdu_digits(str(day.day))} {months[day.month - 1]} {_to_urdu_digits(str(day.year))}"
    if language == "roman_urdu":
        months = ROMAN_URDU_MONTHS
    else:
        months = EN_MONTHS
    return f"{day.day} {months[day.month - 1]} {day.year}"


def parse_report_amount(text: str) -> Decimal | None:
    """Parse the first amount out of free text (Roman Urdu / Urdu / English).

    ``"do sau"`` → 200, ``"paanch hazaar"`` → 5000, ``"2.5 lakh"`` →
    250000, ``"PKR 12,500"`` → 12500, ``"۱۲۵۰۰"`` → 12500.
    Returns ``None`` when no amount is present. Word amounts are only
    parsed when the text is Roman Urdu / Urdu / mixed (an English
    ``"do"`` is not the number 2).
    """
    if not text:
        return None
    cleaned = normalize_digits(text)
    lang = detect_language(cleaned)
    if lang in ("roman_urdu", "urdu", "mixed"):
        # Digit + multiplier first ("2.5 lakh"): normalize_number_words
        # mishandles the decimal point, so handle it explicitly.
        lowered = cleaned.casefold()
        for word in _CURRENCY_WORDS:
            lowered = lowered.replace(word, " ")
        digit_match = re.search(r"(\d[\d,]*\.?\d*)\s*([A-Za-z\u0600-\u06ff]+)?", lowered)
        if digit_match:
            try:
                value = Decimal(digit_match.group(1).replace(",", ""))
            except InvalidOperation:
                value = None
            if value is not None:
                multiplier_word = (digit_match.group(2) or "").strip()
                if multiplier_word in _DIGIT_MULTIPLIERS:
                    value *= _DIGIT_MULTIPLIERS[multiplier_word]
                return value
        # Word-only amount ("do sau", "paanch hazaar", "دو سو").
        normalized = normalize_number_words(cleaned)
        match = _NUMBER_RE.search(normalized)
        if match:
            try:
                return Decimal(match.group(0).replace(",", ""))
            except InvalidOperation:
                return None
        return None
    # English: digits only, currency words stripped.
    lowered = cleaned.casefold()
    for word in _CURRENCY_WORDS:
        lowered = lowered.replace(word, " ")
    match = _NUMBER_RE.search(lowered)
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None


def parse_report_date_range(text: str, today: date | None = None) -> tuple[date, date] | None:
    """Parse a relative date phrase into an inclusive (start, end) range.

    ``"pichle mahine"`` → the whole of last month; ``"is hafte"`` →
    Monday–Sunday of this week; ``"aaj"`` → today. ``"kal"`` returns
    ``None`` (yesterday or tomorrow — ambiguous by design). Unknown
    phrases return ``None`` instead of guessing.
    """
    if not text:
        return None
    today = today or date.today()
    normalized = normalize_date_words(normalize_digits(text.casefold()))
    token = next((t for t in _DATE_TOKENS if t in normalized), None)
    if token is None:
        return None
    if token == "today":
        return today, today
    if token in ("this week", "last week", "next week"):
        monday = today - timedelta(days=today.weekday())
        if token == "last week":
            monday -= timedelta(days=7)
        elif token == "next week":
            monday += timedelta(days=7)
        return monday, monday + timedelta(days=6)
    if token in ("this month", "last month", "next month"):
        year, month = today.year, today.month
        if token == "last month":
            month -= 1
            if month == 0:
                month, year = 12, year - 1
        elif token == "next month":
            month += 1
            if month == 13:
                month, year = 1, year + 1
        start = date(year, month, 1)
        return start, date(year, month, monthrange(year, month)[1])
    # this year / last year / next year
    year = today.year + {"last year": -1, "this year": 0, "next year": 1}[token]
    return date(year, 1, 1), date(year, 12, 31)
