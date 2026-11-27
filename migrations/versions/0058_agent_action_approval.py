"""Add agent action proposals with independent human approval and idempotent execution.

Revision ID: 0058_agent_action_approval
Revises: 0057_org_mfa_session_proof
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0058_agent_action_approval"
down_revision = "0057_org_mfa_session_proof"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_action_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("agent_profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_profiles.id", ondelete="SET NULL")),
        sa.Column("requester_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("access_groups.id", ondelete="SET NULL")),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rationale", sa.String(1000), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending_approval", nullable=False),
        sa.Column("approver_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("approval_rationale", sa.String(1000)),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("execution_result", postgresql.JSONB(astext_type=sa.Text())),
        sa.CheckConstraint(
            "status IN ('pending_approval', 'approved', 'rejected', 'expired', 'executed')",
            name=op.f("ck_agent_action_status_valid"),
        ),
        sa.CheckConstraint(
            "action_type IN ('webhook_dispatch', 'document_publish', 'notification_broadcast')",
            name=op.f("ck_agent_action_type_valid"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(rationale)) BETWEEN 3 AND 1000",
            name=op.f("ck_agent_action_rationale_valid"),
        ),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_agent_action_proposals_idempotency_key")),
    )
    op.create_index(
        "ix_agent_action_proposals_status_created",
        "agent_action_proposals",
        ["status", "requested_at"],
    )
    op.create_index(
        "ix_agent_action_proposals_dept_status",
        "agent_action_proposals",
        ["department_id", "status"],
    )
    op.create_index(
        "ix_agent_action_proposals_requester",
        "agent_action_proposals",
        ["requester_user_id", "status"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_records = bind.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM agent_action_proposals)")
    )
    if has_records:
        raise RuntimeError("Agent action proposal history is retained; downgrade requires a separately reviewed maintenance plan.")
    op.drop_index("ix_agent_action_proposals_requester", table_name="agent_action_proposals")
    op.drop_index("ix_agent_action_proposals_dept_status", table_name="agent_action_proposals")
    op.drop_index("ix_agent_action_proposals_status_created", table_name="agent_action_proposals")
    op.drop_table("agent_action_proposals")
