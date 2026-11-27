"""Add external partner workspaces and isolated identity/session lifecycle.

Revision ID: 0049_partner_identity_boundary
Revises: 0048_financial_form_review
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0049_partner_identity_boundary"
down_revision = "0048_financial_form_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "partner_workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'suspended')", name="ck_partner_workspaces_status_valid"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_partner_workspaces_slug", "partner_workspaces", ["slug"], unique=True)

    op.create_table(
        "partner_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("failed_login_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("failed_login_attempts >= 0", name="ck_partner_identities_failed_login_attempts_nonnegative"),
        sa.ForeignKeyConstraint(["workspace_id"], ["partner_workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "email", name="uq_partner_identities_workspace_email"),
    )
    op.create_index(
        "ix_partner_identities_workspace_active", "partner_identities", ["workspace_id", "is_active"]
    )

    op.create_table(
        "partner_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("expires_at > created_at", name="ck_partner_invitations_expiry_after_creation"),
        sa.CheckConstraint("length(token_hash) = 64", name="ck_partner_invitations_token_hash_length"),
        sa.CheckConstraint(
            "(accepted_at IS NULL) OR (revoked_at IS NULL)",
            name="ck_partner_invitations_not_accepted_and_revoked",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["partner_workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_partner_invitations_token_hash"),
    )
    op.create_index(
        "ix_partner_invitations_workspace_email", "partner_invitations", ["workspace_id", "email"]
    )
    op.create_index("ix_partner_invitations_expires_at", "partner_invitations", ["expires_at"])
    op.create_index(
        "uq_partner_invitations_active_email", "partner_invitations", ["workspace_id", "email"],
        unique=True, postgresql_where=sa.text("accepted_at IS NULL AND revoked_at IS NULL"),
    )

    op.create_table(
        "partner_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["identity_id"], ["partner_identities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_partner_sessions_token_hash"),
    )
    op.create_index("ix_partner_sessions_identity_id", "partner_sessions", ["identity_id"])
    op.create_index("ix_partner_sessions_identity_expiry", "partner_sessions", ["identity_id", "expires_at"])

    op.create_table(
        "partner_knowledge_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "search_vector", postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', content)", persisted=True), nullable=False,
        ),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_partner_knowledge_sources_status_valid"),
        sa.CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="ck_partner_knowledge_sources_title_valid"),
        sa.CheckConstraint("length(content) BETWEEN 1 AND 50000", name="ck_partner_knowledge_sources_content_valid"),
        sa.CheckConstraint("length(content_sha256) = 64", name="ck_partner_knowledge_sources_content_sha256_length"),
        sa.ForeignKeyConstraint(["workspace_id"], ["partner_workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_partner_knowledge_sources_workspace_status", "partner_knowledge_sources", ["workspace_id", "status"]
    )
    op.create_index(
        "ix_partner_knowledge_sources_search", "partner_knowledge_sources", ["search_vector"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_partner_knowledge_sources_search", table_name="partner_knowledge_sources")
    op.drop_index("ix_partner_knowledge_sources_workspace_status", table_name="partner_knowledge_sources")
    op.drop_table("partner_knowledge_sources")
    op.drop_index("ix_partner_sessions_identity_expiry", table_name="partner_sessions")
    op.drop_index("ix_partner_sessions_identity_id", table_name="partner_sessions")
    op.drop_table("partner_sessions")
    op.drop_index("uq_partner_invitations_active_email", table_name="partner_invitations")
    op.drop_index("ix_partner_invitations_expires_at", table_name="partner_invitations")
    op.drop_index("ix_partner_invitations_workspace_email", table_name="partner_invitations")
    op.drop_table("partner_invitations")
    op.drop_index("ix_partner_identities_workspace_active", table_name="partner_identities")
    op.drop_table("partner_identities")
    op.drop_index("uq_partner_workspaces_slug", table_name="partner_workspaces")
    op.drop_table("partner_workspaces")
