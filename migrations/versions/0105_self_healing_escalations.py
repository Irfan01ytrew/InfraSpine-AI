"""Step W slice 4: owner-facing escalation delivery records.

Revision ID: 0105_self_healing_escalations
Revises: 0104_self_healing_probes

NOTE (merge time): revision 0105 is also claimed on other agent branches
(AK, AI, AB, AA, G). Renumber at merge.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0105_self_healing_escalations"
down_revision: str | None = "0104_self_healing_probes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "self_healing_escalations",
        sa.Column("id", UUID(), nullable=False),
        sa.Column("incident_key", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("component", sa.String(32), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("evidence", sa.String(2000), nullable=False, server_default=sa.text("''")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("channel", sa.String(16), nullable=False, server_default=sa.text("'admin_ui'")),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="self_healing_escalation_severity_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("incident_key"),
    )
    op.create_index(
        "ix_self_healing_escalations_acknowledged",
        "self_healing_escalations", ["acknowledged_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_self_healing_escalations_acknowledged", table_name="self_healing_escalations")
    op.drop_table("self_healing_escalations")
