"""Analytics questions over spreadsheets: table detection, the validated query, exact computation, and the
chat path that uses the computed result as the answer's only source."""

import datetime as dt
import io

import openpyxl
import pytest

from app.rag import tables
from tests.conftest import USER_EMAIL, login
from tests.rag_helpers import activate_provider, ask, process_all, upload

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _workbook() -> bytes:
    book = openpyxl.Workbook()
    summary = book.active
    summary.title = "Summary"
    summary.append(["Access audit report"])  # a title row is not a header
    summary.append([])
    summary.append(["Action type", "Entries", None, "Module", "Entries"])  # two tables side by side
    summary.append(["Report Viewed", 30, None, "Account", 30])
    summary.append(["Inquiry", 12, None, "Developer", 12])
    logs = book.create_sheet("Detailed Logs")
    logs.append(["Log date", "Report name", "IP", "Amount"])
    rows = [
        (dt.datetime(2026, 9, 8, 10), "Form-1", "10.0.0.1", 100),
        (dt.datetime(2026, 9, 8, 11), "Form-1", "10.0.0.1", 50),
        (dt.datetime(2026, 9, 8, 15), "Ledger", "10.0.0.2", 25),
        (dt.datetime(2026, 9, 9, 9), "Form-1", "10.0.0.1", 10),
        (dt.datetime(2026, 9, 10, 9), "Cash book", "10.0.0.1", "1,000"),
    ]
    for row in rows:
        logs.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


@pytest.fixture
def workbook_tables(tmp_path):
    path = tmp_path / "audit.xlsx"
    path.write_bytes(_workbook())
    return tables.read_tables(path, "xlsx")


def _query(found, **spec):
    import json

    return tables.parse_query(json.dumps(spec), found)


def test_headers_and_side_by_side_tables_are_found(workbook_tables):
    shapes = [(t.sheet, t.columns, len(t.rows)) for t in workbook_tables]
    assert shapes == [
        ("Summary", ("Action type", "Entries"), 2),
        ("Summary", ("Module", "Entries"), 2),
        ("Detailed Logs", ("Log date", "Report name", "IP", "Amount"), 5),
    ]


def test_counts_sums_shares_and_date_groups_are_exact(workbook_tables):
    logs = workbook_tables[2].id
    header, rows, matched, groups, total = tables.run_query(
        _query(workbook_tables, table=logs, operation="count", group_by=["Report name"], limit=2)
    )
    assert header == ["Report name", "Row count", "Share of total"]
    assert rows == [["Form-1", "3", "60.0%"], ["Ledger", "1", "20.0%"]] and (matched, groups, total) == (5, 3, 5)

    _, by_amount, _, _, _ = tables.run_query(_query(workbook_tables, table=logs, operation="sum", value="Amount", group_by=["Report name"]))
    assert by_amount[0][:2] == ["Cash book", "1000"]  # "1,000" read as a number

    _, per_day, _, _, _ = tables.run_query(_query(workbook_tables, table=logs, operation="count", group_by=["Log date:day"]))
    assert per_day[0][:2] == ["2026-09-08", "3"]

    _, filtered, matched, _, _ = tables.run_query(
        _query(workbook_tables, table=logs, operation="count", group_by=["IP"],
               filters=[{"column": "Report name", "op": "eq", "value": "form-1"}, {"column": "Amount", "op": "gte", "value": 50}])
    )
    assert matched == 2 and filtered[0][:2] == ["10.0.0.1", "2"]


def test_queries_that_do_not_fit_the_tables_are_rejected(workbook_tables):
    logs = workbook_tables[2].id
    assert tables.parse_query("NONE", workbook_tables) is None
    for bad in (
        {"table": "T99", "operation": "count"},
        {"table": logs, "operation": "drop"},
        {"table": logs, "operation": "sum", "value": "Salary"},
        {"table": logs, "operation": "count", "group_by": ["Password"]},
        {"table": logs, "operation": "count", "filters": [{"column": "IP", "op": "regex", "value": ".*"}]},
    ):
        with pytest.raises(tables.QueryError):
            _query(workbook_tables, **bad)
    with pytest.raises(tables.QueryError):
        tables.parse_query("import os; os.remove('x')", workbook_tables)
    assert _query(workbook_tables, table=logs, operation="count", limit=9999).limit == 50


def test_csv_tables_are_read(tmp_path):
    path = tmp_path / "hosts.csv"
    path.write_text("Host,Owner,Patches\ndb01,platform,3\nweb01,web,5\ndb02,platform,4\n", encoding="utf-8")
    found = tables.read_tables(path, "csv")
    _, rows, _, _, _ = tables.run_query(_query(found, table=found[0].id, operation="sum", value="Patches", group_by=["Owner"]))
    assert rows[0][:2] == ["platform", "7"]


def test_analytics_questions_are_detected():
    for question in ("check which access she most", "kis din sab se zyada logs the?", "how many entries per day", "top 5 reports"):
        assert tables.wants_table_analysis(question), question
    assert not tables.wants_table_analysis("what is the refund policy?")


def test_chat_answers_an_analytics_question_from_every_row(client, fake_provider):
    activate_provider(client)
    login(client, USER_EMAIL)
    doc = upload(client, "audit.xlsx", _workbook(), content_type=XLSX).json()
    process_all(client)

    def reply(message: str) -> str:
        if "TABLES in audit.xlsx" in message:
            table = next(line.split(" ")[0] for line in message.splitlines() if "Detailed Logs" in line)
            return f'{{"table": "{table}", "operation": "count", "group_by": ["Report name"], "limit": 3}}'
        return "She opened **Form-1** most: 3 times, 60.0% of her activity [S1]."

    fake_provider.rag_reply = reply
    body = ask(client, "which report did she open most?", document_ids=[doc["id"]]).json()
    assert body["strategy"] == "table_analysis" and body["insufficient_evidence"] is False
    assert "Form-1" in body["answer"] and body["citations"][0]["location"].startswith("Sheet 'Detailed Logs', computed")

    # A wording with a number the calculation does not contain is not shown; the calculation itself is.
    fake_provider.rag_reply = lambda m: reply(m) if "TABLES in" in m else "She opened Form-1 most, 97 times [S1]."
    fallback = ask(client, "which report did she open most?", document_ids=[doc["id"]]).json()
    assert "97" not in fallback["answer"] and "| Form-1 | 3 | 60.0% |" in fallback["answer"]
    assert fallback["insufficient_evidence"] is False and fallback["citations"]


def test_a_counting_question_about_another_document_does_not_query_a_spreadsheet(client, fake_provider):
    from tests.rag_helpers import make_pdf

    activate_provider(client)
    login(client, USER_EMAIL)
    upload(client, "audit.xlsx", _workbook(), content_type=XLSX)
    upload(client, "contract.pdf", make_pdf("Vendor contract: the notice period is 60 days for either party."))
    process_all(client)
    fake_provider.rag_reply = lambda m: "The notice period is 60 days [S1]."
    body = ask(client, "How many days is the vendor contract notice period?").json()
    assert body["strategy"] != "table_analysis" and "60 days" in body["answer"]
    assert all(prompt != tables.QUERY_PROMPT for prompt, _ in fake_provider.calls)  # no table query was asked
