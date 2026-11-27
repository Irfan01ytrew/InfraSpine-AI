"""Plain text (TXT) and comma-separated values (CSV).

TXT sections are blocks of whole lines; CSV sections are blocks of whole rows that repeat the header, so
every chunk is self-describing and cites an exact line or row range.
"""

import csv
import io
import json
from html.parser import HTMLParser
from pathlib import Path

from app.rag.parsers.base import Locator, ParseError, ParseLimits, Section, TextBudget, require_text

_DELIMITERS = ",;\t|"


def decode_text(data: bytes) -> str:
    """UTF-8 / UTF-16 with BOM, then UTF-8, then Windows-1252 (common for legacy exports)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _read(path: Path, limits: ParseLimits) -> str:
    text = decode_text(path.read_bytes()).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    if len(text) > limits.max_chars:
        raise ParseError("text_too_large", "The file contains more text than the processing limit allows.")
    return text


def line_range_label(start: int, end: int) -> str:
    return f"Line {start}" if start == end else f"Lines {start}–{end}"


def _sections_from_text(value: str, limits: ParseLimits) -> list[Section]:
    """Split extracted text into bounded, line-addressable sections shared by text formats."""
    if len(value) > limits.max_chars:
        raise ParseError("text_too_large", "The file contains more text than the processing limit allows.")
    lines = value.split("\n")
    sections: list[Section] = []
    block: list[str] = []
    start = 1
    size = 0

    def flush(end: int) -> None:
        text = "\n".join(block).strip()
        if text:
            label = line_range_label(start, end)
            sections.append(Section(text, Locator("lines", len(sections) + 1, label,
                                                   {"start_line": start, "end_line": end})))

    for number, line in enumerate(lines, start=1):
        if block and size + len(line) + 1 > limits.section_chars:
            flush(number - 1)
            block, start, size = [], number, 0
        if not block and not line.strip():
            start = number + 1
            continue
        block.append(line)
        size += len(line) + 1
    flush(len(lines))
    return require_text(sections)


class TextParser:
    file_format = "txt"
    extensions = (".txt",)

    def inspect(self, path: Path, limits: ParseLimits) -> int | None:
        _read(path, limits)
        return None

    def parse(self, path: Path, limits: ParseLimits) -> list[Section]:
        return _sections_from_text(_read(path, limits), limits)


class MarkdownParser(TextParser):
    file_format = "md"
    extensions = (".md", ".markdown")


class JsonParser:
    """Validate JSON and index a readable, deterministic representation with line citations."""

    file_format = "json"
    extensions = (".json",)

    @staticmethod
    def _normalised(path: Path, limits: ParseLimits) -> str:
        raw = _read(path, limits)
        try:
            value = json.loads(raw)
            rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
            raise ParseError("invalid_file", "The JSON file could not be read. Check that it is valid JSON.") from None
        if len(rendered) > limits.max_chars:
            raise ParseError("text_too_large", "The file contains more text than the processing limit allows.")
        return rendered

    def inspect(self, path: Path, limits: ParseLimits) -> int | None:
        self._normalised(path, limits)
        return None

    def parse(self, path: Path, limits: ParseLimits) -> list[Section]:
        return _sections_from_text(self._normalised(path, limits), limits)


class _VisibleHtml(HTMLParser):
    _BLOCKS = frozenset({"address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
                         "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
                         "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
                         "table", "tbody", "td", "th", "thead", "tr", "ul"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "template", "noscript"}:
            self.hidden_depth += 1
        elif not self.hidden_depth and tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "template", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif not self.hidden_depth and tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth and data.strip():
            self.parts.append(data.strip())


class HtmlParser:
    """Extract visible HTML text locally; scripts and styles are excluded and never executed."""

    file_format = "html"
    extensions = (".html", ".htm")

    @staticmethod
    def _visible(path: Path, limits: ParseLimits) -> str:
        parser = _VisibleHtml()
        try:
            parser.feed(_read(path, limits))
            parser.close()
        except (ValueError, RecursionError):
            raise ParseError("invalid_file", "The HTML file could not be read. It may be damaged.") from None
        visible = "\n".join(part.strip() for part in parser.parts if part.strip())
        if len(visible) > limits.max_chars:
            raise ParseError("text_too_large", "The file contains more text than the processing limit allows.")
        return visible

    def inspect(self, path: Path, limits: ParseLimits) -> int | None:
        self._visible(path, limits)
        return None

    def parse(self, path: Path, limits: ParseLimits) -> list[Section]:
        return _sections_from_text(self._visible(path, limits), limits)


def _dialect(sample: str) -> type[csv.Dialect] | csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=_DELIMITERS)
    except csv.Error:
        return csv.excel


def _cell(value: str) -> str:
    return " ".join(value.split())


def record_line(label: str, headers: list[str], values: list[str]) -> str:
    """'Row 7: Region: North; Units: 42' - keeps the column name next to every value."""
    parts = []
    for index, value in enumerate(values):
        value = _cell(value)
        if not value:
            continue
        name = headers[index] if index < len(headers) and headers[index] else f"Column {index + 1}"
        parts.append(f"{name}: {value}")
    return f"{label}: " + "; ".join(parts) if parts else ""


class CsvParser:
    file_format = "csv"
    extensions = (".csv",)

    def _rows(self, path: Path, limits: ParseLimits) -> list[list[str]]:
        text = _read(path, limits)
        try:
            return list(csv.reader(io.StringIO(text), _dialect(text[:20_000])))
        except csv.Error:
            raise ParseError("invalid_file", "The CSV file could not be read. It may be damaged.") from None

    def inspect(self, path: Path, limits: ParseLimits) -> int | None:
        self._rows(path, limits)
        return None

    def parse(self, path: Path, limits: ParseLimits) -> list[Section]:
        rows = self._rows(path, limits)
        first = next((i for i, row in enumerate(rows) if any(c.strip() for c in row)), None)
        if first is None:
            raise ParseError("no_text", "No extractable text found.")
        headers = [_cell(c) for c in rows[first]]
        columns = ", ".join(h for h in headers if h)
        budget = TextBudget(limits.max_chars * 2)  # labels and column names add to the raw text
        sections: list[Section] = []
        block: list[str] = []
        start = end = first + 2

        def flush() -> None:
            if block:
                label = f"Row {start}" if start == end else f"Rows {start}–{end}"
                text = f"{label} (columns: {columns})\n" + "\n".join(block)
                sections.append(
                    Section(budget.take(text), Locator("rows", len(sections) + 1, label, {"start_row": start, "end_row": end}))
                )

        size = 0
        room = max(limits.section_chars - len(columns) - 40, limits.section_chars // 2)  # header line repeats
        for number, row in enumerate(rows[first + 1 :], start=first + 2):  # 1-based file rows; header excluded
            line = record_line(f"Row {number}", headers, row)
            if not line:
                continue
            if block and size + len(line) + 1 > room:
                flush()
                block, size = [], 0
            if not block:
                start = number
            block.append(line)
            end = number
            size += len(line) + 1
        flush()
        if not sections:  # header only: index the header itself
            sections.append(Section(f"Columns: {columns}", Locator("rows", 1, f"Row {first + 1}", {"start_row": first + 1, "end_row": first + 1})))
        return require_text(sections)
