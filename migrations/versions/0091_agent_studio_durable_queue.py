"""Add durable queue, idempotency and measured usage to Agent Studio runs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0091_agent_studio_durable_queue"
down_revision = "0090_merge_module_heads"
branch_labels = None
depends_on = None


def upgrade():
    # Isolated-schema runs (INFASPINE_TEST_SCHEMA) set search_path to
    # "<test schema>,public". A schema-less has_table() uses
    # pg_table_is_visible, so it sees public's table and wrongly skips
    # creating the table in the test schema; 0092's unqualified ALTER TABLE
    # then hits public's table and fails with DuplicateColumnError. Check
    # the current schema explicitly so the table is created where the
    # migration is actually running.
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    if not sa.inspect(bind).has_table("integration_whatsapp_dispatches", schema=schema):
        op.create_table(
            "integration_whatsapp_dispatches",
            sa.Column("inbound_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("phase", sa.String(length=24), server_default=sa.text("'waiting'"), nullable=False),
            sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("incoming_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("provider_message_id", sa.String(length=160), nullable=True),
            sa.Column("delivery_status", sa.String(length=24), nullable=True),
            sa.Column("receipt_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["inbound_id"], ["agent_studio_whatsapp_inbound.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("inbound_id"),
            sa.UniqueConstraint(
                "provider_message_id",
                name="uq_integration_whatsapp_dispatches_provider_message_id",
            ),
        )

    op.drop_constraint("ck_agent_studio_runs_status_valid", "agent_studio_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_studio_runs_status_valid",
        "agent_studio_runs",
        "status IN ('queued', 'running', 'awaiting_approval', 'completed', 'failed', 'cancelled')",
    )
    op.alter_column("agent_studio_runs", "status", server_default=sa.text("'queued'"))
    op.alter_column("agent_studio_runs", "started_at", server_default=None, nullable=True)

    op.add_column("agent_studio_runs", sa.Column("idempotency_key", sa.String(length=64), nullable=True))
    op.add_column("agent_studio_runs", sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column("agent_studio_runs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.add_column("agent_studio_runs", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agent_studio_runs", sa.Column("provider", sa.String(length=80), nullable=True))
    op.add_column("agent_studio_runs", sa.Column("model", sa.String(length=160), nullable=True))
    op.add_column("agent_studio_runs", sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column("agent_studio_runs", sa.Column("completion_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column("agent_studio_runs", sa.Column("llm_calls", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column("agent_studio_runs", sa.Column("estimated_cost_usd", sa.Numeric(18, 8), nullable=True))
    op.add_column("agent_studio_runs", sa.Column("usage_by_model", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.create_check_constraint(
        "ck_agent_studio_runs_attempt_count_nonnegative",
        "agent_studio_runs",
        "attempt_count >= 0",
    )
    op.create_check_constraint(
        "ck_agent_studio_runs_usage_nonnegative",
        "agent_studio_runs",
        "prompt_tokens >= 0 AND completion_tokens >= 0 AND llm_calls >= 0",
    )
    op.create_index(
        "uq_agent_studio_runs_owner_id",
        "agent_studio_runs",
        ["owner_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_agent_studio_runs_queue",
        "agent_studio_runs",
        ["status", "next_attempt_at", "created_at"],
    )


def downgrade():
    op.execute("UPDATE agent_studio_runs SET status='failed' WHERE status IN ('queued', 'cancelled')")
    op.drop_index("ix_agent_studio_runs_queue", table_name="agent_studio_runs")
    op.drop_index("uq_agent_studio_runs_owner_id", table_name="agent_studio_runs")
    op.drop_constraint("ck_agent_studio_runs_usage_nonnegative", "agent_studio_runs", type_="check")
    op.drop_constraint("ck_agent_studio_runs_attempt_count_nonnegative", "agent_studio_runs", type_="check")
    op.drop_column("agent_studio_runs", "estimated_cost_usd")
    op.drop_column("agent_studio_runs", "usage_by_model")
    op.drop_column("agent_studio_runs", "llm_calls")
    op.drop_column("agent_studio_runs", "completion_tokens")
    op.drop_column("agent_studio_runs", "prompt_tokens")
    op.drop_column("agent_studio_runs", "model")
    op.drop_column("agent_studio_runs", "provider")
    op.drop_column("agent_studio_runs", "claimed_at")
    op.drop_column("agent_studio_runs", "next_attempt_at")
    op.drop_column("agent_studio_runs", "attempt_count")
    op.drop_column("agent_studio_runs", "idempotency_key")
    op.alter_column("agent_studio_runs", "started_at", server_default=sa.func.now(), nullable=False)
    op.alter_column("agent_studio_runs", "status", server_default=sa.text("'running'"))
    op.drop_constraint("ck_agent_studio_runs_status_valid", "agent_studio_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_studio_runs_status_valid",
        "agent_studio_runs",
        "status IN ('running', 'awaiting_approval', 'completed', 'failed')",
    )
    # upgrade() creates the dispatch table when it is absent, so a downgrade below this revision must remove
    # it as well; otherwise the FK to agent_studio_whatsapp_inbound blocks downgrading 0086. Take a database
    # backup before any downgrade if WhatsApp delivery evidence must be retained.
    op.execute("DROP TABLE IF EXISTS integration_whatsapp_dispatches")
