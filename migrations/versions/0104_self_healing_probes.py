"""Step W slices 2–3: self-healing probe snapshots and incident records.

Revision ID: 0104_self_healing_probes
Revises: 0103_unified_studio_governance

NOTE (merge time): revision 0104 is also claimed on other agent branches
(HR, WhatsApp AE, AI freshness, AB, G, AA, AM, Z, R, AF, AL). Renumber at merge.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0104_self_healing_probes"
down_revision: str | None = "0103_unified_studio_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "self_healing_probes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("component", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("age_seconds", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("detail", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('ok', 'degraded', 'down', 'unknown')", name="self_healing_probe_status_valid"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_self_healing_probes_component_recorded",
        "self_healing_probes", ["component", "recorded_at"],
    )
    op.create_table(
        "self_healing_incidents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("component", sa.String(32), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("evidence", sa.String(2000), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'open'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="self_healing_incident_severity_valid",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'remedied', 'escalated')",
            name="self_healing_incident_status_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index(
        "ix_self_healing_incidents_status_updated",
        "self_healing_incidents", ["status", "updated_at"],
    )


def downgrade() -> None:
    # Operational history only (probe snapshots + remedy bookkeeping); dropping it
    # loses history, never business state, so no guard is needed.
    op.drop_index("ix_self_healing_incidents_status_updated", table_name="self_healing_incidents")
    op.drop_table("self_healing_incidents")
    op.drop_index("ix_self_healing_probes_component_recorded", table_name="self_healing_probes")
    op.drop_table("self_healing_probes")
