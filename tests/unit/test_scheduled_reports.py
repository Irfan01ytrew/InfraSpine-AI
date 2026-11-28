"""Step AA slice 1: schedule model + daily cash summary builder (pure functions).

No database, no network, no sending — everything here is deterministic and
testable without fixtures.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.reports.scheduled import (
    DEFAULT_OVERDUE_DAYS,
    InvoiceRow,
    PaymentRow,
    ReportSchedule,
    build_daily_cash_summary,
    build_overdue_alert,
    build_weekly_invoices_summary,
    is_quiet_hour,
    next_due,
    render_daily_cash_summary_text,
    render_overdue_alert_text,
    render_weekly_invoices_text,
    should_send,
    validate_schedule,
)

PKT = ZoneInfo("Asia/Karachi")
UTC = ZoneInfo("UTC")


def _schedule(**overrides):
    base = {
        "template": "daily_cash_summary",
        "cadence": "daily",
        "time_of_day": time(8, 0),
        "timezone": "Asia/Karachi",
        "recipients": ("owner",),
    }
    base.update(overrides)
    return ReportSchedule(**base)


def _dt(year, month, day, hour, minute=0, tz=PKT):
    return datetime(year, month, day, hour, minute, tzinfo=tz)


# --- next_due ---------------------------------------------------------------

def test_next_due_daily_before_time_today():
    assert next_due(_schedule(), _dt(2026, 11, 28, 7, 0)) == _dt(2026, 11, 28, 8, 0)


def test_next_due_daily_after_time_tomorrow():
    assert next_due(_schedule(), _dt(2026, 11, 28, 9, 0)) == _dt(2026, 11, 29, 8, 0)


def test_next_due_daily_exactly_at_time_is_tomorrow():
    assert next_due(_schedule(), _dt(2026, 11, 28, 8, 0)) == _dt(2026, 11, 29, 8, 0)


def test_next_due_weekly_next_matching_weekday():
    # 2026-11-28 is a Saturday; next Monday (weekday 0) is 2026-11-30.
    weekly = _schedule(cadence="weekly", weekday=0)
    assert next_due(weekly, _dt(2026, 11, 28, 9, 0)) == _dt(2026, 11, 30, 8, 0)


def test_next_due_weekly_same_day_after_time_is_next_week():
    weekly = _schedule(cadence="weekly", weekday=5)  # Saturday
    assert next_due(weekly, _dt(2026, 11, 28, 9, 0)) == _dt(2026, 12, 5, 8, 0)


def test_next_due_respects_timezone():
    # 08:00 PKT == 03:00 UTC; a 02:00 UTC "now" is still before today's run.
    assert next_due(_schedule(), datetime(2026, 11, 28, 2, 0, tzinfo=UTC)) == _dt(
        2026, 11, 28, 8, 0
    )


# --- quiet hours ------------------------------------------------------------

def test_quiet_hours_overnight_window():
    s = _schedule()
    assert is_quiet_hour(s, _dt(2026, 11, 28, 23, 30))
    assert is_quiet_hour(s, _dt(2026, 11, 28, 3, 0))
    assert not is_quiet_hour(s, _dt(2026, 11, 28, 8, 0))
    assert not is_quiet_hour(s, _dt(2026, 11, 28, 21, 59))


def test_quiet_hours_daytime_window():
    s = _schedule(quiet_start=time(12, 0), quiet_end=time(13, 0))
    assert is_quiet_hour(s, _dt(2026, 11, 28, 12, 30))
    assert not is_quiet_hour(s, _dt(2026, 11, 28, 13, 0))
    assert not is_quiet_hour(s, _dt(2026, 11, 28, 11, 59))


# --- should_send ------------------------------------------------------------

def test_should_send_right_after_due_time():
    assert should_send(_schedule(), _dt(2026, 11, 28, 8, 5))


def test_should_send_false_before_due_time():
    assert not should_send(_schedule(), _dt(2026, 11, 28, 7, 55))


def test_should_send_false_when_disabled():
    assert not should_send(_schedule(enabled=False), _dt(2026, 11, 28, 8, 5))


def test_should_send_false_in_quiet_hours():
    # 08:00 run with quiet hours covering the morning is held back.
    s = _schedule(quiet_start=time(7, 0), quiet_end=time(9, 0))
    assert not should_send(s, _dt(2026, 11, 28, 8, 5))


def test_should_send_false_when_already_sent_this_occurrence():
    s = _schedule(last_sent_at=_dt(2026, 11, 28, 8, 1))
    assert not should_send(s, _dt(2026, 11, 28, 8, 5))


def test_should_send_true_when_last_sent_was_yesterday():
    s = _schedule(last_sent_at=_dt(2026, 11, 27, 8, 1))
    assert should_send(s, _dt(2026, 11, 28, 8, 5))


def test_should_send_false_for_long_missed_occurrence():
    # 08:00 run, worker only wakes at noon: stale occurrence is skipped.
    assert not should_send(_schedule(), _dt(2026, 11, 28, 12, 0))


# --- validation -------------------------------------------------------------

@pytest.mark.parametrize(
    "overrides",
    [
        {"template": "unknown_template"},
        {"cadence": "hourly"},
        {"weekday": 7},
        {"channels": ()},
        {"channels": ("pigeon",)},
        {"recipients": ()},
        {"overdue_days": -1},
        {"timezone": "Mars/Olympus"},
        {"quiet_start": time(8, 0), "quiet_end": time(8, 0)},
    ],
)
def test_validate_schedule_rejects_bad_values(overrides):
    with pytest.raises(ValueError):
        validate_schedule(_schedule(**overrides))


# --- daily cash summary builder ---------------------------------------------

def _rows():
    as_of = date(2026, 11, 28)
    invoices = [
        InvoiceRow("pending", Decimal("100"), "PKR", date(2026, 11, 20)),  # 8d old -> overdue
        InvoiceRow("pending", Decimal("50"), "PKR", date(2026, 11, 27)),
        InvoiceRow("verified", Decimal("200"), "PKR", date(2026, 11, 25)),
        InvoiceRow("exception", Decimal("75"), "PKR", date(2026, 11, 10)),  # overdue
        InvoiceRow("paid", Decimal("500"), "PKR", date(2026, 11, 1)),
        InvoiceRow("pending", Decimal("999"), "USD", date(2026, 11, 27)),  # other currency
        InvoiceRow("approved", None, "PKR", date(2026, 11, 27)),  # no amount yet
    ]
    payments = [
        PaymentRow("awaiting_approval", Decimal("300"), "PKR", _dt(2026, 11, 15, 10)),
        PaymentRow("verified", Decimal("400"), "PKR", _dt(2026, 11, 28, 9), _dt(2026, 11, 28, 9)),
        PaymentRow("dispatched", Decimal("150"), "PKR", _dt(2026, 11, 28, 10)),
        PaymentRow("awaiting_approval", Decimal("10"), "USD", _dt(2026, 11, 15, 10)),
    ]
    return as_of, invoices, payments


def test_builder_totals_by_status_and_currency():
    as_of, invoices, payments = _rows()
    s = build_daily_cash_summary(invoices, payments, as_of=as_of, currency="PKR")
    assert s.total_invoiced == Decimal("925")  # 100+50+200+75+500+0, USD excluded
    assert s.total_paid == Decimal("400")  # verified payments only
    assert s.invoices["pending"].count == 2
    assert s.invoices["pending"].amount == Decimal("150")
    assert s.invoices["verified"].amount == Decimal("200")
    assert s.invoices["paid"].amount == Decimal("500")
    assert s.invoices["approved"].count == 1  # None amount counts as 0
    assert s.payments["awaiting_approval"].count == 1
    assert s.payments["awaiting_approval"].amount == Decimal("300")
    assert s.payments["dispatched"].amount == Decimal("150")


def test_builder_overdue_counts():
    as_of, invoices, payments = _rows()
    s = build_daily_cash_summary(invoices, payments, as_of=as_of, currency="PKR")
    assert s.overdue_invoices == 2  # pending 8d old + exception 18d old
    assert s.overdue_payments == 1  # awaiting approval 13d old


def test_builder_overdue_threshold_is_configurable():
    as_of, invoices, payments = _rows()
    s = build_daily_cash_summary(
        invoices, payments, as_of=as_of, currency="PKR", overdue_days=30
    )
    assert s.overdue_invoices == 0
    assert s.overdue_payments == 0


def test_builder_empty_input_is_zeroed_not_an_error():
    s = build_daily_cash_summary([], [], as_of=date(2026, 11, 28), currency="PKR")
    assert s.total_invoiced == Decimal("0")
    assert s.total_paid == Decimal("0")
    assert s.invoices == {}
    assert s.payments == {}
    assert s.overdue_invoices == 0


def test_builder_rejects_unknown_invoice_status():
    with pytest.raises(ValueError):
        build_daily_cash_summary(
            [InvoiceRow("mystery", Decimal("1"), "PKR")],
            [],
            as_of=date(2026, 11, 28),
            currency="PKR",
        )


def test_builder_rejects_unknown_payment_status():
    with pytest.raises(ValueError):
        build_daily_cash_summary(
            [],
            [PaymentRow("mystery", Decimal("1"), "PKR")],
            as_of=date(2026, 11, 28),
            currency="PKR",
        )


def test_builder_default_overdue_days_is_seven():
    assert DEFAULT_OVERDUE_DAYS == 7


# --- text rendering ---------------------------------------------------------

def test_render_contains_totals_and_overdue_line():
    as_of, invoices, payments = _rows()
    s = build_daily_cash_summary(invoices, payments, as_of=as_of, currency="PKR")
    text = render_daily_cash_summary_text(s)
    assert "2026-11-28" in text
    assert "Total invoiced: 925 PKR" in text
    assert "Total paid (verified): 400 PKR" in text
    assert "pending: 2 — 150 PKR" in text
    assert "Overdue: 2 invoice(s), 1 payment(s) awaiting approval" in text


def test_render_empty_summary_has_no_status_sections():
    s = build_daily_cash_summary([], [], as_of=date(2026, 11, 28), currency="PKR")
    text = render_daily_cash_summary_text(s)
    assert "Total invoiced: 0 PKR" in text
    assert "Invoices:" not in text
    assert "Overdue:" not in text


def test_render_never_includes_account_numbers_or_links():
    as_of, invoices, payments = _rows()
    s = build_daily_cash_summary(invoices, payments, as_of=as_of, currency="PKR")
    text = render_daily_cash_summary_text(s)
    assert "http" not in text
    # No action links: a report must never tell the reader to approve/pay here.
    assert "approve here" not in text.lower()
    assert "pay now" not in text.lower()
    assert "click" not in text.lower()


# --- weekly invoices (slice 5) ----------------------------------------------

def _weekly_rows():
    week_start, week_end = date(2026, 11, 23), date(2026, 11, 29)
    invoices = [
        InvoiceRow("pending", Decimal("100"), "PKR", date(2026, 11, 24),
                   created_at=datetime(2026, 11, 24, 9, 0, tzinfo=PKT),
                   invoice_ref="INV-0001", vendor_name="Alpha Supplies"),
        InvoiceRow("verified", Decimal("200"), "PKR", date(2026, 11, 25),
                   created_at=datetime(2026, 11, 25, 9, 0, tzinfo=PKT),
                   invoice_ref="INV-0002", vendor_name="Beta Traders"),
        InvoiceRow("paid", Decimal("500"), "PKR", date(2026, 11, 26),
                   created_at=datetime(2026, 11, 26, 9, 0, tzinfo=PKT),
                   invoice_ref="INV-0003", vendor_name="Alpha Supplies"),
        # Created before the week: not "new", but still an open exception.
        InvoiceRow("exception", Decimal("75"), "PKR", date(2026, 11, 10),
                   created_at=datetime(2026, 11, 10, 9, 0, tzinfo=PKT),
                   invoice_ref="INV-0004", vendor_name="Gamma Goods"),
        # Other currency: ignored for the PKR summary.
        InvoiceRow("pending", Decimal("999"), "USD", date(2026, 11, 24),
                   created_at=datetime(2026, 11, 24, 9, 0, tzinfo=PKT),
                   invoice_ref="INV-0005", vendor_name="Delta Inc"),
    ]
    payments = [
        PaymentRow("verified", Decimal("400"), "PKR",
                   created_at=datetime(2026, 11, 25, 10, 0, tzinfo=PKT),
                   verified_at=datetime(2026, 11, 26, 10, 0, tzinfo=PKT),
                   vendor_name="Beta Traders"),
        # Verified before the week: not counted as paid this week.
        PaymentRow("verified", Decimal("50"), "PKR",
                   created_at=datetime(2026, 11, 20, 10, 0, tzinfo=PKT),
                   verified_at=datetime(2026, 11, 20, 10, 0, tzinfo=PKT),
                   vendor_name="Beta Traders"),
    ]
    return week_start, week_end, invoices, payments


def test_weekly_summary_groups_new_invoices_by_status():
    week_start, week_end, invoices, payments = _weekly_rows()
    s = build_weekly_invoices_summary(invoices, payments, week_start=week_start,
                                      week_end=week_end, currency="PKR")
    assert s.new_count == 3
    assert s.total_new == Decimal("800")
    assert s.new_by_status["pending"].count == 1
    assert s.new_by_status["verified"].amount == Decimal("200")
    assert s.new_by_status["paid"].amount == Decimal("500")
    assert "exception" not in s.new_by_status  # created before the week


def test_weekly_summary_counts_exceptions_any_age():
    week_start, week_end, invoices, payments = _weekly_rows()
    s = build_weekly_invoices_summary(invoices, payments, week_start=week_start,
                                      week_end=week_end, currency="PKR")
    assert s.exceptions_open_count == 1
    assert s.exceptions_open[0].ref == "INV-0004"
    assert s.exceptions_open[0].age_days == 19  # 2026-11-10 to week end 2026-11-29


def test_weekly_summary_top_vendors_and_paid_this_week():
    week_start, week_end, invoices, payments = _weekly_rows()
    s = build_weekly_invoices_summary(invoices, payments, week_start=week_start,
                                      week_end=week_end, currency="PKR")
    assert [v.vendor_name for v in s.top_vendors] == ["Alpha Supplies", "Beta Traders"]
    assert s.top_vendors[0].amount == Decimal("600")
    assert s.paid_count == 1 and s.total_paid == Decimal("400")


def test_weekly_summary_falls_back_to_invoice_date_without_created_at():
    week_start, week_end = date(2026, 11, 23), date(2026, 11, 29)
    s = build_weekly_invoices_summary(
        [InvoiceRow("pending", Decimal("10"), "PKR", date(2026, 11, 24))],
        [], week_start=week_start, week_end=week_end, currency="PKR")
    assert s.new_count == 1


def test_weekly_summary_rejects_unknown_status():
    week_start, week_end = date(2026, 11, 23), date(2026, 11, 29)
    with pytest.raises(ValueError):
        build_weekly_invoices_summary(
            [InvoiceRow("mystery", Decimal("1"), "PKR")], [],
            week_start=week_start, week_end=week_end, currency="PKR")


def test_weekly_render_lists_items_and_more_line():
    week_start, week_end = date(2026, 11, 23), date(2026, 11, 29)
    invoices = [
        InvoiceRow("exception", Decimal("10"), "PKR", date(2026, 11, 1),
                   created_at=datetime(2026, 11, 1, 9, 0, tzinfo=PKT),
                   invoice_ref=f"INV-{n:04d}", vendor_name="Vendor")
        for n in range(11)
    ]
    s = build_weekly_invoices_summary(invoices, [], week_start=week_start,
                                      week_end=week_end, currency="PKR")
    text = render_weekly_invoices_text(s)
    assert "Exceptions needing a decision: 11" in text
    assert "…and 1 more" in text
    assert "http" not in text


# --- overdue alert (slice 5) -------------------------------------------------

def test_overdue_alert_lists_oldest_first():
    as_of = date(2026, 11, 28)
    invoices = [
        InvoiceRow("pending", Decimal("100"), "PKR", date(2026, 11, 20),
                   invoice_ref="INV-0001", vendor_name="Alpha Supplies"),
        InvoiceRow("exception", Decimal("75"), "PKR", date(2026, 11, 10),
                   invoice_ref="INV-0002", vendor_name="Gamma Goods"),
        InvoiceRow("pending", Decimal("50"), "PKR", date(2026, 11, 27),
                   invoice_ref="INV-0003", vendor_name="Beta Traders"),  # 1d old: not overdue
    ]
    payments = [
        PaymentRow("awaiting_approval", Decimal("250"), "PKR",
                   created_at=datetime(2026, 11, 18, 10, 0, tzinfo=PKT),
                   vendor_name="Alpha Supplies"),
    ]
    alert = build_overdue_alert(invoices, payments, as_of=as_of, currency="PKR")
    assert alert.total_count == 3
    assert alert.total_amount == Decimal("425")
    assert [item.ref for item in alert.items] == ["INV-0002", "Alpha Supplies", "INV-0001"]
    assert alert.items[0].age_days == 18 and alert.items[0].kind == "invoice"
    assert alert.items[1].kind == "payment"
    text = render_overdue_alert_text(alert)
    assert "Overdue: 3 item(s) — 425 PKR" in text
    assert "18d overdue" in text


def test_overdue_alert_empty_when_nothing_overdue():
    alert = build_overdue_alert([], [], as_of=date(2026, 11, 28), currency="PKR")
    assert alert.total_count == 0 and alert.items == ()
    assert "Overdue: 0 item(s)" in render_overdue_alert_text(alert)


def test_overdue_alert_caps_listed_items():
    as_of = date(2026, 11, 28)
    invoices = [
        InvoiceRow("pending", Decimal("10"), "PKR", date(2026, 11, 1),
                   invoice_ref=f"INV-{n:04d}", vendor_name="Vendor")
        for n in range(21)
    ]
    alert = build_overdue_alert(invoices, [], as_of=as_of, currency="PKR")
    assert alert.total_count == 21 and len(alert.items) == 20
    assert "…and 1 more" in render_overdue_alert_text(alert)


def test_overdue_alert_rejects_unknown_status():
    with pytest.raises(ValueError):
        build_overdue_alert([InvoiceRow("mystery", Decimal("1"), "PKR")], [],
                            as_of=date(2026, 11, 28), currency="PKR")
