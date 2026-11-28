"""Step AO slice 3: connection pooling + performance measurement.

Measure first, tune second. Every number here is measured from the live
pool or from PostgreSQL statistics views — never guessed:

- Pool stats come from the SQLAlchemy pool itself (size, checked out,
  overflow). The tuning recommendation is justified by the measured
  peak, not by the defaults.
- Slow queries come from ``pg_stat_statements`` when the extension
  exists; when it does not, the collector reports ``available: False``
  honestly instead of inventing numbers.
- The index audit reports *candidates*: unused indexes (zero scans
  since statistics reset, excluding indexes that enforce constraints)
  and tables that are only ever sequentially scanned. A candidate is
  never a verdict — dropping or adding an index is a migration and
  follows the migration-safety rules (plan §7).
"""

from __future__ import annotations

import configparser
import io
import math
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncEngine

from app.ops.db_health import Band, classify_slow_query

Recommendation = Literal["keep", "increase", "decrease"]


def classify_pool_pressure(checked_out: int, pool_size: int, max_overflow: int) -> Band:
    """Share of the total connection budget (pool + overflow) in use."""
    if pool_size < 0 or max_overflow < 0 or checked_out < 0:
        raise ValueError("pool numbers must not be negative")
    budget = pool_size + max_overflow
    if budget == 0:
        return "unknown"
    percent = 100.0 * checked_out / budget
    if percent >= 90.0:
        return "critical"
    if percent >= 70.0:
        return "warning"
    return "ok"


def pool_tuning_recommendation(
    *,
    peak_checked_out: int,
    pool_size: int,
    max_overflow: int,
    overflow_observed: bool,
) -> dict:
    """Justify the pool numbers from the measured peak.

    Rules (recorded in the plan doc §4):
    - The pool must cover the measured peak plus 25% headroom; overflow
      is for bursts, not for the steady state.
    - Overflow was used (peak reached the pool size) → raise the pool
      size toward the peak instead of living in overflow.
    - Peak below half the pool size → the pool is oversized; suggest
      lowering it so idle connections stop sitting on the database.
    - Otherwise keep the current numbers; a recommendation to change
      nothing is a result, not a non-answer.
    """
    if peak_checked_out < 0 or pool_size < 1 or max_overflow < 0:
        raise ValueError("pool measurements must be sane (pool_size >= 1, nothing negative)")
    reasons: list[str] = []
    if overflow_observed:
        suggested_size = max(pool_size, math.ceil(peak_checked_out * 1.25))
        suggested_overflow = max(10, math.ceil(peak_checked_out * 0.5))
        reasons.append(
            f"overflow was used: peak {peak_checked_out} reached the pool size {pool_size}; "
            "raising the pool size keeps steady traffic out of overflow"
        )
        return {
            "recommendation": "increase",
            "suggested_pool_size": suggested_size,
            "suggested_max_overflow": suggested_overflow,
            "reasons": reasons,
        }
    if peak_checked_out * 2 < pool_size:
        suggested_size = max(2, math.ceil(peak_checked_out * 1.25)) if peak_checked_out else 2
        reasons.append(
            f"peak {peak_checked_out} is below half the pool size {pool_size}; "
            "the pool is oversized for the measured workload"
        )
        return {
            "recommendation": "decrease",
            "suggested_pool_size": suggested_size,
            "suggested_max_overflow": max_overflow,
            "reasons": reasons,
        }
    reasons.append(
        f"peak {peak_checked_out} fits the pool size {pool_size} with headroom and never used overflow"
    )
    return {
        "recommendation": "keep",
        "suggested_pool_size": pool_size,
        "suggested_max_overflow": max_overflow,
        "reasons": reasons,
    }


def collect_pool_stats(engine: AsyncEngine) -> dict:
    """Measured pool state from the engine's own pool. No database call."""
    pool = engine.pool
    checked_out = int(pool.checkedout())
    pool_size = int(pool.size())
    overflow = int(pool.overflow())
    max_overflow = int(getattr(pool, "_max_overflow", 0) or 0)
    return {
        "size": pool_size,
        "checked_in": int(pool.checkedin()),
        "checked_out": checked_out,
        "overflow": overflow,
        "max_overflow": max_overflow,
        "band": classify_pool_pressure(checked_out, pool_size, max_overflow),
    }


