"""Allow the delegated, read-only organization usage reports permission (reports.view).

Revision ID: 0068_reports_view_permission
Revises: 0067_agent_run_content_retention
"""

from alembic import op


revision = "0068_reports_view_permission"
down_revision = "0067_agent_run_content_retention"
branch_labels = None
depends_on = None

_NAME = "ck_permission_grants_permission_valid"
_BEFORE = "permission IN ('knowledge.review', 'knowledge.publish', 'data.steward')"
_AFTER = "permission IN ('knowledge.review', 'knowledge.publish', 'data.steward', 'reports.view')"


def upgrade() -> None:
    op.drop_constraint(op.f(_NAME), "permission_grants", type_="check")
    op.create_check_constraint(op.f(_NAME), "permission_grants", _AFTER)


def downgrade() -> None:
    # Grants are append-only history; downgrading is refused while any reports.view grant exists.
    op.drop_constraint(op.f(_NAME), "permission_grants", type_="check")
    op.create_check_constraint(op.f(_NAME), "permission_grants", _BEFORE)
