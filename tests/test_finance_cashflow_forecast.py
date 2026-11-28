"""Tests for the Step AC cash-flow forecaster (pure functions, no DB)."""

from datetime import date
from decimal import Decimal

import pytest

from app.finance.cashflow import (ForecastMonth, HistoryMonth, PendingInvoice,
                                  forecast_cash_flow)

TODAY = date(2026, 11, 28)
D = Decimal


def history_months(n, outflow='1000', inflow='0', start=(2026, 6)):
    """n consecutive months ending 2026-11, oldest first."""
    out = []
    year, month = start
    for _ in range(n):
        out.append(HistoryMonth(month=f'{year:04d}-{month:02d}',
                                outflow=D(outflow), inflow=D(inflow)))
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out


def codes(result):
    return [w['code'] for w in result.warnings]


def test_scheduled_invoice_lands_in_its_due_month():
    result = forecast_cash_flow(
        history=[], pending=[PendingInvoice(amount=D('500'), currency='PKR',
                                            due_date=date(2026, 12, 10))],
        opening_balance=D('1000'), currency='PKR', months=3, today=TODAY)
    assert [m.month for m in result.months] == ['2026-11', '2026-12', '2027-01']
    assert result.months[0].scheduled_outflow == D('0')
    assert result.months[1].scheduled_outflow == D('500')
    assert result.months[2].scheduled_outflow == D('0')
    assert result.months[1].basis == 'scheduled'


def test_invoice_date_plus_terms_sets_due_month():
    # invoice 2026-11-05 + 30 days = 2026-12-05 -> month 2
    result = forecast_cash_flow(
        history=[], pending=[PendingInvoice(amount=D('200'), currency='PKR',
                                            invoice_date=date(2026, 11, 5))],
        opening_balance=D('0'), currency='PKR', months=2, today=TODAY)
    assert result.months[1].scheduled_outflow == D('200')


def test_due_date_wins_over_invoice_date():
    result = forecast_cash_flow(
        history=[], pending=[PendingInvoice(amount=D('200'), currency='PKR',
                                            due_date=date(2026, 11, 30),
                                            invoice_date=date(2026, 10, 1))],
        opening_balance=D('0'), currency='PKR', months=1, today=TODAY)
    assert result.months[0].scheduled_outflow == D('200')


def test_invoice_without_any_date_goes_to_month_one_with_warning():
    result = forecast_cash_flow(
        history=[], pending=[PendingInvoice(amount=D('150'), currency='PKR')],
        opening_balance=D('0'), currency='PKR', months=2, today=TODAY)
    assert result.months[0].scheduled_outflow == D('150')
    assert 'missing_due_date' in codes(result)


def test_overdue_invoice_goes_to_month_one_with_warning():
    result = forecast_cash_flow(
        history=[], pending=[PendingInvoice(amount=D('300'), currency='PKR',
                                            due_date=date(2026, 9, 15))],
        opening_balance=D('0'), currency='PKR', months=2, today=TODAY)
    assert result.months[0].scheduled_outflow == D('300')
    assert 'overdue_invoice' in codes(result)


def test_recurring_average_uses_last_six_months_only():
    # 7 months of history: oldest (6000) must fall out of the 6-month window.
    hist = [HistoryMonth(month='2026-05', outflow=D('6000'))]
    hist += history_months(6, outflow='1000', start=(2026, 6))
    result = forecast_cash_flow(history=hist, pending=[], opening_balance=D('0'),
                                currency='PKR', months=1, today=TODAY)
    assert result.months[0].recurring_estimate == D('1000')
    assert result.months[0].basis == 'recurring'
    assert result.history_months_used == 6


def test_recurring_average_uses_available_months_when_few():
    result = forecast_cash_flow(history=history_months(2, outflow='400'),
                                pending=[], opening_balance=D('0'),
                                currency='PKR', months=1, today=TODAY)
    assert result.months[0].recurring_estimate == D('400')
    assert result.history_months_used == 2


def test_empty_history_means_zero_recurring_low_confidence_and_warning():
    result = forecast_cash_flow(history=[], pending=[], opening_balance=D('0'),
                                currency='PKR', months=1, today=TODAY)
    assert result.months[0].recurring_estimate == D('0')
    assert result.months[0].expected_outflow == D('0')
    assert result.months[0].confidence == 'low'
    assert result.months[0].basis == 'none'
    assert 'no_history' in codes(result)


def test_running_balance_math_across_months():
    hist = history_months(6, outflow='100', inflow='500')
    result = forecast_cash_flow(history=hist, pending=[], opening_balance=D('1000'),
                                currency='PKR', months=3, today=TODAY)
    # each month: net +400 -> 1400, 1800, 2200
    assert [m.closing_balance for m in result.months] == [D('1400'), D('1800'), D('2200')]
    assert result.closing_balance == D('2200')
    assert result.total_expected_outflow == D('300')
    assert result.total_expected_inflow == D('1500')


