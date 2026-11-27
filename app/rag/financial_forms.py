"""Bounded, source-linked extraction for common finance documents.

This module is deliberately deterministic: it never asks an LLM to invent field values. Missing, malformed, or
ambiguous values are returned as review exceptions instead of being filled from context or guessed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.rag.parsers.base import ExtractedTable

MAX_TEXT_CHARS = 200_000
MAX_TABLES = 20
MAX_ROWS_PER_TABLE = 2_000
MAX_LINE_ITEMS = 2_000

_LABEL_SEPARATOR = re.compile(r"\s*(?::|\||\t)\s*", re.UNICODE)
_NUMBER = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})+,\d{3}|\d+)(?:\.\d{1,4})?$")
_DATE_FORMATS = {
    "dmy": ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"),
    "mdy": ("%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y"),
    "ymd": ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d"),
}
_ISO_CURRENCIES = ("PKR", "USD", "EUR", "GBP", "INR", "AED", "SAR", "CAD", "AUD", "JPY")
_CURRENCY_RE = re.compile(r"\b(" + "|".join(_ISO_CURRENCIES) + r")\b", re.IGNORECASE)
_RUPEE_RE = re.compile(r"\bRs\.?(?=\s|[0-9])|\u20b9|\u20a8", re.IGNORECASE)
_DOLLAR_RE = re.compile(r"\$|\u20ac|\u00a3")

_ALIASES = {
    "invoice_number": ("invoice number", "invoice no", "invoice #", "invoice id"),
    "purchase_order_number": ("purchase order number", "purchase order no", "po number", "po no", "po #"),
    "document_date": ("invoice date", "purchase order date", "order date", "issue date", "date"),
    "vendor": ("vendor", "supplier", "vendor name", "supplier name", "billed by", "issued by"),
    "subtotal": ("subtotal", "sub total", "net amount"),
    "tax": ("tax", "sales tax", "vat", "gst"),
    "total": ("grand total", "invoice total", "total due", "amount due", "order total", "total"),
}
_REQUIRED = {
    "invoice": ("invoice_number", "document_date", "vendor", "total"),
    "purchase_order": ("purchase_order_number", "document_date", "vendor", "total"),
}


@dataclass(frozen=True)
class FormIssue:
    code: str
    field: str
    message: str
    severity: str = "error"
    source: dict[str, Any] | None = None


@dataclass(frozen=True)
class FormField:
    value: str | None
    raw: str | None
    valid: bool
    source: dict[str, Any] | None = None


@dataclass(frozen=True)
class FormExtraction:
    profile: str
    status: str
    fields: dict[str, FormField]
    line_items: tuple[dict[str, Any], ...]
    exceptions: tuple[FormIssue, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "status": self.status,
            "fields": {name: asdict(value) for name, value in self.fields.items()},
            "line_items": list(self.line_items),
            "exceptions": [asdict(issue) for issue in self.exceptions],
        }


def detect_form_profile(filename: str, text: str) -> str | None:
    """Classify only explicit invoice / purchase-order signals; unrelated files remain unclassified."""
    name = filename.casefold()
    content = text[:MAX_TEXT_CHARS]
    if re.search(r"purchase[ _-]?order|\bpo\b", name) or re.search(
        r"\bpurchase[ -]order\b|^\s*PO\s*(?:no\.?|number|#)\s*[:#]?", content, re.IGNORECASE | re.MULTILINE
    ):
        return "purchase_order"
    if re.search(r"\binvoice\b", name) or re.search(
        r"\b(?:tax\s+)?invoice\s*(?:no\.?|number|#|date)\s*[:#]?|^\s*(?:tax\s+)?invoice\s*$",
        content,
        re.IGNORECASE | re.MULTILINE,
    ):
        return "invoice"
    return None


def extract_financial_form(
    profile: str,
    text_sections: list[tuple[str, dict[str, Any] | None]],
    tables: list[ExtractedTable],
    *,
    date_order: str = "dmy",
) -> FormExtraction:
    """Extract one invoice or purchase order from parser sections and structured tables.

    `text_sections` hold extracted text and its original page/section locator. Amounts are represented as decimal
    strings. Table data is bounded and every accepted field keeps a source locator for review.
    """
    if profile not in {"invoice", "purchase_order", "form"}:
        raise ValueError("Unsupported financial form profile.")
    if date_order not in _DATE_FORMATS:
        raise ValueError("date_order must be dmy, mdy, or ymd.")
    text = "\n".join(section for section, _ in text_sections)[:MAX_TEXT_CHARS]
    labels = _label_values(text_sections, tables)
    exceptions: list[FormIssue] = []
    if len(text_sections) > 100 or sum(len(section) for section, _ in text_sections) > MAX_TEXT_CHARS:
        exceptions.append(FormIssue("text_limit", "document", "Text exceeded the extraction limit; review all pages."))
    if len(tables) > MAX_TABLES or any(len(table.rows) > MAX_ROWS_PER_TABLE for table in tables[:MAX_TABLES]):
        exceptions.append(FormIssue("table_limit", "line_items", "A table exceeded the extraction limit; the result may be incomplete."))
    fields: dict[str, FormField] = {}

    if profile == "form":
        raw_fields = {label: item for label, item in labels.items() if label and item[0]}
        if not raw_fields:
            exceptions.append(FormIssue("no_fields_found", "fields", "No labeled fields were extracted."))
        for label, (raw, source) in list(raw_fields.items())[:100]:
            key = _field_key(label)
            fields[key] = FormField(_clean(raw), raw, bool(_clean(raw)), source)
        return _result(profile, fields, (), exceptions)

    number_field = "invoice_number" if profile == "invoice" else "purchase_order_number"
    fields[number_field] = _string_field(number_field, labels, exceptions)
    fields["document_date"] = _date_field("document_date", labels, date_order, exceptions)
    fields["vendor"] = _string_field("vendor", labels, exceptions)
    for field_name in ("subtotal", "tax", "total"):
        fields[field_name] = _money_field(field_name, labels, exceptions)
    fields["currency"] = _currency_field(labels, exceptions)

    line_items = _line_items(tables, exceptions)
    subtotal = _decimal(fields.get("subtotal"))
    tax = _decimal(fields.get("tax"))
    total = _decimal(fields.get("total"))
    if subtotal is not None and tax is not None and total is not None and abs(subtotal + tax - total) > Decimal("0.01"):
        exceptions.append(FormIssue(
            "total_components_mismatch", "total",
            "Subtotal plus tax differs from total; check discounts, fees, and rounding.", "warning",
            fields["total"].source,
        ))
    return _result(profile, fields, tuple(line_items), exceptions)


# Known field labels followed by a separator. Layout parsers (Docling) can return a block of "Label: value" lines as
# one paragraph; these positions split it back into one label per line. Longest labels first, so "Invoice Date"
# wins over "Date" and "Grand Total" over "Total".
_KNOWN_LABEL = re.compile(
    r"(?<![\w])(?:"
    + "|".join(sorted({re.escape(alias) for aliases in _ALIASES.values() for alias in aliases}, key=len, reverse=True))
    + r")\s*(?::|\|)",
    re.IGNORECASE,
)


def _label_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.splitlines():
        starts = [match.start() for match in _KNOWN_LABEL.finditer(line)]
        if len(starts) < 2:
            lines.append(line)
            continue
        lines.append(line[: starts[0]])  # any heading before the first label ("TAX INVOICE")
        bounds = [*starts, len(line)]
        lines.extend(line[start:end] for start, end in zip(bounds, bounds[1:]))
    return lines


def _label_values(
    text_sections: list[tuple[str, dict[str, Any] | None]], tables: list[ExtractedTable]
) -> dict[str, tuple[str, dict[str, Any] | None]]:
    found: dict[str, tuple[str, dict[str, Any] | None]] = {}
    for text, locator in text_sections[:100]:
        for line in _label_lines(text[:MAX_TEXT_CHARS]):
            split = _LABEL_SEPARATOR.split(line.strip(), maxsplit=1)
            if len(split) == 2 and _norm(split[0]) and _clean(split[1]):
                found.setdefault(_norm(split[0]), (_clean(split[1]), locator))
    for table in tables[:MAX_TABLES]:
        columns = [_norm(str(column)) for column in table.columns]
        for row_index, row in enumerate(table.rows[:MAX_ROWS_PER_TABLE]):
            if len(row) < 2:
                continue
            for label, raw in zip(columns, row, strict=False):
                if label and _clean(str(raw)):
                    position = table.row_positions[row_index] if row_index < len(table.row_positions) else table.position
                    source = {"kind": table.kind, "position": position, "table_index": table.index, "row_number": row_index + 1}
                    found.setdefault(label, (_clean(str(raw)), source))
        for row_index, row in enumerate(table.summary_rows[:MAX_ROWS_PER_TABLE]):
            for column_index, raw in enumerate(row):
                if column_index < len(columns) and columns[column_index] and _clean(str(raw)):
                    position = table.position
                    source = {"kind": table.kind, "position": position, "table_index": table.index, "summary_row": row_index + 1}
                    found.setdefault(columns[column_index], (_clean(str(raw)), source))
            first = _norm(str(row[0])) if row else ""
            if "total" in first:
                for index, column in enumerate(columns):
                    if any(term in column for term in ("amount", "total", "price")) and index < len(row) and _clean(str(row[index])):
                        source = {"kind": table.kind, "position": table.position, "table_index": table.index,
                                  "summary_row": row_index + 1}
                        found.setdefault("grand total", (_clean(str(row[index])), source))
                        break
    return found


def _pick(labels: dict[str, tuple[str, dict[str, Any] | None]], field_name: str):
    for alias in _ALIASES[field_name]:
        value = labels.get(_norm(alias))
        if value:
            return value
    return None


def _string_field(field_name: str, labels, exceptions: list[FormIssue]) -> FormField:
    item = _pick(labels, field_name)
    if not item:
        exceptions.append(FormIssue("required_field_missing", field_name, f"{field_name.replace('_', ' ').title()} was not found."))
        return FormField(None, None, False)
    value, source = item
    return FormField(_clean(value), value, bool(_clean(value)), source)


def _date_field(field_name: str, labels, date_order: str, exceptions: list[FormIssue]) -> FormField:
    item = _pick(labels, field_name)
    if not item:
        exceptions.append(FormIssue("required_field_missing", field_name, "Document date was not found."))
        return FormField(None, None, False)
    raw, source = item
    value = _parse_date(raw, date_order)
    if value is None:
        exceptions.append(FormIssue("invalid_date", field_name, "Date could not be parsed using the configured date order.", source=source))
    return FormField(value.isoformat() if value else None, raw, value is not None, source)


def _money_field(field_name: str, labels, exceptions: list[FormIssue]) -> FormField:
    item = _pick(labels, field_name)
    if not item:
        if field_name == "total":
            exceptions.append(FormIssue("required_field_missing", field_name, "Total amount was not found."))
        return FormField(None, None, field_name != "total")
    raw, source = item
    parsed = _parse_money(raw)
    if parsed is None:
        exceptions.append(FormIssue("invalid_amount", field_name, "Amount is not a valid grouped decimal value.", source=source))
        return FormField(None, raw, False, source)
    amount, _ = parsed
    return FormField(format(amount, "f"), raw, True, source)


def _currency_field(labels, exceptions: list[FormIssue]) -> FormField:
    for name in ("total", "subtotal", "tax"):
        item = _pick(labels, name)
        if not item:
            continue
        raw, source = item
        parsed = _parse_money(raw)
        if parsed is None:
            continue
        _, currency = parsed
        if currency:
            return FormField(currency, raw, True, source)
        if _RUPEE_RE.search(raw) or _DOLLAR_RE.search(raw):
            exceptions.append(FormIssue(
                "ambiguous_currency", "currency", "Currency symbol is ambiguous; select the ISO currency during review.",
                "warning", source,
            ))
            return FormField(None, raw, False, source)
        exceptions.append(FormIssue(
            "currency_missing", "currency", "No explicit currency code was found; verify the amount's currency.",
            "warning", source,
        ))
        return FormField(None, raw, False, source)
    return FormField(None, None, False)


def _line_items(tables: list[ExtractedTable], exceptions: list[FormIssue]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for table in tables[:MAX_TABLES]:
        if not table.structured:
            continue
        columns = [_norm(str(column)) for column in table.columns]
        desc = _column(columns, {"description", "item", "product", "service", "details"})
        amount = _column(columns, {"amount", "line total", "extended amount", "total amount"})
        quantity = _column(columns, {"quantity", "qty", "units"})
        unit_price = _column(columns, {"unit price", "rate", "price each", "unit cost"})
        if desc is None or amount is None:
            continue
        for row_index, row in enumerate(table.rows[:MAX_ROWS_PER_TABLE]):
            if len(items) >= MAX_LINE_ITEMS:
                exceptions.append(FormIssue("line_item_limit", "line_items", "Line item count exceeded the extraction limit.", "warning"))
                return items
            description = _cell(row, desc)
            raw_amount = _cell(row, amount)
            if not description and not raw_amount:
                continue
            parsed_amount = _parse_money(raw_amount) if raw_amount else None
            source = {
                "kind": table.kind,
                "position": table.row_positions[row_index] if row_index < len(table.row_positions) else table.position,
                "table_index": table.index,
                "row_number": row_index + 1,
            }
            if parsed_amount is None:
                exceptions.append(FormIssue("invalid_line_amount", f"line_items[{row_index + 1}].amount",
                                            "Line item amount is missing or invalid.", source=source))
            raw_qty = _cell(row, quantity) if quantity is not None else ""
            qty = _parse_decimal(raw_qty) if raw_qty else None
            raw_unit = _cell(row, unit_price) if unit_price is not None else ""
            unit = _parse_money(raw_unit) if raw_unit else None
            if raw_qty and qty is None:
                exceptions.append(FormIssue("invalid_quantity", f"line_items[{row_index + 1}].quantity",
                                            "Quantity is not a valid decimal.", source=source))
            if raw_unit and unit is None:
                exceptions.append(FormIssue("invalid_unit_price", f"line_items[{row_index + 1}].unit_price",
                                            "Unit price is not a valid amount.", source=source))
            if qty is not None and unit is not None and parsed_amount is not None and abs(qty * unit[0] - parsed_amount[0]) > Decimal("0.01"):
                exceptions.append(FormIssue("line_total_mismatch", f"line_items[{row_index + 1}].amount",
                                            "Quantity times unit price differs from the row amount.", "warning", source))
            items.append({
                "description": description or None,
                "quantity": format(qty, "f") if qty is not None else None,
                "unit_price": format(unit[0], "f") if unit else None,
                "amount": format(parsed_amount[0], "f") if parsed_amount else None,
                "currency": parsed_amount[1] if parsed_amount else None,
                "source": source,
            })
    return items


def _column(columns: list[str], aliases: set[str]) -> int | None:
    return next((index for index, column in enumerate(columns) if column in aliases), None)


def _cell(row: tuple[str, ...], index: int | None) -> str:
    return _clean(str(row[index])) if index is not None and index < len(row) else ""


def _parse_money(raw: str) -> tuple[Decimal, str | None] | None:
    text = raw.strip().replace("\u00a0", " ")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    text = re.sub(r"\s*/[-=]$", "", text).strip()
    currency_match = _CURRENCY_RE.search(text)
    currency = currency_match.group(1).upper() if currency_match else None
    text = _CURRENCY_RE.sub("", text)
    text = re.sub(r"(?i)\bRs\.?(?=\s|[0-9])", "", text)
    text = "".join(char for char in text if unicodedata.category(char) != "Sc")
    text = text.replace(" ", "").strip()
    if not _NUMBER.fullmatch(text):
        return None
    try:
        amount = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    return (-amount if negative else amount), currency


def _parse_decimal(raw: str) -> Decimal | None:
    text = raw.strip().replace(",", "")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d{1,4})?", text):
        return None
    try:
        value = Decimal(text)
        return value if value.is_finite() else None
    except InvalidOperation:
        return None


def _parse_date(raw: str, date_order: str) -> date | None:
    value = raw.strip()
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    for pattern in _DATE_FORMATS[date_order]:
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    return None


def _decimal(field: FormField | None) -> Decimal | None:
    if field is None or not field.valid or field.value is None:
        return None
    try:
        return Decimal(field.value)
    except InvalidOperation:
        return None


def _field_key(label: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", _norm(label)).strip("_")[:80]
    return value or "field"


def _norm(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _clean(value: str) -> str:
    return " ".join(value.split()).strip(" |\t")


def _result(profile: str, fields: dict[str, FormField], line_items: tuple[dict[str, Any], ...], issues: list[FormIssue]) -> FormExtraction:
    status = "needs_review" if issues else "ready"
    return FormExtraction(profile, status, fields, line_items, tuple(issues))
