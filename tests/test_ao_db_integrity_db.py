"""DB tests for Step AO slice 6: integrity checker on a throwaway cluster.

Run with --noconftest: these tests build their own engines. Nothing here
touches the live Codespace database or the owner's PC database.

The test database holds the full app schema (migrated by earlier AO
tests), so list_foreign_keys exercises real constraints. Orphan counts
run one count query per foreign key; the tables are small, so the full
check stays fast.
"""

import asyncio
import os

import pytest
from sqlalchemy import text

from app.db.session import create_engine
from app.ops.db_integrity import check_integrity, count_orphans, list_foreign_keys

TEST_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL is not set")


def test_foreign_keys_are_listed_with_trust_flags():
    async def _body():
        engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)
        try:
            fks = await list_foreign_keys(engine)
            return fks
        finally:
            await engine.dispose()

    fks = asyncio.run(_body())
    assert len(fks) > 50  # the full app schema has well over fifty foreign keys
    assert all(fk["child_columns"] and fk["parent_columns"] for fk in fks)
    assert all(len(fk["child_columns"]) == len(fk["parent_columns"]) for fk in fks)
    unvalidated = [fk["constraint_name"] for fk in fks if not fk["validated"]]
    assert unvalidated == []  # no NOT VALID constraint is left unvalidated


def test_no_orphans_in_the_throwaway_schema():
    async def _body():
        engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)
        try:
            verdict = await check_integrity(engine)
            return verdict
        finally:
            await engine.dispose()

    verdict = asyncio.run(_body())
    assert verdict["passed"] is True
    assert verdict["foreign_keys"] > 50
    assert verdict["total_orphans"] == 0
    assert verdict["checked_at"] is not None


def test_count_orphans_finds_a_planted_orphan_and_ignores_nulls():
    async def _body():
        engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE IF EXISTS ao_child CASCADE"))
                await conn.execute(text("DROP TABLE IF EXISTS ao_parent CASCADE"))
                await conn.execute(text("CREATE TABLE ao_parent (id integer PRIMARY KEY)"))
                # No foreign key yet: the orphan row cannot be inserted
                # while the constraint is enforced.
                await conn.execute(
                    text("CREATE TABLE ao_child (id integer PRIMARY KEY, parent_id integer)")
                )
                await conn.execute(text("INSERT INTO ao_parent VALUES (1)"))
                # One real orphan (parent 99 does not exist), one NULL (not an orphan),
                # one valid row.
                await conn.execute(text("INSERT INTO ao_child VALUES (1, 99), (2, NULL), (3, 1)"))
                # NOT VALID adds the constraint without checking existing rows,
                # so the orphan survives — and the checker must report both
                # the orphan and the unvalidated constraint.
                await conn.execute(
                    text(
                        "ALTER TABLE ao_child ADD CONSTRAINT ao_child_parent_fk "
                        "FOREIGN KEY (parent_id) REFERENCES ao_parent(id) NOT VALID"
                    )
                )
            fks = [fk for fk in await list_foreign_keys(engine) if fk["child_table"] == "ao_child"]
            assert len(fks) == 1
            assert fks[0]["validated"] is False
            return await count_orphans(engine, fks[0])
        finally:
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE IF EXISTS ao_child CASCADE"))
                await conn.execute(text("DROP TABLE IF EXISTS ao_parent CASCADE"))
            await engine.dispose()

    assert asyncio.run(_body()) == 1
