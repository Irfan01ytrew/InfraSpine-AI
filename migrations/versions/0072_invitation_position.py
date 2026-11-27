"""Let an invitation carry a position and a manager that are applied when the invitee signs up.

Revision ID: 0072_invitation_position
Revises: 0071_positions_and_reporting
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0072_invitation_position"
down_revision = "0071_positions_and_reporting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_invitations", sa.Column(
        "position_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("positions.id", ondelete="SET NULL", name=op.f("fk_user_invitations_position_id_positions")), nullable=True,
    ))
    op.add_column("user_invitations", sa.Column(
        "reports_to_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL", name=op.f("fk_user_invitations_reports_to_id_users")), nullable=True,
    ))


def downgrade() -> None:
    op.drop_column("user_invitations", "reports_to_id")
    op.drop_column("user_invitations", "position_id")
