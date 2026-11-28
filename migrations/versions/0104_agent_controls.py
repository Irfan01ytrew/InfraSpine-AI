"""Step AK (slice 2): unified per-agent control records, audit events, spend ledger.

Revision ID: 0104_agent_controls
Revises: 0103_unified_studio_governance

Note: renumbered at integration time (was 0105_agent_controls on agent/agent-controls;
the linear chain 0104-0132 is recorded in INTEGRATION-SNAPSHOT.md, map v3).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0104_agent_controls"
down_revision: str | None = "0103_unified_studio_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_control_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_kind", sa.String(16), nullable=False, server_default="studio"),
        sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("scope_id", UUID(as_uuid=True), sa.ForeignKey("platform_scopes.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("data_scopes", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("permissions", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("allowed_actions", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("spend_limit_usd", sa.Numeric(19, 4)),
        sa.Column("spend_used_usd", sa.Numeric(19, 4), nullable=False, server_default="0"),
        sa.Column("alert_threshold", sa.Numeric(5, 4), nullable=False, server_default="0.80"),
        sa.Column("max_steps", sa.Integer, nullable=False, server_default="6"),
        sa.Column("delegate_to", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("max_delegation_depth", sa.Integer, nullable=False, server_default="1"),
        sa.Column("killed_at", sa.DateTime(timezone=True)),
        sa.Column("killed_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("kill_reason", sa.String(300)),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active', 'paused', 'disabled', 'killed')",
                           name="ck_agent_control_status_valid"),
        sa.CheckConstraint("agent_kind IN ('studio', 'profile', 'governance')",
                           name="ck_agent_control_kind_valid"),
        sa.CheckConstraint("version > 0", name="ck_agent_control_version_positive"),
        sa.CheckConstraint("max_steps BETWEEN 1 AND 8", name="ck_agent_control_max_steps_valid"),
        sa.CheckConstraint("max_delegation_depth >= 0", name="ck_agent_control_delegation_depth_valid"),
        sa.CheckConstraint("spend_used_usd >= 0", name="ck_agent_control_spend_used_valid"),
        sa.CheckConstraint("spend_limit_usd IS NULL OR spend_limit_usd >= 0",
                           name="ck_agent_control_spend_limit_valid"),
        sa.CheckConstraint("alert_threshold > 0 AND alert_threshold <= 1",
                           name="ck_agent_control_alert_threshold_valid"),
        sa.CheckConstraint("jsonb_typeof(data_scopes) = 'array'", name="ck_agent_control_scopes_are_array"),
        sa.CheckConstraint("jsonb_typeof(permissions) = 'array'", name="ck_agent_control_permissions_are_array"),
        sa.CheckConstraint("jsonb_typeof(allowed_actions) = 'array'", name="ck_agent_control_actions_are_array"),
        sa.CheckConstraint("jsonb_typeof(delegate_to) = 'array'", name="ck_agent_control_delegates_are_array"),
        sa.CheckConstraint(
            "(status = 'killed' AND killed_at IS NOT NULL) OR (status <> 'killed' AND killed_at IS NULL)",
            name="ck_agent_control_kill_details_valid"),
        sa.UniqueConstraint("agent_id", name="uq_agent_control_records_agent_id"),
    )
    op.create_index("ix_agent_control_records_owner_status", "agent_control_records", ["owner_id", "status"])
    op.create_index("ix_agent_control_records_scope_status", "agent_control_records", ["scope_id", "status"])

    op.create_table(
        "agent_control_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("actor_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reason", sa.String(300), nullable=False, server_default=""),
        sa.Column("before", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("after", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_control_events_agent_created", "agent_control_events", ["agent_id", "created_at"])
    op.create_index("ix_agent_control_events_event_created", "agent_control_events", ["event", "created_at"])

    op.create_table(
        "agent_spend_ledger",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False, server_default=""),
        sa.Column("model", sa.String(160), nullable=False, server_default=""),
        sa.Column("tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(19, 4), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("tokens >= 0", name="ck_agent_spend_tokens_valid"),
        sa.CheckConstraint("cost_usd >= 0", name="ck_agent_spend_cost_valid"),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_spend_ledger_idempotency_key"),
    )
    op.create_index("ix_agent_spend_ledger_agent_recorded", "agent_spend_ledger", ["agent_id", "recorded_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM agent_control_records)")):
        raise RuntimeError("Agent control records exist. Delete them (with a verified backup) instead of "
                           "downgrading, so no agent silently loses its kill-switch and spend limits.")
    op.drop_table("agent_spend_ledger")
    op.drop_table("agent_control_events")
    op.drop_table("agent_control_records")
