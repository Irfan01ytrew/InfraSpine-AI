"""Step AG slice 2: migration 0136_hr_leave_attendance roundtrip.

Upgrade to head, downgrade to 0103 (the HR tables disappear), upgrade to head
again, and `alembic check` clean. Run this file alone in its own pytest
process: it moves the shared *_test database through revisions.
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


def _hr_tables() -> set:
    rows = run_db(lambda conn: conn.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name LIKE 'hr_%'"))).all()
    return {row[0] for row in rows}


def test_migration_0104_roundtrip():
    expected = {"hr_leave_policies", "hr_leave_balances", "hr_leave_requests",
                "hr_attendance", "hr_payroll_schedules", "hr_salary_reminders"}
    cfg = _cfg()
    command.upgrade(cfg, "head")
    assert expected <= _hr_tables()
    head = run_db(lambda conn: conn.scalar(text("SELECT version_num FROM alembic_version")))
    assert head == "0136_hr_leave_attendance"

    command.downgrade(cfg, "0103")
    assert not (expected & _hr_tables())
    assert run_db(lambda conn: conn.scalar(text("SELECT version_num FROM alembic_version"))) \
        == "0103_unified_studio_governance"

    command.upgrade(cfg, "head")
    assert expected <= _hr_tables()
    command.check(cfg)
