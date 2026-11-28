"""Database health checks for Step AO.

Two layers:

1. Pure threshold classifiers (stdlib only, no database): connection
   usage, longest-running query, dead-tuple ratio, replication lag,
   slow queries. Each returns one of "ok", "warning", "critical" or
   "unknown" ("unknown" means the signal is missing — a missing signal
   never pretends to be healthy and never raises an alarm by itself).
2. ``collect_db_health``: a read-only async collector. It runs only
   SELECT / SHOW statements against the engine it is given, writes
   nothing, and reports "unavailable" honestly when an optional
   signal (a replica, ``pg_stat_statements``) does not exist.
"""

from __future__ import annotations

from typing import Literal, Mapping

from sqlalchemy.ext.asyncio import AsyncEngine

Band = Literal["ok", "warning", "critical", "unknown"]

OK = "ok"
WARNING = "warning"
CRITICAL = "critical"
UNKNOWN = "unknown"

#: Worst band wins; "unknown" never escalates a summary.
_BAND_SEVERITY = {OK: 0, UNKNOWN: 0, WARNING: 1, CRITICAL: 2}


def band_for(value: float, warn_at: float, crit_at: float, *, higher_is_worse: bool = True) -> Band:
    """Generic threshold band. Raises when the thresholds are inverted."""
    if warn_at == crit_at:
        raise ValueError("warn_at and crit_at must differ")
    if higher_is_worse and warn_at > crit_at:
        raise ValueError("warn_at must be below crit_at when higher is worse")
    if not higher_is_worse and warn_at < crit_at:
        raise ValueError("warn_at must be above crit_at when lower is worse")
    if higher_is_worse:
        if value >= crit_at:
            return CRITICAL
        if value >= warn_at:
            return WARNING
        return OK
    if value <= crit_at:
        return CRITICAL
    if value <= warn_at:
        return WARNING
    return OK


def classify_connection_usage(total_connections: int, max_connections: int | None) -> Band:
    """Share of the connection budget in use. Unknown when the limit is not known."""
    if max_connections is None or max_connections <= 0:
        return UNKNOWN
    if total_connections < 0:
        raise ValueError("total_connections must not be negative")
    percent = 100.0 * total_connections / max_connections
    return band_for(percent, 70.0, 90.0)


def classify_longest_query_age(seconds: float | None) -> Band:
    """Age of the longest-running non-idle query. None means no query is running."""
    if seconds is None:
        return UNKNOWN
    if seconds < 0:
        raise ValueError("seconds must not be negative")
    return band_for(seconds, 60.0, 300.0)


def classify_dead_tuple_ratio(dead_tuples: int, live_tuples: int) -> Band:
    """Share of dead tuples among live + dead. An empty table is not bloat."""
    if dead_tuples < 0 or live_tuples < 0:
        raise ValueError("tuple counts must not be negative")
    total = dead_tuples + live_tuples
    if total == 0:
        return OK
    return band_for(dead_tuples / total, 0.10, 0.20)


def classify_replication_lag(seconds: float | None) -> Band:
    """Replication lag. None means there is no replica to measure — unknown, never ok."""
    if seconds is None:
        return UNKNOWN
    if seconds < 0:
        raise ValueError("seconds must not be negative")
    return band_for(seconds, 5.0, 60.0)


def classify_slow_query(mean_ms: float | None, calls: int, *, warn_ms: float = 100.0, crit_ms: float = 1000.0) -> Band:
    """A query's mean execution time. A query never called has no mean to judge."""
    if calls < 0:
        raise ValueError("calls must not be negative")
    if calls == 0 or mean_ms is None:
        return UNKNOWN
    if mean_ms < 0:
        raise ValueError("mean_ms must not be negative")
    return band_for(mean_ms, warn_ms, crit_ms)


def summarize_health(bands: Mapping[str, Band]) -> Band:
    """Worst band wins. All-unknown reports unknown; an empty input reports ok."""
    worst: Band = OK
    seen_any = False
    for band in bands.values():
        seen_any = True
        if _BAND_SEVERITY[band] > _BAND_SEVERITY[worst]:
            worst = band
    if not seen_any:
        return OK
    if worst == OK and all(band == UNKNOWN for band in bands.values()):
        return UNKNOWN
    return worst


