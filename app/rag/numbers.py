"""Locale-aware reading of numbers and amounts, shared by table calculations and the evidence checks.

Business documents write amounts as "Rs. 18,450/-", "PKR 1,25,000", "$1,234.50", "(1,200)" or "12%". A cell is a
number only when its digit grouping is valid in Western (1,234,567) or South Asian (12,34,567) form: a malformed
value ("1,2,3") is not silently turned into a number, so a calculation never sums text it misread.
"""

from __future__ import annotations

import re
import unicodedata

# Thousands grouping: South Asian (lakh/crore) first, so "12,34,567" is not read as "12," plus "34,567".
GROUPED = re.compile(r"(?<![\d,])(?:\d{1,2}(?:,\d{2})+,\d{3}|\d{1,3}(?:,\d{3})+)(?![\d,]?\d)")
_PLAIN = re.compile(r"^[+-]?(?:\d{1,2}(?:,\d{2})+,\d{3}|\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
# A currency written as a code or an abbreviation: an ISO 4217 code (PKR, USD) or the rupee's "Rs"/"Rs.".
_CURRENCY_WORD = re.compile(r"^(?:[A-Z]{3}|Rs\.?)\s*|\s*(?:[A-Z]{3}|Rs\.?)$")
# "/-" or "/=" closes an amount in South Asian business writing ("Rs. 18,450/-": eighteen thousand ... only).
_ONLY_MARK = re.compile(r"\s*/[-=]$")


def ungroup(text: str) -> str:
    """The text with thousands separators removed from validly grouped numbers ("Rs. 1,25,000" -> "Rs. 125000")."""
    return GROUPED.sub(lambda match: match.group(0).replace(",", ""), text)


def parse_amount(value: str) -> float | None:
    """A number, amount or percentage written in a cell, or None when it is not one."""
    text = value.strip()
    negative = text.startswith("(") and text.endswith(")")  # accounting negative
    if negative:
        text = text[1:-1].strip()
    text = _ONLY_MARK.sub("", text).removesuffix("%").strip()
    text = _strip_symbols(_CURRENCY_WORD.sub("", text, count=2)).strip()
    text = _strip_symbols(text)
    if not _PLAIN.match(text):
        return None
    number = float(text.replace(",", ""))
    return -number if negative else number


def _strip_symbols(text: str) -> str:
    """Currency symbols ($, €, £, ₨, ₹ ...) at either end, by Unicode category rather than a list."""
    while text and unicodedata.category(text[0]) == "Sc":
        text = text[1:].lstrip()
    while text and unicodedata.category(text[-1]) == "Sc":
        text = text[:-1].rstrip()
    return text
