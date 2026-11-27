"""Tables as rows and columns.

Docling returns each table as a grid of cells (with header flags and column spans). This module turns those grids
into logical tables and into retrieval sections:

* Header rows are the leading rows Docling flags as column headers; without flags, a first row of text labels is
  taken as the header. A merged header cell whose words name the columns it spans ("ID Name" over ID and Name) is
  split. A table whose header cannot be determined is kept as text but marked unstructured: it is never counted.
* Data rows exclude repeated header rows (a header printed again after a page break) and total rows
  ("Total | 12,345/-"), so counts and sums are not inflated. A spanning data cell is kept once, in its first column.
* A table continued on the next page (adjacent in reading order, same columns, no header or the same header) is
  merged into one table; each row keeps its own page.
* Sections are blocks of whole rows, each repeating the table's title and column names, so no record is cut in
  two or repeated by chunk overlap (each block fits one chunk).

Pure Python (no Docling import): the rules are tested directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.rag.parsers.base import ExtractedTable, Locator
from app.rag.parsers.text_parser import record_line

_TOTAL = re.compile(r"(grand\s+|sub-?\s*)?totals?\s*:?", re.IGNORECASE)
_NUMBER = re.compile(r"[\s(+\-]*(rs\.?|pkr|\$)?\s*[\d.,]+\s*%?\s*(/-)?[)\s]*", re.IGNORECASE)
MAX_TABLE_ROWS = 50_000  # per document; larger extractions are kept as text only


@dataclass(frozen=True)
class GridCell:
    text: str
    header: bool = False
    start_col: int = 0  # first column the cell covers (a spanning cell repeats in every column it covers)


@dataclass
class Fragment:
    """One table as the extractor found it. `position` is its page (PDF) or section ordinal (DOCX); `continues`
    is True when nothing but page furniture separates it from the previous table (it may be that table's
    continuation on the next page)."""

    position: int
    grid: list[list[GridCell]]
    title: str = ""
    continues: bool = False
    # Set by `build_tables`: the table this fragment belongs to, the data rows it contributed, and the
    # 1-based number (within that table) of its first row.
    table: ExtractedTable | None = None
    rows: list[tuple[str, ...]] = field(default_factory=list)
    first_row: int = 1


def _plain(text: str | None) -> str:
    return " ".join((text or "").split())


def _width(grid: list[list[GridCell]]) -> int:
    return max((len(row) for row in grid), default=0)


def _flagged_headers(grid: list[list[GridCell]]) -> int:
    count = 0
    for row in grid:
        filled = [cell for cell in row if _plain(cell.text)]
        if not filled or not all(cell.header for cell in filled):
            break
        count += 1
    return count


def _distinct(row: list[GridCell]) -> list[GridCell]:
    return [cell for column, cell in enumerate(row) if cell.start_col == column]


def _header_rows(grid: list[list[GridCell]]) -> tuple[int, str]:
    """(number of header rows, note). 0 means no reliable header."""
    flagged = _flagged_headers(grid)
    if 0 < flagged < len(grid):
        return flagged, ""
    if flagged == len(grid):
        return 0, "only header rows"
    labels = [_plain(cell.text) for cell in _distinct(grid[0])] if grid else []
    if len(grid) > 1 and len(labels) >= 2 and all(labels) and not any(_NUMBER.fullmatch(text) for text in labels):
        return 1, ""
    return 0, "no header row"


def _columns(grid: list[list[GridCell]], header_rows: int, width: int) -> tuple[str, ...]:
    names = []
    for column in range(width):
        parts: list[str] = []
        for row in grid[:header_rows]:
            text = _plain(row[column].text) if column < len(row) else ""
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        names.append(" ".join(parts))
    if header_rows == 1:  # one merged header cell whose words name each column it spans
        row = grid[0]
        for column, cell in enumerate(row):
            if cell.start_col != column:
                continue
            span = 1
            while column + span < len(row) and row[column + span].start_col == column:
                span += 1
            words = _plain(cell.text).split()
            if span > 1 and len(words) == span:
                names[column : column + span] = words
    named = [name or f"Column {i + 1}" for i, name in enumerate(names)]
    seen: dict[str, int] = {}
    unique = []
    for name in named:
        seen[name] = seen.get(name, 0) + 1
        unique.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    return tuple(unique)


def _values(row: list[GridCell], width: int) -> tuple[str, ...]:
    return tuple(
        _plain(row[column].text) if column < len(row) and row[column].start_col == column else ""
        for column in range(width)
    )


def _looks_like_data(row: list[GridCell], group: _Group, width: int) -> bool:
    """Whether a row flagged as a header is really a data row of `group` (Docling often flags the first row of a
    continued table as its header): it has a number where the group's header has a label and its rows have
    numbers."""
    values = _values(row, width)
    for column, value in enumerate(values):
        if not value or not _NUMBER.fullmatch(value) or _NUMBER.fullmatch(group.columns[column]):
            continue
        filled = [r[column] for r in group.rows if r[column]]
        if filled and sum(1 for v in filled if _NUMBER.fullmatch(v)) >= 0.8 * len(filled):
            return True
    return False


def _title_is_misread_first_row(fragment: Fragment, grid: list[list[GridCell]], group: _Group, width: int) -> bool:
    """Docling can label the first row of a continued table as a section heading.

    Only override the explicit continuation marker when that heading starts with the first two
    cells of the adjacent page's first row and the row matches the previous table's data shape.
    """
    if not fragment.title or not grid:
        return False
    values = _values(grid[0], width)
    row_prefix = _plain(" ".join(value for value in values[:2] if value)).casefold()
    title = _plain(fragment.title).casefold()
    return bool(row_prefix and title.startswith(row_prefix) and _looks_like_data(grid[0], group, width))


def _is_total(values: tuple[str, ...]) -> bool:
    first = next((value for value in values if value), "")
    return bool(_TOTAL.fullmatch(first))


@dataclass
class _Group:
    title: str
    columns: tuple[str, ...]
    structured: bool
    note: str
    rows: list[tuple[str, ...]] = field(default_factory=list)
    positions: list[int] = field(default_factory=list)
    summary: list[tuple[str, ...]] = field(default_factory=list)


def build_tables(fragments: list[Fragment], kind: str, start_index: int = 1) -> list[ExtractedTable]:
    """Logical tables from the fragments in reading order (see the module docstring). Sets each fragment's
    `rows` to the data rows it contributed, so its sections can be placed where it appeared."""
    groups: list[_Group] = []
    owners: list[int] = []  # the group of each fragment
    total_rows = 0
    for fragment in fragments:
        grid = [row for row in fragment.grid if any(_plain(cell.text) for cell in row)]
        width = _width(grid)
        fragment.rows = []
        if not grid or width == 0:
            owners.append(-1)
            continue
        flagged = _flagged_headers(grid)
        previous = groups[-1] if groups else None
        title_is_row = previous is not None and _title_is_misread_first_row(fragment, grid, previous, width)
        continued = (
            previous is not None
            and kind == "page"
            and (fragment.continues or title_is_row)
            and previous.structured
            and len(previous.columns) == width
            and bool(previous.positions)
            and fragment.position in (previous.positions[-1], previous.positions[-1] + 1)
        )
        if continued and flagged and _columns(grid, flagged, width) != previous.columns and _looks_like_data(grid[0], previous, width):
            flagged = 0  # the "header" is the continuation's first data row
        if continued and (flagged == 0 or _columns(grid, flagged, width) == previous.columns):
            group, data = previous, grid[flagged:]  # the table continues after a page break
        else:
            header_rows, note = _header_rows(grid)
            structured = header_rows > 0 and width >= 2
            if header_rows and width < 2:
                note = "a single column"
            columns = _columns(grid, header_rows, width) if header_rows else tuple(f"Column {i + 1}" for i in range(width))
            group = _Group(fragment.title, columns, structured, note)
            groups.append(group)
            data = grid[header_rows:]
        owners.append(groups.index(group))
        fragment.first_row = len(group.rows) + 1
        names = tuple(name.casefold() for name in group.columns)
        for row in data:
            values = _values(row, width)
            if not any(values):
                continue
            if group.structured and tuple(value.casefold() for value in values) == names:
                continue  # the header printed again (for example after a page break)
            if group.structured and _is_total(values):
                group.summary.append(values)
                continue
            group.rows.append(values)
            group.positions.append(fragment.position)
            fragment.rows.append(values)
            total_rows += 1
    if total_rows > MAX_TABLE_ROWS:  # too large to keep as rows: text only, never computed over a cut-off copy
        for group in groups:
            group.structured, group.note = False, "too many rows"
    tables = []
    for number, group in enumerate(groups, start=start_index):
        if not group.rows:
            group.structured, group.note = False, group.note or "no data rows"
        tables.append(
            ExtractedTable(
                index=number,
                title=group.title,
                kind=kind,
                columns=group.columns,
                rows=tuple(group.rows) if total_rows <= MAX_TABLE_ROWS else (),
                row_positions=tuple(group.positions) if total_rows <= MAX_TABLE_ROWS else (),
                summary_rows=tuple(group.summary),
                structured=group.structured,
                note=group.note,
            )
        )
    for fragment, owner in zip(fragments, owners, strict=True):
        fragment.table = tables[owner] if owner >= 0 else None
    return tables


def grid_text(fragment: Fragment) -> str:
    """The fragment's cells as plain lines ("a | b | c"), for a table that has no data rows to section."""
    lines = []
    for row in fragment.grid:
        cells = [_plain(cell.text) for cell in _distinct(row)]
        if any(cells):
            lines.append(" | ".join(cell for cell in cells if cell))
    return "\n".join(lines)


