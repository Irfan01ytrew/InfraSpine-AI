"""Add immutable, department-scoped agent profile versions.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("agent_type", sa.String(length=32), nullable=False),
        sa.Column("department_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_scope", sa.String(length=24), nullable=False),
        sa.Column(
            "tool_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_agent_profiles_version_positive"),
        sa.CheckConstraint(
            "agent_type IN ('knowledge_research', 'market_research', 'company_analysis', 'employee_training')",
            name="ck_agent_profiles_agent_type_valid",
        ),
        sa.CheckConstraint(
            "knowledge_scope IN ('requester_acl', 'department_only')",
            name="ck_agent_profiles_knowledge_scope_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(tool_policy) = 'array' AND "
            "tool_policy <@ '[\"knowledge_search\", \"public_web_research\", "
            "\"company_data_analysis\", \"training_plan\"]'::jsonb",
            name="ck_agent_profiles_tool_policy_allowlisted",
        ),
        sa.ForeignKeyConstraint(
            ["department_group_id"], ["access_groups.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_key", "version", name="uq_agent_profiles_key_version"),
    )
    op.create_index(
        "uq_agent_profiles_one_enabled_version",
        "agent_profiles",
        ["agent_key"],
        unique=True,
        postgresql_where=sa.text("enabled"),
    )
    op.create_index(
        "ix_agent_profiles_department", "agent_profiles", ["department_group_id", "enabled"]
    )
    op.execute(
        """
        CREATE FUNCTION agent_profiles_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'agent profile versions are retained' USING ERRCODE = 'check_violation';
            END IF;
            IF ROW(NEW.id, NEW.agent_key, NEW.version, NEW.name, NEW.agent_type,
                   NEW.department_group_id, NEW.knowledge_scope, NEW.tool_policy,
                   NEW.created_by_user_id, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.agent_key, OLD.version, OLD.name, OLD.agent_type,
                   OLD.department_group_id, OLD.knowledge_scope, OLD.tool_policy,
                   OLD.created_by_user_id, OLD.created_at) THEN
                RAISE EXCEPTION 'agent profile version configuration is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT OLD.enabled AND NEW.enabled THEN
                RAISE EXCEPTION 'disabled agent profile versions cannot be re-enabled' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_profiles_guard
        BEFORE UPDATE OR DELETE ON agent_profiles
        FOR EACH ROW EXECUTE FUNCTION agent_profiles_guard();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agent_profiles) THEN
                RAISE EXCEPTION 'agent profile history exists; refusing a data-losing downgrade';
            END IF;
        END $$;
        """
    )
    op.execute("DROP TRIGGER agent_profiles_guard ON agent_profiles")
    op.execute("DROP FUNCTION agent_profiles_guard()")
    op.drop_index("ix_agent_profiles_department", table_name="agent_profiles")
    op.drop_index("uq_agent_profiles_one_enabled_version", table_name="agent_profiles")
    op.drop_table("agent_profiles")
