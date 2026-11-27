"""Record provider receipts separately from bridge submission."""

import sqlalchemy as sa
from alembic import op

revision = "0088_baileys_delivery_receipt"
down_revision = "0087_agent_studio_baileys"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_studio_baileys_messages", sa.Column("receipt_status", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_baileys_receipt_status", "agent_studio_baileys_messages", "receipt_status BETWEEN 2 AND 4",
    )


def downgrade():
    op.drop_constraint("ck_baileys_receipt_status", "agent_studio_baileys_messages", type_="check")
    op.drop_column("agent_studio_baileys_messages", "receipt_status")
