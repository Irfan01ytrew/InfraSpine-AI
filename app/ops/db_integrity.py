"""Step AO slice 6: data-integrity checks — foreign keys and orphan rows.

Everything here is read-only: the checker measures, it never deletes.
An orphan is a child row whose parent is missing; orphans are
reported, never silently deleted — deleting one is a human-approved
data fix, with the row identified first.

A foreign-key column that is NULL is not an orphan: SQL says a NULL
in any part of a foreign key means "no parent required", so NULL
foreign keys are excluded from the orphan count.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

#: A foreign key Postgres does not trust yet (created NOT VALID and
#: never validated): the database is not enforcing it for old rows.
FK_COLUMNS_SQL = """
SELECT
    con.conname AS constraint_name,
    child.relname AS child_table,
    parent.relname AS parent_table,
    con.convalidated AS validated,
    (SELECT array_agg(a.attname ORDER BY k.ord)
       FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
       JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum) AS child_columns,
    (SELECT array_agg(a.attname ORDER BY k.ord)
       FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord)
       JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum) AS parent_columns
FROM pg_constraint con
JOIN pg_class child ON child.oid = con.conrelid
JOIN pg_class parent ON parent.oid = con.confrelid
JOIN pg_namespace n ON n.oid = child.relnamespace
WHERE con.contype = 'f' AND n.nspname = 'public'
ORDER BY child.relname, con.conname
"""


def integrity_verdict(fk_status: list[dict], orphan_counts: list[dict]) -> dict:
    """Pure verdict over measured FK status and per-FK orphan counts."""
    unvalidated = [fk["constraint_name"] for fk in fk_status if not fk.get("validated")]
    orphans = [
        {"constraint": row["constraint_name"], "orphans": int(row["orphans"])}
        for row in orphan_counts
        if int(row["orphans"]) > 0
    ]
    return {
        "passed": not unvalidated and not orphans,
        "foreign_keys": len(fk_status),
        "unvalidated": unvalidated,
        "orphans": orphans,
        "total_orphans": sum(item["orphans"] for item in orphans),
    }


async def list_foreign_keys(engine: AsyncEngine) -> list[dict]:
    """Every foreign key in the public schema, with its trust flag. Read-only."""
    async with engine.connect() as conn:
        rows = (await conn.execute(text(FK_COLUMNS_SQL))).mappings().all()
    return [
        {
            "constraint_name": row["constraint_name"],
            "child_table": row["child_table"],
            "parent_table": row["parent_table"],
            "validated": bool(row["validated"]),
            "child_columns": list(row["child_columns"] or []),
            "parent_columns": list(row["parent_columns"] or []),
        }
        for row in rows
    ]


def _orphan_count_sql(fk: dict) -> str:
    """Count child rows with no matching parent. Identifiers come from the catalog, quoted here."""
    from app.ops.db_roles import quote_identifier

    child = quote_identifier(fk["child_table"])
    parent = quote_identifier(fk["parent_table"])
    pairs = zip(fk["child_columns"], fk["parent_columns"])
    join_on = " AND ".join(
        f"c.{quote_identifier(c_col)} = p.{quote_identifier(p_col)}" for c_col, p_col in pairs
    )
    not_null = " AND ".join(f"c.{quote_identifier(c_col)} IS NOT NULL" for c_col in fk["child_columns"])
    return (
        f"SELECT count(*) FROM {child} c "
        f"WHERE {not_null} AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {join_on})"
    )


async def count_orphans(engine: AsyncEngine, fk: dict) -> int:
    """Orphan rows for one foreign key. Read-only; NULL foreign keys are not orphans."""
    async with engine.connect() as conn:
        return int(await conn.scalar(text(_orphan_count_sql(fk))))


async def check_integrity(engine: AsyncEngine) -> dict:
    """Full integrity verdict: every FK's trust flag + orphan count. Read-only.

    One count query per foreign key; on a large database this takes
    seconds, which is why the admin page runs it on demand rather
    than on every page load of the app.
    """
    fk_status = await list_foreign_keys(engine)
    orphan_counts = [
        {"constraint_name": fk["constraint_name"], "orphans": await count_orphans(engine, fk)}
        for fk in fk_status
    ]
    return {
        **integrity_verdict(fk_status, orphan_counts),
        "checked_at": datetime.now(UTC).isoformat(),
    }
