"""Add encrypted per-user TOTP factors, recovery digests and login challenges."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0052_user_totp_mfa"
down_revision = "0051_reviewed_agent_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_mfa_factors",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_totp_counter", sa.BigInteger(), server_default=sa.text("-1"), nullable=False),
        sa.CheckConstraint("last_totp_counter >= -1", name="last_counter_valid"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "user_mfa_recovery_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "code_hash", name="uq_user_mfa_recovery_digest"),
    )
    op.create_index("ix_user_mfa_recovery_unused", "user_mfa_recovery_codes", ["user_id", "used_at"])
    op.create_table(
        "user_mfa_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempts BETWEEN 0 AND 5", name="attempts_bounded"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_user_mfa_challenges_user_expiry", "user_mfa_challenges", ["user_id", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_user_mfa_challenges_user_expiry", table_name="user_mfa_challenges")
    op.drop_table("user_mfa_challenges")
    op.drop_index("ix_user_mfa_recovery_unused", table_name="user_mfa_recovery_codes")
    op.drop_table("user_mfa_recovery_codes")
    op.drop_table("user_mfa_factors")
