"""Allow department-scoped delegated capabilities: people.invite, training.manage, agents.manage.

Revision ID: 0069_department_capability_permissions
Revises: 0068_reports_view_permission
"""

from alembic import op


revision = "0069_department_capability_permissions"
down_revision = "0068_reports_view_permission"
branch_labels = None
depends_on = None

_NAME = "ck_permission_grants_permission_valid"
_BEFORE = "permission IN ('knowledge.review', 'knowledge.publish', 'data.steward', 'reports.view')"
_AFTER = (
    "permission IN ('knowledge.review', 'knowledge.publish', 'data.steward', 'reports.view', "
    "'people.invite', 'training.manage', 'agents.manage')"
)


def upgrade() -> None:
    op.drop_constraint(op.f(_NAME), "permission_grants", type_="check")
    op.create_check_constraint(op.f(_NAME), "permission_grants", _AFTER)


def downgrade() -> None:
    # Grants are append-only history; downgrading is refused while any of these grants exists.
    op.drop_constraint(op.f(_NAME), "permission_grants", type_="check")
    op.create_check_constraint(op.f(_NAME), "permission_grants", _BEFORE)
