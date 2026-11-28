"""Slice 1 tests: NL -> structured report query parser (Step X).

Pure stdlib tests: no database, no network, no app imports beyond
app.finance.nl_query (which itself imports nothing from the app).
Run: python -m pytest tests/test_nl_reporting_parser.py -v --noconftest
with a minimal venv (pytest only).
"""

from datetime import date

import pytest

from app.finance.nl_query import UNPAID_STATUSES, parse_nl_report

# 2026-11-27 is a Friday; Monday of that week is 2026-11-23.
TODAY = date(2026, 11, 27)


def test_owner_example_last_month_unpaid():
    q = parse_nl_report("last month's invoices, unpaid ones", today=TODAY)
    assert q.intent == "invoices"
    assert q.date_from == date(2026, 10, 1)
    assert q.date_to == date(2026, 10, 31)
    assert q.statuses == UNPAID_STATUSES
    assert "rejected" not in q.statuses
    assert "paid" not in q.statuses


def test_roman_urdu_last_month_unpaid():
    q = parse_nl_report("pichle mahine ke unpaid invoices dikhao", today=TODAY)
    assert q.date_from == date(2026, 10, 1)
    assert q.date_to == date(2026, 10, 31)
    assert q.statuses == UNPAID_STATUSES


def test_this_month_roman_urdu():
    q = parse_nl_report("is mahine ke invoices", today=TODAY)
    # "this month" = the full current calendar month (like "last month").
    assert (q.date_from, q.date_to) == (date(2026, 11, 1), date(2026, 11, 30))


def test_this_week():
    q = parse_nl_report("is hafte ke invoices", today=TODAY)
    assert (q.date_from, q.date_to) == (date(2026, 11, 23), TODAY)


def test_last_week():
    q = parse_nl_report("pichla hafta invoices", today=TODAY)
    assert (q.date_from, q.date_to) == (date(2026, 11, 16), date(2026, 11, 22))


def test_today():
    q = parse_nl_report("aaj ke invoices", today=TODAY)
    assert (q.date_from, q.date_to) == (TODAY, TODAY)


def test_yesterday_roman_urdu_kal_warns():
    q = parse_nl_report("kal ke invoices", today=TODAY)
    assert (q.date_from, q.date_to) == (date(2026, 11, 26), date(2026, 11, 26))
    assert any("kal" in w for w in q.warnings)


def test_yesterday_english_no_warning():
    q = parse_nl_report("yesterday's invoices", today=TODAY)
    assert (q.date_from, q.date_to) == (date(2026, 11, 26), date(2026, 11, 26))
    assert not any("kal" in w for w in q.warnings)


def test_last_30_days():
    q = parse_nl_report("pichle 30 din ke invoices", today=TODAY)
    assert (q.date_from, q.date_to) == (date(2026, 10, 29), TODAY)


def test_this_year_spend_is_sum():
    q = parse_nl_report("is saal ka kharcha", today=TODAY)
    assert (q.date_from, q.date_to) == (date(2026, 1, 1), TODAY)
    assert q.aggregate == "sum"


def test_last_year():
    q = parse_nl_report("pichla saal invoices", today=TODAY)
    assert (q.date_from, q.date_to) == (date(2025, 1, 1), date(2025, 12, 31))


def test_named_month_with_year():
    q = parse_nl_report("november 2026 invoices", today=TODAY)
    assert (q.date_from, q.date_to) == (date(2026, 11, 1), date(2026, 11, 30))


def test_named_month_without_year_means_most_recent():
    q = parse_nl_report("december invoices", today=TODAY)
    assert (q.date_from, q.date_to) == (date(2025, 12, 1), date(2025, 12, 31))


def test_unpaid_excludes_rejected_and_paid():
    q = parse_nl_report("unpaid invoices", today=TODAY)
    assert set(q.statuses) == set(UNPAID_STATUSES)
    assert "rejected" not in q.statuses
    assert "paid" not in q.statuses


def test_paid():
    q = parse_nl_report("paid invoices last month", today=TODAY)
    assert q.statuses == ("paid",)


def test_approved():
    q = parse_nl_report("approved invoices this month", today=TODAY)
    assert q.statuses == ("approved",)


def test_pending():
    q = parse_nl_report("pending invoices", today=TODAY)
    assert q.statuses == ("pending",)


def test_rejected():
    q = parse_nl_report("rejected invoices last month", today=TODAY)
    assert q.statuses == ("rejected",)


def test_exception():
    q = parse_nl_report("invoices with exception", today=TODAY)
    assert q.statuses == ("exception",)


def test_count_kitne():
    q = parse_nl_report("kitne invoices hain last month", today=TODAY)
    assert q.aggregate == "count"


def test_sum_by_vendor():
    q = parse_nl_report("last month spend by vendor", today=TODAY)
    assert q.aggregate == "sum_by_vendor"


def test_sum_by_department_roman_urdu():
    q = parse_nl_report("kharcha department ke hisab se", today=TODAY)
    assert q.aggregate == "sum_by_department"


def test_vendor_capture():
    q = parse_nl_report("invoices from Acme last month", today=TODAY)
    assert q.vendor == "acme"


def test_currency():
    q = parse_nl_report("unpaid invoices in USD", today=TODAY)
    assert q.currency == "USD"


def test_unknown_intent():
    q = parse_nl_report("hello, how are you?", today=TODAY)
    assert q.intent == "unknown"
    assert q.date_from is None and q.statuses == ()


def test_empty_string_is_unknown():
    assert parse_nl_report("", today=TODAY).intent == "unknown"


def test_no_date_warns_but_still_valid():
    q = parse_nl_report("unpaid invoices", today=TODAY)
    assert q.intent == "invoices"
    assert q.date_from is None
    assert any("date" in w for w in q.warnings)


def test_mixed_roman_urdu_total():
    q = parse_nl_report("pichle mahine ke unpaid invoices ka total kitna hai", today=TODAY)
    assert (q.date_from, q.date_to) == (date(2026, 10, 1), date(2026, 10, 31))
    assert q.statuses == UNPAID_STATUSES
    assert q.aggregate == "sum"


def test_last_month_from_march_is_february():
    q = parse_nl_report("last month invoices", today=date(2026, 3, 15))
    assert (q.date_from, q.date_to) == (date(2026, 2, 1), date(2026, 2, 28))


def test_last_month_from_january_is_last_december():
    q = parse_nl_report("last month invoices", today=date(2026, 1, 5))
    assert (q.date_from, q.date_to) == (date(2025, 12, 1), date(2025, 12, 31))
