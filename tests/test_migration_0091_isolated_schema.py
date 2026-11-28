"""Migration 0091 must create the WhatsApp dispatch table in the schema the
upgrade is actually running in, not skip it because public already has one.

Regression test for the blocker Agent 32 diagnosed: 0091 guarded table
creation with a schema-less ``has_table()``, which under SQLAlchemy's
PostgreSQL dialect uses ``pg_table_is_visible`` — so in an isolated-schema
run (search_path ``<test schema>,public``) it saw public's table, skipped
creating the table in the test schema, and 0092's unqualified ``ALTER TABLE``
then hit public's table and failed with ``DuplicateColumnError``, rolling
back the whole upgrade.

The test runs the real upgrade 0001 -> 0092 inside a fresh isolated schema
while public already holds the table (the conftest database is migrated to
head in public), and proves the dispatch table — including 0092's progress
columns — lands in the test schema while public is left untouched.
Dedicated ``*_test`` database only; the throwaway schema is dropped after.
"""

import secrets

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests.conftest import TEST_DATABASE_URL, run_db

TARGET_REVISION = "0092_whatsapp_progress_receipt"


def _schema_exists(schema: str) -> bool:
    return run_db(lambda c: c.execute(
        text("SELECT count(*) FROM information_schema.schemata WHERE schema_name = :schema"),
        {"schema": schema},
    )).scalar() == 1


def _table_columns(schema: str, table: str) -> set[str]:
    rows = run_db(lambda c: c.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table"
        ),
        {"schema": schema, "table": table},
    )).all()
    return {row.column_name for row in rows}


def _version(schema: str) -> str | None:
    return run_db(lambda c: c.execute(
        text(f'SELECT version_num FROM "{schema}".alembic_version'),
    )).scalar()


def test_0091_creates_dispatch_table_in_isolated_schema(monkeypatch):
    # Precondition: the shared trigger — public already has the table with
    # 0092's progress columns (conftest migrates public to head).
    public_columns = _table_columns("public", "integration_whatsapp_dispatches")
    assert {"progress_message_id", "progress_status", "progress_at"} <= public_columns

    schema = "infraspine_test_" + secrets.token_hex(6)
    monkeypatch.setenv("INFASPINE_TEST_SCHEMA", schema)
    try:
        run_db(lambda c: c.execute(text(f'CREATE SCHEMA "{schema}"')))
        cfg = Config("alembic.ini")
        cfg.attributes["database_url"] = TEST_DATABASE_URL
        cfg.attributes["configure_logger"] = False
        # Full chain 0001 -> 0092 inside the fresh isolated schema. Before
        # the 0091 fix this failed at 0092 with DuplicateColumnError.
        command.upgrade(cfg, TARGET_REVISION)

        assert _version(schema) == TARGET_REVISION
        test_columns = _table_columns(schema, "integration_whatsapp_dispatches")
        assert {
            "inbound_id", "phase", "claim_token", "incoming_at",
            "provider_message_id", "delivery_status", "receipt_at",
            "progress_message_id", "progress_status", "progress_at",
        } <= test_columns
        # Public was not touched by the isolated-schema upgrade.
        assert _table_columns("public", "integration_whatsapp_dispatches") == public_columns
    finally:
        run_db(lambda c: c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')))
        assert not _schema_exists(schema)