def test_scheduled_and_recurring_are_not_double_counted():
    hist = history_months(6, outflow='100')
    result = forecast_cash_flow(
        history=hist,
        pending=[PendingInvoice(amount=D('250'), currency='PKR',
                                due_date=date(2026, 11, 15))],
        opening_balance=D('0'), currency='PKR', months=1, today=TODAY)
    month = result.months[0]
    assert month.scheduled_outflow == D('250')
    assert month.recurring_estimate == D('100')
    assert month.expected_outflow == D('350')  # 250 + 100, not 250 counted twice
    assert month.basis == 'mixed'


def test_expected_inflow_is_history_average():
    hist = history_months(4, outflow='0', inflow='800')
    result = forecast_cash_flow(history=hist, pending=[], opening_balance=D('0'),
                                currency='PKR', months=2, today=TODAY)
    assert all(m.expected_inflow == D('800') for m in result.months)


def test_confidence_thresholds():
    assert forecast_cash_flow(history=history_months(6), pending=[],
                              opening_balance=D('0'), currency='PKR',
                              months=1, today=TODAY).months[0].confidence == 'high'
    assert forecast_cash_flow(history=history_months(4), pending=[],
                              opening_balance=D('0'), currency='PKR',
                              months=1, today=TODAY).months[0].confidence == 'medium'
    assert forecast_cash_flow(history=history_months(2), pending=[],
                              opening_balance=D('0'), currency='PKR',
                              months=1, today=TODAY).months[0].confidence == 'low'


def test_months_parameter_respected():
    result = forecast_cash_flow(history=[], pending=[], opening_balance=D('0'),
                                currency='PKR', months=12, today=TODAY)
    assert len(result.months) == 12
    assert result.months[-1].month == '2027-10'
    assert isinstance(result.months[0], ForecastMonth)


def test_invalid_months_rejected():
    for bad in (0, 13):
        with pytest.raises(ValueError):
            forecast_cash_flow(history=[], pending=[], opening_balance=D('0'),
                               currency='PKR', months=bad, today=TODAY)


def test_other_currency_rows_skipped_with_warning():
    result = forecast_cash_flow(
        history=[],
        pending=[PendingInvoice(amount=D('100'), currency='USD',
                                due_date=date(2026, 11, 20)),
                 PendingInvoice(amount=D('50'), currency='PKR',
                                due_date=date(2026, 11, 20))],
        opening_balance=D('0'), currency='PKR', months=1, today=TODAY)
    assert result.months[0].scheduled_outflow == D('50')
    assert 'skipped_other_currency' in codes(result)


def test_negative_pending_amount_rejected():
    with pytest.raises(ValueError):
        PendingInvoice(amount=D('-5'), currency='PKR')


def test_negative_history_amount_rejected():
    with pytest.raises(ValueError):
        HistoryMonth(month='2026-11', outflow=D('-1'))


def test_missing_opening_balance_starts_at_zero_with_warning():
    result = forecast_cash_flow(history=[], pending=[], opening_balance=None,
                                currency='PKR', months=1, today=TODAY)
    assert result.opening_balance == D('0')
    assert 'opening_balance_unknown' in codes(result)


def test_invoice_beyond_horizon_is_reported_not_silent():
    result = forecast_cash_flow(
        history=[],
        pending=[PendingInvoice(amount=D('900'), currency='PKR',
                                due_date=date(2027, 6, 1))],
        opening_balance=D('0'), currency='PKR', months=3, today=TODAY)
    assert all(m.scheduled_outflow == D('0') for m in result.months)
    assert 'beyond_horizon' in codes(result)
    assert '900' in next(w['detail'] for w in result.warnings
                         if w['code'] == 'beyond_horizon')


def test_future_history_month_ignored_with_warning():
    hist = history_months(2, outflow='100') + [HistoryMonth(month='2027-01',
                                                            outflow=D('9999'))]
    result = forecast_cash_flow(history=hist, pending=[], opening_balance=D('0'),
                                currency='PKR', months=1, today=TODAY)
    assert result.history_months_used == 2
    assert result.months[0].recurring_estimate == D('100')
    assert 'future_history_month' in codes(result)


def test_result_is_labelled_a_forecast():
    result = forecast_cash_flow(history=history_months(6), pending=[],
                                opening_balance=D('0'), currency='PKR',
                                months=1, today=TODAY)
    assert result.kind == 'forecast'
    assert result.currency == 'PKR'
    assert result.as_of == TODAY


def test_amounts_quantised_to_two_decimals():
    hist = [HistoryMonth(month='2026-11', outflow=D('100.005'))]
    result = forecast_cash_flow(history=hist, pending=[], opening_balance=D('0'),
                                currency='PKR', months=1, today=TODAY)
    # 100.005 quantises (ROUND_HALF_EVEN) to 100.00; either way it is 2dp.
    assert result.months[0].recurring_estimate == result.months[0].recurring_estimate.quantize(D('0.01'))


def test_invalid_month_string_rejected():
    with pytest.raises(ValueError):
        HistoryMonth(month='2026-13', outflow=D('1'))
    with pytest.raises(ValueError):
        HistoryMonth(month='Nov 2026', outflow=D('1'))
