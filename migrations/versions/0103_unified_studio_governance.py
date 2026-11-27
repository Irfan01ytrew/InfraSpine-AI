"""Step E completion: Agent Studio runs and agent action proposals on the unified runtime (definition origins
`studio` and `governance`), and the admin choice of payment connector.

Revision ID: 0103_unified_studio_governance
Revises: 0102_finance_invoices
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0103_unified_studio_governance"
down_revision: str | None = "0102_finance_invoices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_ORIGINS = "origin IN ('workflow','automation','system')"
NEW_ORIGINS = "origin IN ('workflow','automation','system','studio','governance')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", type_="check")
    op.create_check_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", NEW_ORIGINS)
    op.add_column("org_settings", sa.Column("finance_payment_connector", sa.String(16), nullable=True))
    op.create_check_constraint(
        op.f("ck_org_settings_finance_connector_valid"), "org_settings",
        "finance_payment_connector IS NULL OR finance_payment_connector IN ('mock','http','http+mock')")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM wf_runs r JOIN wf_definitions d ON d.id = r.workflow_id "
                           "WHERE d.origin IN ('studio','governance'))")):
        raise RuntimeError("Agent Studio or governance runs exist on the unified runtime. Restore a verified backup "
                           "instead of downgrading.")
    op.execute("ALTER TABLE wf_versions DISABLE TRIGGER wf_versions_immutable")
    op.execute("DELETE FROM wf_versions WHERE workflow_id IN "
               "(SELECT id FROM wf_definitions WHERE origin IN ('studio','governance'))")
    op.execute("ALTER TABLE wf_versions ENABLE TRIGGER wf_versions_immutable")
    op.execute("DELETE FROM wf_definitions WHERE origin IN ('studio','governance')")
    op.drop_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", type_="check")
    op.create_check_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", OLD_ORIGINS)
    op.drop_constraint(op.f("ck_org_settings_finance_connector_valid"), "org_settings", type_="check")
    op.drop_column("org_settings", "finance_payment_connector")