@dataclass(frozen=True)
class TableBlock:
    position: int
    first_row: int  # 1-based data row numbers within the table
    last_row: int
    text: str


def table_blocks(fragment: Fragment, room: int) -> list[TableBlock]:
    """Sections for the rows `fragment` contributed to its table: blocks of whole rows that each repeat the
    table's title and column names and fit in `room` characters, so a chunk holds complete records and no record
    is repeated in two chunks. The fragment holding the table's last row also carries its total rows."""
    table = fragment.table
    if table is None or not fragment.rows:
        return []
    title = f"{table.title} · " if table.title else ""
    columns = ", ".join(table.columns)
    of = f" of {len(table.rows)}" if table.structured and table.rows else ""
    shape = "" if table.structured else " (column headers unclear)"

    def heading(a: int, b: int) -> str:
        rows = f"Row {a}" if a == b else f"Rows {a}–{b}"
        return f"{title}Table {table.index} · {rows}{of}{shape} (columns: {columns})"

    numbered = [
        (fragment.first_row + i, record_line(f"Row {fragment.first_row + i}", list(table.columns), list(values)))
        for i, values in enumerate(fragment.rows)
    ]
    last = fragment.first_row + len(fragment.rows) - 1
    if table.summary_rows and last == len(table.rows):
        numbered += [(None, record_line("Total row", list(table.columns), list(values))) for values in table.summary_rows]
    budget = max(room - len(heading(last, last)) - 1, room // 2)
    blocks: list[TableBlock] = []
    lines: list[str] = []
    first = end = fragment.first_row
    size = 0

    def flush() -> None:
        blocks.append(TableBlock(fragment.position, first, end, heading(first, end) + "\n" + "\n".join(lines)))

    for number, line in numbered:
        if lines and number is not None and size + len(line) + 1 > budget:
            flush()
            lines, size, first = [], 0, number
        lines.append(line)
        size += len(line) + 1
        end = number if number is not None else end
    if lines:
        flush()
    return blocks


def page_table_locator(block: TableBlock, table: ExtractedTable) -> Locator:
    rows = f"row {block.first_row}" if block.first_row == block.last_row else f"rows {block.first_row}–{block.last_row}"
    return Locator(
        "page", block.position, f"Page {block.position} · Table {table.index}, {rows}",
        {"page": block.position, "table": table.index, "rows": [block.first_row, block.last_row]},
    )
