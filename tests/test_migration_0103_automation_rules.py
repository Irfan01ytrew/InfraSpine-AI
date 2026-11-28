"""Gap 3: legacy automation rules are mirrored as workflow definitions by migration 0101,
and the 0103 origin widening + payment-connector column round-trip cleanly (dedicated *_test DB).

Flow: start at head -> downgrade to 0100 -> seed 4 legacy automation rules (2 with a creator,
2 without) -> upgrade to 0103 -> exactly the 2 creator rules are mirrored as `origin='automation'`
definitions with published versions -> downgrade to 0100 -> mirrors are gone, the seed rules survive.
"""
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, run_db

REVISION_0100 = "0100_platform_membership_backfill"
REVISION_0103 = "0103_unified_studio_governance"


def _cfg():
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["configure_logger"] = False
    return cfg


def _current_revision():
    rows = run_db(lambda c: c.execute(text("SELECT version_num FROM alembic_version"))).all()
    assert len(rows) == 1, f"expected a single alembic version row, got {rows!r}"
    return rows[0].version_num


def _mirrors():
    return run_db(lambda c: c.execute(text(
        "SELECT origin_ref, name FROM wf_definitions WHERE origin = 'automation' ORDER BY origin_ref"
    ))).all()


def _mirror_version_count():
    return run_db(lambda c: c.execute(text(
        "SELECT count(*) FROM wf_versions v JOIN wf_definitions d ON d.id = v.workflow_id "
        "WHERE d.origin = 'automation' AND v.status = 'published'"
    ))).scalar_one()


def _column_exists(table, column):
    return run_db(lambda c: c.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = :table AND column_name = :column)"
    ), {"table": table, "column": column})).scalar_one()


def _seed_rules(group_id, creator_id):
    """Four legacy rules: two with a creator (must be mirrored), two without (must not)."""
    keys = [uuid.uuid4() for _ in range(4)]

    def insert(key, name, creator):
        run_db(lambda c: c.execute(text(
            "INSERT INTO automation_workflows (id, definition_key, version, name, department_group_id, "
            "trigger_type, action_type, action_config, enabled, created_by_user_id) "
            "VALUES (:id, :key, 1, :name, :group, 'manual', 'create_task', '{}'::jsonb, true, :creator)"
        ), {"id": uuid.uuid4(), "key": key, "name": name, "group": group_id, "creator": creator}))

    insert(keys[0], "Gap 3 rule with creator A", creator_id)
    insert(keys[1], "Gap 3 rule with creator B", creator_id)
    insert(keys[2], "Gap 3 rule without creator C", None)
    insert(keys[3], "Gap 3 rule without creator D", None)
    return keys


def test_automation_rules_mirrored_on_upgrade_and_removed_on_downgrade():
    cfg = _cfg()
    assert _current_revision() == REVISION_0103
    try:
        command.downgrade(cfg, REVISION_0100)
        assert _current_revision() == REVISION_0100

        group_id = uuid.uuid4()
        run_db(lambda c: c.execute(text(
            "INSERT INTO access_groups (id, name, kind) VALUES (:id, 'Gap 3 test department', 'department')"
        ), {"id": group_id}))
        creator_id = run_db(lambda c: c.execute(
            text("SELECT id FROM users WHERE email = :email"), {"email": ADMIN_EMAIL}
        )).scalar_one()
        keys = _seed_rules(group_id, creator_id)
        assert run_db(lambda c: c.execute(
            text("SELECT count(*) FROM automation_workflows"))).scalar_one() == 4

        command.upgrade(cfg, REVISION_0103)
        assert _current_revision() == REVISION_0103

        mirrors = _mirrors()
        assert len(mirrors) == 2, f"expected exactly 2 automation mirrors, got {len(mirrors)}: {mirrors!r}"
        assert {row.origin_ref for row in mirrors} == {str(keys[0]), str(keys[1])}
        assert {row.name for row in mirrors} == {
            "Gap 3 rule with creator A", "Gap 3 rule with creator B"}
        assert _mirror_version_count() == 2
        # 0103's own change: the payment-connector column exists after the upgrade.
        assert _column_exists("org_settings", "finance_payment_connector")

        command.downgrade(cfg, REVISION_0100)
        assert _current_revision() == REVISION_0100
        # The 0101 downgrade removes every non-workflow definition; the mirror's
        # origin column itself is dropped with it.
        assert not _column_exists("wf_definitions", "origin")
        assert not _column_exists("org_settings", "finance_payment_connector")
        # The legacy seed rules survive the round-trip untouched.
        assert run_db(lambda c: c.execute(
            text("SELECT count(*) FROM automation_workflows"))).scalar_one() == 4
    finally:
        command.upgrade(cfg, "head")
    assert _current_revision() == REVISION_0103


def test_0103_payment_connector_check_constraint_exists():
    cfg = _cfg()
    assert _current_revision() == REVISION_0103
    assert _column_exists("org_settings", "finance_payment_connector")
    check = run_db(lambda c: c.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'org_settings'::regclass AND conname = 'ck_org_settings_finance_connector_valid'"
    ))).scalar_one_or_none()
    assert check is not None, "0103 check constraint ck_org_settings_finance_connector_valid is missing"
    assert "'mock'" in check and "'http'" in check and "'http+mock'" in check
