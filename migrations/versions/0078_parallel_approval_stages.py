"""Parallel approval stages: steps decided at the same time by all or any of their approvers.

Revision ID: 0078_parallel_approval_stages
Revises: 0077_approval_escalation_email
"""

from alembic import op
import sqlalchemy as sa


revision = "0078_parallel_approval_stages"
down_revision = "0077_approval_escalation_email"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("approval_chain_steps", sa.Column("parallel_with_previous", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("approval_chain_steps", sa.Column("stage_mode", sa.String(8), nullable=False, server_default=sa.text("'all'")))
    op.create_check_constraint(op.f("ck_approval_chain_steps_stage_mode_valid"), "approval_chain_steps", "stage_mode IN ('all', 'any')")
    op.add_column("request_approval_steps", sa.Column("stage", sa.Integer(), nullable=False, server_default=sa.text("1")))
    op.add_column("request_approval_steps", sa.Column("stage_mode", sa.String(8), nullable=False, server_default=sa.text("'all'")))
    op.create_check_constraint(op.f("ck_request_approval_steps_stage_mode_valid"), "request_approval_steps", "stage_mode IN ('all', 'any')")
    # Existing requests were strictly sequential: every step is its own stage.
    op.execute("UPDATE request_approval_steps SET stage = step_order")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_request_approval_steps_stage_mode_valid"), "request_approval_steps", type_="check")
    op.drop_column("request_approval_steps", "stage_mode")
    op.drop_column("request_approval_steps", "stage")
    op.drop_constraint(op.f("ck_approval_chain_steps_stage_mode_valid"), "approval_chain_steps", type_="check")
    op.drop_column("approval_chain_steps", "stage_mode")
    op.drop_column("approval_chain_steps", "parallel_with_previous")
