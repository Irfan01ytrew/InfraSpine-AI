"""Step AN (Companion OS) slices 2-3: personal goals, projects, tasks, preferences,
append-only progress, per-integration consent and the approval-gated digest outbox.

Revision ID: 0104_companion_os
Revises: 0103_unified_studio_governance
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0104_companion_os"
down_revision: str | None = "0103_unified_studio_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companion_projects",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(1000), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active','archived')", name="ck_companion_projects_status_valid"),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 1 AND 120", name="ck_companion_projects_name_length"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_companion_projects_user_status", "companion_projects", ["user_id", "status"])

    op.create_table(
        "companion_goals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'active'")),
        sa.Column("priority", sa.String(8), nullable=False, server_default=sa.text("'medium'")),
        sa.Column("deadline", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active','archived','completed')", name="ck_companion_goals_status_valid"
        ),
        sa.CheckConstraint("priority IN ('low','medium','high')", name="ck_companion_goals_priority_valid"),
        sa.CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="ck_companion_goals_title_length"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["companion_projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_companion_goals_user_status", "companion_goals", ["user_id", "status"])
    op.create_index("ix_companion_goals_user_project", "companion_goals", ["user_id", "project_id"])

    op.create_table(
        "companion_tasks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("goal_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'todo'")),
        sa.Column("priority", sa.String(8), nullable=False, server_default=sa.text("'medium'")),
        sa.Column("due", sa.Date(), nullable=True),
        sa.Column("estimated_hours", sa.Float(), nullable=False, server_default=sa.text("'0'")),
        sa.Column("action_kind", sa.String(16), nullable=False, server_default=sa.text("'internal'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('todo','in_progress','done','cancelled')", name="ck_companion_tasks_status_valid"
        ),
        sa.CheckConstraint("priority IN ('low','medium','high')", name="ck_companion_tasks_priority_valid"),
        sa.CheckConstraint(
            "action_kind IN ('internal','send','book','pay','share','publish','deploy')",
            name="ck_companion_tasks_action_kind_valid",
        ),
        sa.CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="ck_companion_tasks_title_length"),
        sa.CheckConstraint("estimated_hours >= 0", name="ck_companion_tasks_hours_valid"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["goal_id"], ["companion_goals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_companion_tasks_user_status", "companion_tasks", ["user_id", "status"])
    op.create_index("ix_companion_tasks_goal_due", "companion_tasks", ["goal_id", "due"])

    op.create_table(
        "companion_preferences",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("language", sa.String(16), nullable=False, server_default=sa.text("'en'")),
        sa.Column("timezone", sa.String(64), nullable=False, server_default=sa.text("'Asia/Karachi'")),
        sa.Column("weekly_capacity_hours", sa.Float(), nullable=False, server_default=sa.text("'40'")),
        sa.Column("quiet_start", sa.Time(), nullable=False, server_default=sa.text("'22:00'")),
        sa.Column("quiet_end", sa.Time(), nullable=False, server_default=sa.text("'07:00'")),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default=sa.text("'90'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("language IN ('en','ur','roman_urdu')", name="ck_companion_prefs_language_valid"),
        sa.CheckConstraint(
            "weekly_capacity_hours > 0 AND weekly_capacity_hours <= 168",
            name="ck_companion_prefs_capacity_valid",
        ),
        sa.CheckConstraint("retention_days >= 1", name="ck_companion_prefs_retention_valid"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "companion_progress",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("goal_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=True),
        sa.Column("percent", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(1000), nullable=False, server_default=sa.text("''")),
        sa.Column("on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("percent BETWEEN 0 AND 100", name="ck_companion_progress_percent_valid"),
        sa.CheckConstraint("length(btrim(note)) <= 1000", name="ck_companion_progress_note_length"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["goal_id"], ["companion_goals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["companion_tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_companion_progress_user_goal_created", "companion_progress", ["user_id", "goal_id", "created_at"]
    )
    op.create_index("ix_companion_progress_task_created", "companion_progress", ["task_id", "created_at"])

    op.create_table(
        "companion_consents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("integration", sa.String(32), nullable=False),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("integration IN ('email_digest')", name="ck_companion_consents_integration_valid"),
        sa.CheckConstraint("length(btrim(scope)) BETWEEN 1 AND 64", name="ck_companion_consents_scope_length"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_companion_consents_active", "companion_consents", ["user_id", "integration"], unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_companion_consents_user_integration", "companion_consents", ["user_id", "integration"]
    )

    op.create_table(
        "companion_digest_outbox",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("consent_id", sa.UUID(), nullable=True),
        sa.Column("approval_id", sa.UUID(), nullable=True),
        sa.Column("digest_date", sa.Date(), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("message_id", sa.String(256), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("'0'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','sent','cancelled','failed')", name="ck_companion_digest_status_valid"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["consent_id"], ["companion_consents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approval_id"], ["platform_approval_requests.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_companion_digest_user_date", "companion_digest_outbox", ["user_id", "digest_date"], unique=True,
        postgresql_where=sa.text("status IN ('pending','sent')"),
    )
    op.create_index("ix_companion_digest_user_status", "companion_digest_outbox", ["user_id", "status"])


def downgrade() -> None:
    op.drop_table("companion_digest_outbox")
    op.drop_table("companion_consents")
    op.drop_table("companion_progress")
    op.drop_table("companion_preferences")
    op.drop_table("companion_tasks")
    op.drop_table("companion_goals")
    op.drop_table("companion_projects")
