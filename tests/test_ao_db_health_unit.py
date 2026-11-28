"""Pure unit tests for Step AO database-health classifiers. No database."""

import pytest

from app.ops.db_health import (
    CRITICAL,
    OK,
    UNKNOWN,
    WARNING,
    band_for,
    classify_connection_usage,
    classify_dead_tuple_ratio,
    classify_longest_query_age,
    classify_replication_lag,
    classify_slow_query,
    summarize_health,
)


def test_band_for_higher_is_worse_boundaries_are_inclusive():
    assert band_for(69.9, 70.0, 90.0) == OK
    assert band_for(70.0, 70.0, 90.0) == WARNING
    assert band_for(89.9, 70.0, 90.0) == WARNING
    assert band_for(90.0, 70.0, 90.0) == CRITICAL


def test_band_for_lower_is_worse():
    assert band_for(10.0, 5.0, 1.0, higher_is_worse=False) == OK
    assert band_for(5.0, 5.0, 1.0, higher_is_worse=False) == WARNING
    assert band_for(1.0, 5.0, 1.0, higher_is_worse=False) == CRITICAL


def test_band_for_rejects_inverted_or_equal_thresholds():
    with pytest.raises(ValueError):
        band_for(50.0, 90.0, 70.0)
    with pytest.raises(ValueError):
        band_for(50.0, 70.0, 70.0)
    with pytest.raises(ValueError):
        band_for(50.0, 1.0, 5.0, higher_is_worse=False)


def test_connection_usage_bands_and_unknown_limit():
    assert classify_connection_usage(0, 100) == OK
    assert classify_connection_usage(69, 100) == OK
    assert classify_connection_usage(70, 100) == WARNING
    assert classify_connection_usage(90, 100) == CRITICAL
    assert classify_connection_usage(10, None) == UNKNOWN
    assert classify_connection_usage(10, 0) == UNKNOWN
    with pytest.raises(ValueError):
        classify_connection_usage(-1, 100)


def test_longest_query_age_bands():
    assert classify_longest_query_age(None) == UNKNOWN
    assert classify_longest_query_age(0) == OK
    assert classify_longest_query_age(59.9) == OK
    assert classify_longest_query_age(60.0) == WARNING
    assert classify_longest_query_age(300.0) == CRITICAL
    with pytest.raises(ValueError):
        classify_longest_query_age(-1)


def test_dead_tuple_ratio_bands():
    assert classify_dead_tuple_ratio(0, 0) == OK  # empty table is not bloat
    assert classify_dead_tuple_ratio(9, 91) == OK
    assert classify_dead_tuple_ratio(10, 90) == WARNING
    assert classify_dead_tuple_ratio(20, 80) == CRITICAL
    with pytest.raises(ValueError):
        classify_dead_tuple_ratio(-1, 10)


def test_replication_lag_bands():
    assert classify_replication_lag(None) == UNKNOWN  # no replica: unknown, never ok
    assert classify_replication_lag(4.9) == OK
    assert classify_replication_lag(5.0) == WARNING
    assert classify_replication_lag(60.0) == CRITICAL
    with pytest.raises(ValueError):
        classify_replication_lag(-1)


def test_slow_query_bands_and_unjudgeable_inputs():
    assert classify_slow_query(None, 0) == UNKNOWN
    assert classify_slow_query(500.0, 0) == UNKNOWN
    assert classify_slow_query(None, 10) == UNKNOWN
    assert classify_slow_query(99.9, 10) == OK
    assert classify_slow_query(100.0, 10) == WARNING
    assert classify_slow_query(1000.0, 10) == CRITICAL
    assert classify_slow_query(500.0, 10, warn_ms=600.0, crit_ms=900.0) == OK
    with pytest.raises(ValueError):
        classify_slow_query(10.0, -1)
    with pytest.raises(ValueError):
        classify_slow_query(-5.0, 10)


def test_summarize_health_worst_band_wins():
    assert summarize_health({"a": OK, "b": OK}) == OK
    assert summarize_health({"a": OK, "b": WARNING}) == WARNING
    assert summarize_health({"a": WARNING, "b": CRITICAL}) == CRITICAL
    assert summarize_health({"a": CRITICAL, "b": OK}) == CRITICAL


def test_summarize_health_unknown_never_escalates():
    assert summarize_health({"a": OK, "b": UNKNOWN}) == OK
    assert summarize_health({"a": UNKNOWN}) == UNKNOWN
    assert summarize_health({}) == OK
