"""Let an invitation carry a custom role that is assigned when the invitee signs up.

Revision ID: 0070_invitation_custom_role
Revises: 0069_department_capability_permissions
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0070_invitation_custom_role"
down_revision = "0069_department_capability_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_invitations",
        sa.Column(
            "role_template_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("role_templates.id", ondelete="SET NULL", name=op.f("fk_user_invitations_role_template_id_role_templates")),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("user_invitations", "role_template_id")
