"""Excel workbooks (.xlsx), read in streaming mode with cached formula values.

Each section is a block of whole rows from one sheet. It names the sheet and lists the header columns,
and every value keeps its column name and cell reference ("Units (C7): 42"), so a chunk is meaningful
on its own and its citation points at an exact sheet and cell range.
"""

import datetime as dt
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter

from app.rag.parsers.base import Locator, ParseError, ParseLimits, Section, TextBudget, require_text


def _open(path: Path):
    try:
        return openpyxl.load_workbook(path, read_only=True, data_only=True, keep_links=False)
    except Exception:
        raise ParseError("invalid_file", "The workbook could not be read. It may be damaged.") from None


def format_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time() else value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    return " ".join(str(value).split())


def cell_range(first_col: int, first_row: int, last_col: int, last_row: int) -> str:
    start, end = f"{get_column_letter(first_col)}{first_row}", f"{get_column_letter(last_col)}{last_row}"
    return start if start == end else f"{start}:{end}"


class XlsxParser:
    file_format = "xlsx"
    extensions = (".xlsx",)

    def inspect(self, path: Path, limits: ParseLimits) -> int | None:
        workbook = _open(path)
        try:
            count = len(workbook.worksheets)
        finally:
            workbook.close()
        if count > limits.max_units:
            raise ParseError("too_many_sheets", f"The workbook has {count} sheets; the limit is {limits.max_units}.")
        return count

    def parse(self, path: Path, limits: ParseLimits) -> list[Section]:
        workbook = _open(path)
        try:
            if len(workbook.worksheets) > limits.max_units:
                raise ParseError("too_many_sheets", f"The workbook has more than {limits.max_units} sheets.")
            budget = TextBudget(limits.max_chars * 3)  # cell references and column names add to the raw text
            cells_seen = 0
            sections: list[Section] = []
            for sheet_index, sheet in enumerate(workbook.worksheets, start=1):
                rows: list[tuple[int, list[tuple[int, str]]]] = []
                for row in sheet.iter_rows():
                    cells_seen += len(row)
                    if cells_seen > limits.max_chars:
                        raise ParseError("text_too_large", "The workbook has more cells than the processing limit allows.")
                    cells = [(c.row, c.column, format_value(c.value)) for c in row if getattr(c, "value", None) is not None]
                    cells = [cell for cell in cells if cell[2]]
                    if cells:  # empty cells in streaming mode carry no coordinates; filled ones do
                        rows.append((cells[0][0], [(column, text) for _, column, text in cells]))
                sections.extend(self._sheet_sections(sheet.title, sheet_index, rows, limits, budget))
        except ParseError:
            raise
        except Exception:
            raise ParseError("invalid_file", "The workbook could not be read. It may be damaged.") from None
        finally:
            workbook.close()
        return require_text(sections)

    @staticmethod
    def _sheet_sections(name, sheet_index, rows, limits, budget) -> list[Section]:
        if not rows:
            return []
        header_row, header_cells = rows[0]
        has_header = len(rows) > 1 and all(not _is_number(text) for _, text in header_cells)
        headers = {column: text for column, text in header_cells} if has_header else {}
        body = rows[1:] if has_header else rows
        columns = ", ".join(f"{get_column_letter(c)} {h}" for c, h in headers.items())
        intro = f"Sheet '{name}'" + (f" (columns: {columns})" if columns else "")
        room = max(limits.section_chars - len(intro) - 20, limits.section_chars // 2)

        sections: list[Section] = []
        block: list[str] = []
        block_rows: list[tuple[int, list[tuple[int, str]]]] = []
        size = 0

        def flush() -> None:
            if not block:
                return
            first_row, last_row = block_rows[0][0], block_rows[-1][0]
            cols = [c for _, cells in block_rows for c, _ in cells]
            ref = cell_range(min(cols), first_row, max(cols), last_row)
            text = budget.take(f"{intro}, rows {first_row}–{last_row}\n" + "\n".join(block))
            sections.append(
                Section(
                    text,
                    Locator(
                        "sheet", sheet_index, f"Sheet '{name}' {ref}",
                        {"sheet": name, "sheet_index": sheet_index, "range": ref, "start_row": first_row, "end_row": last_row},
                    ),
                )
            )

        for row_number, cells in body or [rows[0]]:
            parts = []
            for column, text in cells:
                ref = f"{get_column_letter(column)}{row_number}"
                parts.append(f"{headers[column]} ({ref}): {text}" if column in headers else f"{ref}: {text}")
            line = f"Row {row_number}: " + "; ".join(parts)
            if block and size + len(line) + 1 > room:
                flush()
                block, block_rows, size = [], [], 0
            block.append(line)
            block_rows.append((row_number, cells))
            size += len(line) + 1
        flush()
        return sections


def _is_number(text: str) -> bool:
    try:
        float(text.replace(",", ""))
    except ValueError:
        return False
    return True
