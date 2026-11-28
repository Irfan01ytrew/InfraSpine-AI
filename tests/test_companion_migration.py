"""Alembic roundtrip for the Companion OS migration 0104 (Step AN, slice 2).

Upgrade 0103 -> 0104 -> downgrade to 0103 -> upgrade to 0104, then
``alembic check``. The tables only exist after 0104 and are fully removed
by the downgrade. Runs against the dedicated *_test database from conftest.
"""

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests.conftest import TEST_DATABASE_URL, run_db


def _cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["configure_logger"] = False
    return cfg


def _has_table(name: str) -> bool:
    return run_db(
        lambda conn: conn.scalar(
            text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name=:name)"),
            {"name": name},
        )
    )


def test_migration_0104_companion_os_roundtrip():
    tables = [
        "companion_projects",
        "companion_goals",
        "companion_tasks",
        "companion_preferences",
        "companion_progress",
        "companion_consents",
        "companion_digest_outbox",
    ]
    cfg = _cfg()
    command.downgrade(cfg, "0103_unified_studio_governance")
    assert not any(_has_table(name) for name in tables)
    command.upgrade(cfg, "0104_companion_os")
    assert all(_has_table(name) for name in tables)
    command.downgrade(cfg, "0103_unified_studio_governance")
    assert not any(_has_table(name) for name in tables)
    command.upgrade(cfg, "head")
    assert all(_has_table(name) for name in tables)
    command.check(cfg)
