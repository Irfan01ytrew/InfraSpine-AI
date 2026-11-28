"""Tests for app.language.reporting (Step Z slice 5) — pure unit tests.

No DB, no network. Covers amount parsing from Roman Urdu / Urdu /
English text, relative date-range parsing, and locale-aware formatting
(western grouping for English, South Asian lakh/crore grouping for
Roman Urdu and Urdu, Urdu-script digits for Urdu).
"""

from datetime import date
from decimal import Decimal

from app.language.reporting import (
    format_report_date,
    format_report_money,
    format_report_number,
    parse_report_amount,
    parse_report_date_range,
)


# ---------------------------------------------------------------- amounts
def test_parse_word_amounts_roman_urdu():
    assert parse_report_amount("do sau") == Decimal("200")
    assert parse_report_amount("paanch hazaar") == Decimal("5000")
    assert parse_report_amount("do sau se zyada kharcha") == Decimal("200")


def test_parse_word_amounts_urdu_script():
    assert parse_report_amount("دو سو") == Decimal("200")
    assert parse_report_amount("پانچ ہزار") == Decimal("5000")


def test_parse_digit_amounts_with_multipliers():
    assert parse_report_amount("2.5 lakh") == Decimal("250000")
    assert parse_report_amount("3 crore") == Decimal("30000000")
    assert parse_report_amount("PKR 12,500") == Decimal("12500")
    assert parse_report_amount("۱۲۵۰۰") == Decimal("12500")


def test_parse_amount_english_digits_only():
    assert parse_report_amount("invoices over 500") == Decimal("500")
    # English "do" is not the number 2.
    assert parse_report_amount("do it now") is None


def test_parse_amount_none_when_absent():
    assert parse_report_amount("aaj ka match kab hai") is None
    assert parse_report_amount("") is None
    assert parse_report_amount("pichle mahine ka kharcha") is None


# ---------------------------------------------------------------- dates
def test_parse_date_range_months():
    today = date(2026, 11, 28)
    assert parse_report_date_range("pichle mahine ka kharcha", today) == (
        date(2026, 10, 1),
        date(2026, 10, 31),
    )
    assert parse_report_date_range("is mahine", today) == (
        date(2026, 11, 1),
        date(2026, 11, 30),
    )
    assert parse_report_date_range("agle mahine", today) == (
        date(2026, 12, 1),
        date(2026, 12, 31),
    )


def test_parse_date_range_weeks_and_years():
    today = date(2026, 11, 28)  # a Saturday
    assert parse_report_date_range("pichle hafte", today) == (
        date(2026, 11, 16),
        date(2026, 11, 22),
    )
    assert parse_report_date_range("is hafte", today) == (
        date(2026, 11, 23),
        date(2026, 11, 29),
    )
    assert parse_report_date_range("aaj", today) == (today, today)
    assert parse_report_date_range("pichle saal", today) == (
        date(2025, 1, 1),
        date(2025, 12, 31),
    )


def test_parse_date_range_urdu_script_and_ambiguous():
    today = date(2026, 11, 28)
    assert parse_report_date_range("پچھلے مہینے", today) == (
        date(2026, 10, 1),
        date(2026, 10, 31),
    )
    # "kal" means yesterday AND tomorrow — never guessed.
    assert parse_report_date_range("kal ka kharcha", today) is None
    assert parse_report_date_range("show me everything", today) is None
    assert parse_report_date_range("", today) is None


# ---------------------------------------------------------------- formatting
def test_format_number_grouping():
    assert format_report_number(1250000, "en") == "1,250,000"
    assert format_report_number(1250000, "roman_urdu") == "12,50,000"
    assert format_report_number(1250000, "urdu") == "۱۲,۵۰,۰۰۰"
    assert format_report_number(12500, "roman_urdu") == "12,500"
    assert format_report_number(100000, "roman_urdu") == "1,00,000"
    assert format_report_number(42, "en") == "42"
    assert format_report_number(-5, "en") == "-5"
    # Unknown language falls back to English grouping.
    assert format_report_number(1250000, "xx") == "1,250,000"


def test_format_money():
    assert format_report_money(Decimal("1250000"), "PKR", "en") == "PKR 1,250,000.00"
    assert format_report_money(Decimal("1250000"), "PKR", "roman_urdu") == "PKR 12,50,000.00"
    assert format_report_money(Decimal("1250000"), "PKR", "urdu") == "PKR ۱۲,۵۰,۰۰۰.۰۰"
    assert format_report_money(Decimal("99.5"), None, "en") == "99.50"
    assert format_report_money("2500", "USD", "en") == "USD 2,500.00"


def test_format_date():
    day = date(2026, 10, 30)
    assert format_report_date(day, "en") == "30 Oct 2026"
    assert format_report_date(day, "roman_urdu") == "30 Aktobar 2026"
    assert format_report_date(day, "urdu") == "۳۰ اکتوبر ۲۰۲۶"
    assert format_report_date(date(2026, 1, 5), "roman_urdu") == "5 Janvari 2026"
