"""Step AM slice 6: Foundry registry tables (apps, builds, feedback).

Revision ID: 0105_foundry_registry
Revises: 0104_agent_controls

Renumbered at integration time (was 0104_foundry_registry on
agent/software-foundry; the linear chain 0104-0132 is recorded in
INTEGRATION-SNAPSHOT.md, map v3).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0105_foundry_registry"
down_revision: str | None = "0104_agent_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "foundry_apps",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("scope_id", UUID(as_uuid=True),
                  sa.ForeignKey("platform_scopes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("scope_id", "slug", name="uq_foundry_apps_scope_slug"),
    )
    op.create_index("ix_foundry_apps_scope_id", "foundry_apps", ["scope_id"])

    op.create_table(
        "foundry_builds",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("app_id", UUID(as_uuid=True),
                  sa.ForeignKey("foundry_apps.id", ondelete="CASCADE"), nullable=False),
        sa.Column("spec_version", sa.Integer(), nullable=False),
        sa.Column("spec_fingerprint", sa.String(64), nullable=False),
        sa.Column("build_fingerprint", sa.String(64), nullable=False),
        sa.Column("gate", sa.String(20), nullable=False, server_default="blocked"),
        sa.Column("preview_token", sa.String(64), nullable=False, server_default=""),
        sa.Column("preview_expires_in_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("health", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("tests_passed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tests_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deploy_state", sa.String(20), nullable=False, server_default="none"),
        sa.Column("deploy_target", sa.String(64), nullable=True),
        sa.Column("approval_id", UUID(as_uuid=True), nullable=True),
        sa.Column("spec", JSONB, nullable=False, server_default="{}"),
        sa.Column("test_summary", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("build_fingerprint", name="uq_foundry_builds_fingerprint"),
        sa.CheckConstraint("gate IN ('blocked', 'preview_ready')", name="foundry_build_gate_valid"),
        sa.CheckConstraint("health IN ('pending', 'passed', 'failed')", name="foundry_build_health_valid"),
        sa.CheckConstraint(
            "deploy_state IN ('none', 'requested', 'approved', 'deploying', 'deployed', 'rolled_back', 'failed')",
            name="foundry_build_deploy_state_valid"),
        sa.CheckConstraint("spec_version > 0", name="foundry_build_version_positive"),
    )
    op.create_index("ix_foundry_builds_app_id", "foundry_builds", ["app_id"])

    op.create_table(
        "foundry_feedback",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("app_id", UUID(as_uuid=True),
                  sa.ForeignKey("foundry_apps.id", ondelete="CASCADE"), nullable=False),
        sa.Column("build_fingerprint", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("changes", JSONB, nullable=False, server_default="[]"),
        sa.Column("author_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('bug', 'change_request', 'praise', 'question')",
                           name="foundry_feedback_kind_valid"),
    )
    op.create_index("ix_foundry_feedback_app_id", "foundry_feedback", ["app_id"])


def downgrade() -> None:
    op.drop_table("foundry_feedback")
    op.drop_table("foundry_builds")
    op.drop_table("foundry_apps")