async def collect_db_health(engine: AsyncEngine) -> dict:
    """Read-only health snapshot. Writes nothing; returns reachable False when the database cannot be reached.

    Every number is measured, never defaulted: replication lag is None
    (unknown) when no replica exists, and pg_stat_statements is
    reported unavailable when the extension is not installed.
    """
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            database_bytes = await conn.scalar(text("SELECT pg_database_size(current_database())"))
            max_connections = int(await conn.scalar(text("SHOW max_connections")))
            activity = (
                await conn.execute(
                    text(
                        "SELECT count(*), "
                        "count(*) FILTER (WHERE state = 'active'), "
                        "count(*) FILTER (WHERE state = 'idle in transaction'), "
                        "coalesce(max(extract(epoch FROM now() - query_start)) "
                        "FILTER (WHERE query_start IS NOT NULL AND state <> 'idle'), 0) "
                        "FROM pg_stat_activity WHERE datname = current_database()"
                    )
                )
            ).one()
            locks = (
                await conn.execute(
                    text(
                        "SELECT count(*), count(*) FILTER (WHERE NOT granted) FROM pg_locks"
                    )
                )
            ).one()
            lock_wait = await conn.scalar(
                text(
                    "SELECT coalesce(max(extract(epoch FROM now() - query_start)), 0) "
                    "FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                )
            )
            tuples_row = (
                await conn.execute(
                    text(
                        "SELECT coalesce(sum(n_dead_tup), 0), coalesce(sum(n_live_tup), 0) "
                        "FROM pg_stat_user_tables"
                    )
                )
            ).one()
            stat_statements = await conn.scalar(
                text("SELECT count(*) FROM pg_extension WHERE extname = 'pg_stat_statements'")
            )
            replicas = await conn.scalar(text("SELECT count(*) FROM pg_stat_replication"))
    except Exception as exc:  # connection refused, auth failure, timeout: report, never raise
        return {"reachable": False, "error": type(exc).__name__, "overall": CRITICAL}

    total_connections = int(activity[0])
    connections_band = classify_connection_usage(total_connections, max_connections)
    # extract(epoch FROM now() - query_start) can be a hair negative when
    # the query started in the same instant as the transaction (now() is
    # the transaction start); an age is never negative, so clamp at zero.
    longest_query_seconds = max(0.0, float(activity[3]))
    longest_query_band = classify_longest_query_age(longest_query_seconds)
    dead_tuples = int(tuples_row[0])
    live_tuples = int(tuples_row[1])
    bloat_band = classify_dead_tuple_ratio(dead_tuples, live_tuples)
    blocked_locks = int(locks[1])
    lock_wait_seconds = max(0.0, float(lock_wait))  # same sub-instant negative as above
    lock_band = classify_longest_query_age(lock_wait_seconds)
    replication_band = classify_replication_lag(None)  # lag is measured on the replica, never on the primary
    bands = {
        "connections": connections_band,
        "longest_query": longest_query_band,
        "bloat": bloat_band,
        "locks": lock_band,
        "replication": replication_band,
    }
    return {
        "reachable": True,
        "database_bytes": int(database_bytes),
        "connections": {
            "total": total_connections,
            "active": int(activity[1]),
            "idle_in_transaction": int(activity[2]),
            "max": max_connections,
            "band": connections_band,
        },
        "longest_query_seconds": longest_query_seconds,
        "longest_query_band": longest_query_band,
        "locks": {
            "total": int(locks[0]),
            "blocked": blocked_locks,
            "oldest_lock_wait_seconds": lock_wait_seconds,
            "band": lock_band,
        },
        "dead_tuples": dead_tuples,
        "live_tuples": live_tuples,
        "dead_tuple_ratio": (dead_tuples / (dead_tuples + live_tuples)) if (dead_tuples + live_tuples) else 0.0,
        "bloat_band": bloat_band,
        "replication": {
            "replicas": int(replicas),
            "lag_seconds": None,
            "band": replication_band,
        },
        "pg_stat_statements_available": bool(stat_statements),
        "overall": summarize_health(bands),
    }
