"""Common types for document parsers. Every format produces the same output: a list of `Section`s, each
with text and a structured `Locator` that says where in the source file the text came from.

The rest of the RAG pipeline (chunking, embedding, retrieval, citations) only sees sections and locators,
so new formats (OCR, e-mail, connector content) plug in by adding a parser to the registry.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class ParseError(Exception):
    """A permanent problem with the file itself (retrying will not help). `message` is safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Locator:
    """Where a section sits in its source file.

    * `kind`: "page" (PDF), "section" (DOCX), "sheet" (XLSX), "slide" (PPTX), "lines" (TXT), "rows" (CSV),
      "time" (timestamp range in a transcript), "video_frame" (sampled frame timestamp), or "image" (PNG/JPEG:
      the whole file; it has no pages).
    * `position`: 1-based ordinal of the top-level unit (page, section, sheet, slide, line/row block). It is
      stored in `document_chunks.page_number`, so code that only knows page numbers keeps working.
    * `label`: short human-readable form for citations, e.g. "Sheet 'Q1' A5:C40" or "00:02:10.000?00:02:35.000".
    * `attrs`: structured fields (sheet name, cell range, heading path, row/line range, slide title, ...).
    """

    kind: str
    position: int
    label: str
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "position": self.position, "label": self.label, **self.attrs}


def page_locator(page: int) -> Locator:
    return Locator("page", page, f"Page {page}", {"page": page})


def chunk_locator(data: dict | None, page_number: int) -> dict:
    """A stored chunk's locator. Chunks indexed before locators existed are PDF pages."""
    return data or page_locator(page_number).to_json()


@dataclass(frozen=True)
class Section:
    text: str
    locator: Locator


@dataclass(frozen=True)
class ExtractedImage:
    """A picture found in a document (a chart, diagram or screenshot), handed to the worker as PNG bytes."""

    page: int | None  # None: the document is the image itself (an uploaded PNG or JPEG)
    png: bytes
    caption: str = ""


@dataclass(frozen=True)
class ExtractedTable:
    """A table kept as rows and columns (Docling PDF/DOCX; see `app.rag.parsers.table_structure`), stored with the
    document's chunks so questions that need every row ("how many", "total") are computed, not read off a few chunks.

    `rows` are the data rows only: header rows, repeated headers and total rows ("Total | 12,345") are excluded.
    `row_positions[i]` is the page (PDF) or section ordinal (DOCX) of row i, so a table continued across a page
    break is one table with each row cited to its own page. `structured` is False when the header or shape could
    not be read reliably (`note` says why); such a table is still searchable but never used for calculations.
    """

    index: int  # 1-based within the document
    title: str  # the heading the table sits under, if any
    kind: str  # locator kind of its rows: "page" (PDF) or "section" (DOCX)
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    row_positions: tuple[int, ...]
    summary_rows: tuple[tuple[str, ...], ...] = ()
    structured: bool = True
    note: str = ""

    @property
    def position(self) -> int:
        return self.row_positions[0] if self.row_positions else 1


@dataclass(frozen=True)
class ParseLimits:
    max_units: int  # pages, slides or sheets
    max_chars: int  # extracted text
    max_unpacked_bytes: int  # total uncompressed size of an Office (ZIP) package
    section_chars: int  # target size of record-based sections (rows, lines); normally the chunk size


@dataclass(frozen=True)
class Inspection:
    """Result of the quick upload-time check: the format and, for paged formats, the unit count."""

    file_format: str
    unit_count: int | None


class Parser(Protocol):
    file_format: str  # stored in documents.file_format
    extensions: tuple[str, ...]  # accepted filename extensions, lowercase, with the dot

    def inspect(self, path: Path, limits: ParseLimits) -> int | None:
        """Validate quickly (structure, encryption, limits). Returns the page/slide/sheet count, if any."""
        ...

    def parse(self, path: Path, limits: ParseLimits) -> list[Section]:
        """Extract sections in document order. Raises `ParseError` for permanent problems."""
        ...


class TextBudget:
    """Enforces `max_chars` across a whole document while it is being extracted."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self.used = 0

    def take(self, text: str) -> str:
        self.used += len(text)
        if self.used > self._limit:
            raise ParseError("text_too_large", "The file contains more text than the processing limit allows.")
        return text


def require_text(sections: list[Section], hint: str = "") -> list[Section]:
    sections = [s for s in sections if s.text.strip()]
    if not sections:
        raise ParseError("no_text", f"No extractable text found.{hint}")
    return sections
