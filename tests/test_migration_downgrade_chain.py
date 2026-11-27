"""Rollback safety: Agent Studio/WhatsApp revisions downgrade cleanly and upgrade back to head (dedicated *_test DB)."""

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests.conftest import TEST_DATABASE_URL, run_db


def _tables():
    rows = run_db(lambda c: c.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    ))).all()
    return {row.table_name for row in rows}


def test_whatsapp_dispatch_table_does_not_block_downgrading_below_agent_studio_whatsapp():
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["configure_logger"] = False
    assert {"integration_whatsapp_dispatches", "agent_studio_whatsapp_inbound"} <= _tables()
    try:
        # Crosses 0091 (creates the dispatch table) and 0086 (creates the inbound table it references).
        command.downgrade(cfg, "0085_independent_agent_studio")
        remaining = _tables()
        assert "integration_whatsapp_dispatches" not in remaining
        assert "agent_studio_whatsapp_inbound" not in remaining
    finally:
        command.upgrade(cfg, "head")
    assert {"integration_whatsapp_dispatches", "agent_studio_whatsapp_inbound"} <= _tables()
