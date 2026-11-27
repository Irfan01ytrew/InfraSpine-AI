"""Table structure rules (no Docling): headers, data and total rows, page-break continuation, and whole-row
retrieval blocks that repeat the column names without repeating a record."""

import re

from app.rag.parsers import table_structure
from app.rag.parsers.table_structure import Fragment, GridCell, build_tables, table_blocks


def grid(*rows: list[str], header_rows: int = 1, spans: dict[tuple[int, int], int] | None = None) -> list[list[GridCell]]:
    """Cells of a Docling-like grid. `spans[(row, col)] = n`: that cell spans n columns (repeated, as Docling does)."""
    spans = spans or {}
    out = []
    for r, row in enumerate(rows):
        cells: list[GridCell] = []
        for c, text in enumerate(row):
            start = next((s for (rr, s), n in spans.items() if rr == r and s < c < s + n), c)
            cells.append(GridCell(row[start], r < header_rows, start))
        out.append(cells)
    return out


def test_header_total_and_repeated_header_rows_are_not_data():
    fragment = Fragment(1, grid(
        ["Sr", "Description", "Amount"],
        ["1", "Internet link A", "32,982/-"],
        ["Sr", "Description", "Amount"],  # printed again
        ["2", "Internet link B", "26,288/-"],
        ["Total", "", "59,270/-"],
    ))
    [table] = build_tables([fragment], "page")
    assert table.structured and table.columns == ("Sr", "Description", "Amount")
    assert table.rows == (("1", "Internet link A", "32,982/-"), ("2", "Internet link B", "26,288/-"))
    assert table.summary_rows == (("Total", "", "59,270/-"),) and table.row_positions == (1, 1)


def test_a_merged_header_naming_the_columns_it_spans_is_split():
    fragment = Fragment(1, grid(["ID Name", "ID Name", "City"], ["156", "Lead A", "Lahore"], spans={(0, 0): 2}))
    [table] = build_tables([fragment], "page")
    assert table.columns == ("ID", "Name", "City") and table.rows == (("156", "Lead A", "Lahore"),)


def test_a_spanning_data_cell_is_kept_once():
    fragment = Fragment(1, grid(["Item", "Q1", "Q2"], ["Note: closed", "Note: closed", "Note: closed"], ["Fees", "10", "12"],
                                spans={(1, 0): 3}))
    [table] = build_tables([fragment], "page")
    assert table.rows[0] == ("Note: closed", "", "")


def test_a_header_is_inferred_from_labels_and_a_table_without_one_is_never_structured():
    [inferred] = build_tables([Fragment(1, grid(["Host", "Owner"], ["db01", "platform"], header_rows=0))], "page")
    assert inferred.structured and inferred.columns == ("Host", "Owner") and len(inferred.rows) == 1

    [unclear] = build_tables([Fragment(1, grid(["12", "4,000"], ["13", "5,000"], header_rows=0))], "page")
    assert not unclear.structured and unclear.note == "no header row" and len(unclear.rows) == 2
    fragment = Fragment(1, grid(["12", "4,000"], ["13", "5,000"], header_rows=0))
    build_tables([fragment], "page")
    [block] = table_blocks(fragment, 800)
    assert "(column headers unclear)" in block.text and " of " not in block.text.splitlines()[0]


def test_a_table_continued_on_the_next_page_is_one_table():
    first = Fragment(1, grid(["ID", "Name"], ["1", "A"], ["2", "B"]), "Leads")
    without_header = Fragment(2, grid(["3", "C"], ["4", "D"], header_rows=0), continues=True)
    [table] = build_tables([first, without_header], "page")
    assert table.rows == (("1", "A"), ("2", "B"), ("3", "C"), ("4", "D")) and table.row_positions == (1, 1, 2, 2)
    assert (without_header.table, without_header.first_row) == (table, 3)

    repeated_header = Fragment(2, grid(["ID", "Name"], ["3", "C"]), continues=True)
    [table] = build_tables([Fragment(1, grid(["ID", "Name"], ["1", "A"])), repeated_header], "page")
    assert len(table.rows) == 2

    flagged_data = Fragment(2, grid(["3", "C"], ["4", "D"]), continues=True)  # Docling flags the first row
    [table] = build_tables([Fragment(1, grid(["ID", "Name"], ["1", "A"], ["2", "B"])), flagged_data], "page")
    assert [row[0] for row in table.rows] == ["1", "2", "3", "4"]

    # Docling sometimes mislabels the continuation's first data row as a section heading,
    # which clears the ordinary continuation marker in the PDF reader.
    misread_heading = Fragment(2, grid(["3", "C"], ["4", "D"], header_rows=0), "3 C", continues=False)
    [table] = build_tables([Fragment(1, grid(["ID", "Name"], ["1", "A"], ["2", "B"])), misread_heading], "page")
    assert [row[0] for row in table.rows] == ["1", "2", "3", "4"] and misread_heading.first_row == 3


def test_separate_tables_are_not_merged():
    one = Fragment(1, grid(["ID", "Name"], ["1", "A"]))
    other_columns = Fragment(2, grid(["Code", "City", "Zone"], ["x", "y", "z"]), continues=True)
    text_between = Fragment(2, grid(["ID", "Name"], ["2", "B"]), continues=False)
    far_page = Fragment(5, grid(["5", "E"], header_rows=0), continues=True)
    tables = build_tables([one, other_columns, text_between, far_page], "page")
    assert [len(t.rows) for t in tables] == [1, 1, 1, 1] and [t.index for t in tables] == [1, 2, 3, 4]
    docx = build_tables([Fragment(1, grid(["ID", "Name"], ["1", "A"])), Fragment(2, grid(["2", "B"], header_rows=0), continues=True)], "section")
    assert len(docx) == 2  # DOCX tables are never continuations


def test_blocks_hold_whole_rows_once_and_repeat_the_columns():
    rows = [[str(100 + n), f"Lead {n}", "0300-0000000", "Facebook", "Project Alpha", "active", "Lahore"] for n in range(40)]
    fragment = Fragment(1, grid(["ID", "Name", "Phone", "Source", "Project", "Status", "City"], *rows, ["Total", "", "", "", "", "", ""]), "Leads")
    [table] = build_tables([fragment], "page")
    blocks = table_blocks(fragment, 800)
    assert len(blocks) > 3 and all(len(b.text) <= 800 for b in blocks)
    numbered = [int(n) for b in blocks for n in re.findall(r"^Row (\d+): ", b.text, re.M)]
    assert numbered == list(range(1, 41))  # every record exactly once, in order
    assert all(b.text.startswith(f"Leads · Table 1 · Rows {b.first_row}–{b.last_row} of 40 (columns: ID, Name,") for b in blocks)
    assert [(b.first_row, b.last_row) for b in blocks][0][0] == 1 and blocks[-1].last_row == 40
    assert "Total row: ID: Total" in blocks[-1].text and table.summary_rows


def test_an_oversized_extraction_is_kept_as_text_only(monkeypatch):
    monkeypatch.setattr(table_structure, "MAX_TABLE_ROWS", 3)
    fragment = Fragment(1, grid(["ID", "Name"], *[[str(n), "x"] for n in range(5)]))
    [table] = build_tables([fragment], "page")
    assert not table.structured and table.note == "too many rows" and table.rows == ()
    assert len(fragment.rows) == 5 and table_blocks(fragment, 800)  # still searchable as text
