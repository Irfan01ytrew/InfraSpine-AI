"""Add separate Agent Studio pause state."""

from alembic import op
import sqlalchemy as sa


revision = "0095_agent_studio_pause"
down_revision = "0094_qa_retry_history"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_studio_agents",
        sa.Column("paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade():
    op.drop_column("agent_studio_agents", "paused")