async def collect_slow_queries(engine: AsyncEngine, *, limit: int = 10) -> dict:
    """Top queries by total time from pg_stat_statements, or an honest unavailable."""
    from sqlalchemy import text

    async with engine.connect() as conn:
        available = bool(
            await conn.scalar(
                text("SELECT count(*) FROM pg_extension WHERE extname = 'pg_stat_statements'")
            )
        )
        if not available:
            return {"available": False, "queries": []}
        rows = (
            await conn.execute(
                text(
                    "SELECT left(query, 200) AS query, calls, "
                    "round(mean_exec_time::numeric, 2) AS mean_ms, "
                    "round(total_exec_time::numeric, 2) AS total_ms "
                    "FROM pg_stat_statements "
                    "ORDER BY total_exec_time DESC LIMIT :limit"
                ),
                {"limit": limit},
            )
        ).all()
    queries = [
        {
            "query": row[0],
            "calls": int(row[1]),
            "mean_ms": float(row[2]),
            "total_ms": float(row[3]),
            "band": classify_slow_query(float(row[2]), int(row[1])),
        }
        for row in rows
    ]
    return {"available": True, "queries": queries}


async def index_audit(engine: AsyncEngine) -> dict:
    """Unused indexes + sequential-scan-only table candidates. Read-only.

    Unused: zero index scans since statistics reset, excluding indexes
    that back a constraint (dropping one of those would drop the
    constraint). Candidates: tables with only sequential scans, more
    than 1000 live rows and more than 10,000 rows read sequentially —
    a human must confirm with EXPLAIN before any index is added.
    """
    from sqlalchemy import text

    async with engine.connect() as conn:
        unused = (
            await conn.execute(
                text(
                    "SELECT schemaname, relname AS tablename, indexrelname, "
                    "pg_relation_size(indexrelid) AS bytes "
                    "FROM pg_stat_user_indexes "
                    "WHERE idx_scan = 0 "
                    "AND indexrelid NOT IN (SELECT conindid FROM pg_constraint WHERE conindid IS NOT NULL) "
                    "ORDER BY schemaname, tablename, indexrelname"
                )
            )
        ).all()
        candidates = (
            await conn.execute(
                text(
                    "SELECT schemaname, relname, seq_scan, seq_tup_read, n_live_tup "
                    "FROM pg_stat_user_tables "
                    "WHERE idx_scan = 0 AND seq_scan > 0 "
                    "AND seq_tup_read > 10000 AND n_live_tup > 1000 "
                    "ORDER BY seq_tup_read DESC"
                )
            )
        ).all()
    return {
        "unused_indexes": [
            {
                "schema": row[0],
                "table": row[1],
                "index": row[2],
                "bytes": int(row[3]),
            }
            for row in unused
        ],
        "sequential_scan_candidates": [
            {
                "schema": row[0],
                "table": row[1],
                "seq_scan": int(row[2]),
                "seq_tup_read": int(row[3]),
                "live_tuples": int(row[4]),
                "note": "candidate only — confirm with EXPLAIN before adding an index",
            }
            for row in candidates
        ],
    }


_PGBOUNCER_REQUIRED = {
    "pool_mode": "transaction",
    "server_reset_query": "DISCARD ALL",
}


def validate_pgbouncer_config(config_text: str) -> list[str]:
    """Validate a PgBouncer config. Empty list means usable as the AO option.

    Required: a [databases] section naming the application database,
    transaction pooling (session mode breaks the app's use of prepared
    statements across checkouts), ``server_reset_query = DISCARD ALL``
    (no session state may leak between clients), and explicit pool
    sizes. Problems are named; silence is a pass.
    """
    problems: list[str] = []
    parser = configparser.ConfigParser()
    parser.optionxform = str  # keep key case for readable messages
    try:
        parser.read_string(config_text)
    except configparser.Error as exc:
        return [f"unparsable config: {exc}"]
    if not parser.has_section("databases"):
        problems.append("missing [databases] section")
    else:
        if not parser.options("databases"):
            problems.append("[databases] names no database")
    if not parser.has_section("pgbouncer"):
        problems.append("missing [pgbouncer] section")
        return problems
    section = parser["pgbouncer"]
    for key, expected in _PGBOUNCER_REQUIRED.items():
        actual = section.get(key)
        if actual is None:
            problems.append(f"missing {key} (expected {expected!r})")
        elif actual.strip().lower() != expected.lower():
            problems.append(f"{key} is {actual.strip()!r}, expected {expected!r}")
    for key in ("max_client_conn", "default_pool_size"):
        value = section.get(key)
        if value is None:
            problems.append(f"missing {key}")
            continue
        try:
            if int(value) < 1:
                problems.append(f"{key} must be at least 1, got {value!r}")
        except ValueError:
            problems.append(f"{key} must be a number, got {value!r}")
    return problems
