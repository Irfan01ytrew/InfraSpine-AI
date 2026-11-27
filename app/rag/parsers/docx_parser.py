"""Word documents (.docx). Body paragraphs and tables in reading order, split into sections at headings.

Each section starts with its heading path ("Policies > Remote access"), so a chunk keeps its context, and
its citation names the heading. Text before the first heading is the "Document start" section.
"""

import re
from pathlib import Path

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.rag.parsers.base import Locator, ParseError, ParseLimits, Section, TextBudget, require_text

_HEADING = re.compile(r"^heading\s*(\d)$", re.IGNORECASE)


def _open(path: Path):
    try:
        return docx.Document(str(path))
    except Exception:
        raise ParseError("invalid_file", "The Word document could not be read. It may be damaged.") from None


def _heading_level(paragraph: Paragraph) -> int | None:
    name = (paragraph.style.name if paragraph.style is not None else "") or ""
    if name.lower() == "title":
        return 1
    match = _HEADING.match(name.strip())
    return int(match.group(1)) if match else None


def _table_text(table: Table) -> str:
    lines = []
    for row in table.rows:
        cells: list[str] = []
        for cell in row.cells:
            text = " ".join(cell.text.split())
            if text and (not cells or cells[-1] != text):  # merged cells repeat across the grid
                cells.append(text)
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


class DocxParser:
    file_format = "docx"
    extensions = (".docx",)

    def inspect(self, path: Path, limits: ParseLimits) -> int | None:
        _open(path)
        return None

    def parse(self, path: Path, limits: ParseLimits) -> list[Section]:
        document = _open(path)
        budget = TextBudget(limits.max_chars)
        sections: list[Section] = []
        path_stack: list[tuple[int, str]] = []  # (level, heading text)
        body: list[str] = []

        def flush() -> None:
            text = "\n".join(body).strip()
            headings = [h for _, h in path_stack]
            if not text and not headings:
                return
            label = " > ".join(headings) if headings else "Document start"
            heading_line = f"Section: {label}\n" if headings else ""
            sections.append(
                Section(
                    heading_line + text,
                    Locator("section", len(sections) + 1, label, {"heading_path": headings, "section": len(sections) + 1}),
                )
            )

        try:
            for block in document.iter_inner_content():
                if isinstance(block, Paragraph):
                    text = budget.take(block.text.strip())
                    level = _heading_level(block)
                    if level is not None and text:
                        if body:
                            flush()
                        body = []
                        path_stack = [(lvl, h) for lvl, h in path_stack if lvl < level] + [(level, " ".join(text.split()))]
                        continue
                    if text:
                        body.append(text)
                elif isinstance(block, Table):
                    text = budget.take(_table_text(block))
                    if text:
                        body.append(text)
        except ParseError:
            raise
        except Exception:
            raise ParseError("invalid_file", "The Word document could not be read. It may be damaged.") from None
        flush()
        return require_text(sections)
