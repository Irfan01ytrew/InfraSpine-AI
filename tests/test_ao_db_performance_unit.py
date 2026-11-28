"""Unit tests for Step AO slice 3: pool tuning, PgBouncer validation (no database)."""

from pathlib import Path

from app.ops.db_performance import (
    classify_pool_pressure,
    pool_tuning_recommendation,
    validate_pgbouncer_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pool_pressure_bands():
    assert classify_pool_pressure(0, 5, 10) == "ok"
    assert classify_pool_pressure(10, 5, 10) == "ok"  # 10/15 = 67%
    assert classify_pool_pressure(11, 5, 10) == "warning"  # 73%
    assert classify_pool_pressure(14, 5, 10) == "critical"  # 93%
    assert classify_pool_pressure(0, 0, 0) == "unknown"  # no budget to measure against


def test_pool_pressure_rejects_negative_numbers():
    import pytest

    with pytest.raises(ValueError):
        classify_pool_pressure(-1, 5, 10)


def test_tuning_keeps_a_pool_that_fits_its_peak():
    result = pool_tuning_recommendation(
        peak_checked_out=4, pool_size=5, max_overflow=10, overflow_observed=False
    )
    assert result["recommendation"] == "keep"
    assert result["suggested_pool_size"] == 5
    assert result["suggested_max_overflow"] == 10
    assert result["reasons"]


def test_tuning_increases_when_overflow_was_used():
    result = pool_tuning_recommendation(
        peak_checked_out=12, pool_size=5, max_overflow=10, overflow_observed=True
    )
    assert result["recommendation"] == "increase"
    assert result["suggested_pool_size"] >= 12
    assert result["suggested_max_overflow"] >= 10


def test_tuning_decreases_an_oversized_pool():
    result = pool_tuning_recommendation(
        peak_checked_out=1, pool_size=5, max_overflow=10, overflow_observed=False
    )
    assert result["recommendation"] == "decrease"
    assert result["suggested_pool_size"] < 5


def test_tuning_rejects_nonsense_inputs():
    import pytest

    with pytest.raises(ValueError):
        pool_tuning_recommendation(
            peak_checked_out=-1, pool_size=5, max_overflow=10, overflow_observed=False
        )
    with pytest.raises(ValueError):
        pool_tuning_recommendation(
            peak_checked_out=1, pool_size=0, max_overflow=10, overflow_observed=False
        )


def test_pgbouncer_example_config_passes_validation():
    config = (REPO_ROOT / "docs" / "pgbouncer.example.ini").read_text()
    assert validate_pgbouncer_config(config) == []


def test_pgbouncer_config_rejects_session_mode():
    config = (
        "[databases]\ninfraspine = host=127.0.0.1 port=5432 dbname=infraspine\n\n"
        "[pgbouncer]\npool_mode = session\nserver_reset_query = DISCARD ALL\n"
        "max_client_conn = 100\ndefault_pool_size = 20\n"
    )
    problems = validate_pgbouncer_config(config)
    assert any("pool_mode" in problem for problem in problems)


def test_pgbouncer_config_names_every_missing_piece():
    problems = validate_pgbouncer_config("[pgbouncer]\npool_mode = transaction\n")
    assert any("databases" in problem for problem in problems)
    assert any("server_reset_query" in problem for problem in problems)
    assert any("max_client_conn" in problem for problem in problems)
    assert any("default_pool_size" in problem for problem in problems)


def test_pgbouncer_config_rejects_unparsable_text():
    assert validate_pgbouncer_config("[unclosed") != []
