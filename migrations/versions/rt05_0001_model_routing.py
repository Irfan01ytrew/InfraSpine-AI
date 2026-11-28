"""Step T slice 3: routing telemetry table for the smart model router.

Revision ID: rt05_0001_model_routing
Revises: 0103_unified_studio_governance

The revision id follows the feature-branch convention (wf05, bc08, pi04)
instead of the next 0104 number: parallel branches are already using
0104/0105, so a distinct id avoids a revision collision. At merge time
this revision must be rebased onto the merge-time head if 0103 is no
longer the head.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "rt05_0001_model_routing"
down_revision: str | None = "0103_unified_studio_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_routing_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_class", sa.String(length=32), nullable=False),
        sa.Column("policy", sa.String(length=16), server_default=sa.text("'cheapest'"), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("alternates", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("alternates_tried", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'ok'"), nullable=False),
        sa.Column("budget_action", sa.String(length=16), server_default=sa.text("'allow'"), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("actual_cost_usd", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('ok', 'downgraded', 'refused', 'error')", name="status_valid"),
        sa.CheckConstraint("budget_action IN ('allow', 'downgrade', 'refuse')", name="budget_action_valid"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_model_routing_records_user_id_users"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_routing_records")),
    )
    op.create_index(op.f("ix_model_routing_records_created"), "model_routing_records", ["created_at"], unique=False)
    op.create_index(op.f("ix_model_routing_records_task_created"), "model_routing_records", ["task_class", "created_at"], unique=False)
    op.create_index(op.f("ix_model_routing_records_department_created"), "model_routing_records", ["department_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_model_routing_records_department_created"), table_name="model_routing_records")
    op.drop_index(op.f("ix_model_routing_records_task_created"), table_name="model_routing_records")
    op.drop_index(op.f("ix_model_routing_records_created"), table_name="model_routing_records")
    op.drop_table("model_routing_records")
