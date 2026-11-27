"""Add reviewed, versioned internal agent templates and publisher grants.

Revision ID: 0051_reviewed_agent_catalog
Revises: 0050_service_request_workflow_trigger
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0051_reviewed_agent_catalog"
down_revision = "0050_service_request_workflow_trigger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "internal_agent_catalog_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("latest_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("published_version", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revoke_rationale", sa.String(length=2000), nullable=True),
        sa.CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="slug_valid"),
        sa.CheckConstraint("latest_version > 0", name="latest_version_positive"),
        sa.CheckConstraint(
            "published_version IS NULL OR published_version BETWEEN 1 AND latest_version",
            name="published_version_valid",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoked_by_user_id IS NULL AND revoke_rationale IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL AND revoke_rationale IS NOT NULL AND "
            "char_length(btrim(revoke_rationale)) BETWEEN 3 AND 2000)",
            name="revocation_complete",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["revoked_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_internal_agent_catalog_templates_slug"),
    )
    op.create_table(
        "internal_agent_catalog_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("summary", sa.String(length=2000), nullable=False),
        sa.Column("agent_type", sa.String(length=32), nullable=False),
        sa.Column("knowledge_scope", sa.String(length=24), nullable=False),
        sa.Column("tool_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("author_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("submitted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_rationale", sa.String(length=2000), nullable=True),
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint("char_length(btrim(name)) BETWEEN 1 AND 120", name="name_valid"),
        sa.CheckConstraint("char_length(btrim(summary)) BETWEEN 1 AND 2000", name="summary_valid"),
        sa.CheckConstraint(
            "agent_type IN ('knowledge_research', 'market_research', 'company_analysis', 'employee_training')",
            name="agent_type_valid",
        ),
        sa.CheckConstraint("knowledge_scope IN ('requester_acl', 'department_only')", name="knowledge_scope_valid"),
        sa.CheckConstraint(
            "jsonb_typeof(tool_policy) = 'array' AND jsonb_array_length(tool_policy) BETWEEN 1 AND 4 AND "
            "tool_policy <@ '[\"knowledge_search\", \"public_web_research\", "
            "\"company_data_analysis\", \"training_plan\"]'::jsonb",
            name="tool_policy_valid",
        ),
        sa.CheckConstraint("status IN ('draft', 'pending_review', 'approved', 'rejected', 'superseded')", name="status_valid"),
        sa.CheckConstraint(
            "(status = 'draft' AND submitted_at IS NULL AND submitted_by_user_id IS NULL AND "
            "reviewed_at IS NULL AND reviewed_by_user_id IS NULL AND review_rationale IS NULL) OR "
            "(status = 'pending_review' AND submitted_at IS NOT NULL AND submitted_by_user_id IS NOT NULL AND "
            "reviewed_at IS NULL AND reviewed_by_user_id IS NULL AND review_rationale IS NULL) OR "
            "(status IN ('approved', 'rejected', 'superseded') AND submitted_at IS NOT NULL AND "
            "submitted_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL AND reviewed_by_user_id IS NOT NULL AND "
            "review_rationale IS NOT NULL AND char_length(btrim(review_rationale)) BETWEEN 3 AND 2000 AND "
            "reviewed_by_user_id <> author_user_id AND reviewed_by_user_id <> submitted_by_user_id AND "
            "reviewed_at >= submitted_at)",
            name="review_lifecycle_valid",
        ),
        sa.ForeignKeyConstraint(["template_id"], ["internal_agent_catalog_templates.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["submitted_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("template_id", "version", name="uq_internal_agent_catalog_versions_number"),
    )
    op.create_index(
        "ix_internal_agent_catalog_versions_queue",
        "internal_agent_catalog_versions",
        ["status", "created_at"],
    )
    op.create_index(
        "uq_internal_agent_catalog_versions_pending_template",
        "internal_agent_catalog_versions",
        ["template_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending_review'"),
    )
    op.create_table(
        "internal_agent_catalog_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission", sa.String(length=16), nullable=False),
        sa.Column("granted_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("permission IN ('publish', 'review')", name="permission_valid"),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoked_by_user_id IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL)",
            name="revocation_complete",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["granted_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["revoked_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_internal_agent_catalog_grants_active",
        "internal_agent_catalog_grants",
        ["user_id", "permission"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_internal_agent_catalog_grants_user",
        "internal_agent_catalog_grants",
        ["user_id", "permission", "revoked_at"],
    )
    op.create_table(
        "internal_agent_catalog_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("rationale", sa.String(length=2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('version_created', 'version_submitted', 'version_approved', 'version_rejected', 'catalog_revoked')",
            name="event_type_valid",
        ),
        sa.CheckConstraint(
            "(event_type IN ('version_approved', 'version_rejected', 'catalog_revoked') AND rationale IS NOT NULL AND "
            "char_length(btrim(rationale)) BETWEEN 3 AND 2000) OR "
            "(event_type IN ('version_created', 'version_submitted') AND rationale IS NULL)",
            name="rationale_valid",
        ),
        sa.ForeignKeyConstraint(["template_id"], ["internal_agent_catalog_templates.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["version_id"], ["internal_agent_catalog_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_internal_agent_catalog_events_template",
        "internal_agent_catalog_events",
        ["template_id", "created_at"],
    )

    op.execute(
        """
        CREATE FUNCTION prevent_internal_agent_catalog_spec_change() RETURNS trigger AS $$
        BEGIN
            IF NEW.template_id IS DISTINCT FROM OLD.template_id OR
               NEW.version IS DISTINCT FROM OLD.version OR
               NEW.name IS DISTINCT FROM OLD.name OR
               NEW.summary IS DISTINCT FROM OLD.summary OR
               NEW.agent_type IS DISTINCT FROM OLD.agent_type OR
               NEW.knowledge_scope IS DISTINCT FROM OLD.knowledge_scope OR
               NEW.tool_policy IS DISTINCT FROM OLD.tool_policy OR
               NEW.author_user_id IS DISTINCT FROM OLD.author_user_id OR
               NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'internal agent catalog version definitions are immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_internal_agent_catalog_spec_immutable
        BEFORE UPDATE ON internal_agent_catalog_versions
        FOR EACH ROW EXECUTE FUNCTION prevent_internal_agent_catalog_spec_change();
        """
    )
    op.execute(
        """
        CREATE FUNCTION prevent_internal_agent_catalog_event_change() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'internal agent catalog event history is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_internal_agent_catalog_event_append_only
        BEFORE UPDATE OR DELETE ON internal_agent_catalog_events
        FOR EACH ROW EXECUTE FUNCTION prevent_internal_agent_catalog_event_change();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_catalog_data = bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM internal_agent_catalog_templates) OR "
            "EXISTS (SELECT 1 FROM internal_agent_catalog_versions) OR "
            "EXISTS (SELECT 1 FROM internal_agent_catalog_grants) OR "
            "EXISTS (SELECT 1 FROM internal_agent_catalog_events)"
        )
    )
    if has_catalog_data:
        raise RuntimeError("Export/remove internal agent catalog data before downgrading migration 0051.")
    op.execute("DROP TRIGGER trg_internal_agent_catalog_event_append_only ON internal_agent_catalog_events")
    op.execute("DROP TRIGGER trg_internal_agent_catalog_spec_immutable ON internal_agent_catalog_versions")
    op.execute("DROP FUNCTION prevent_internal_agent_catalog_event_change()")
    op.execute("DROP FUNCTION prevent_internal_agent_catalog_spec_change()")
    op.drop_index("ix_internal_agent_catalog_events_template", table_name="internal_agent_catalog_events")
    op.drop_table("internal_agent_catalog_events")
    op.drop_index("ix_internal_agent_catalog_grants_user", table_name="internal_agent_catalog_grants")
    op.drop_index("uq_internal_agent_catalog_grants_active", table_name="internal_agent_catalog_grants")
    op.drop_table("internal_agent_catalog_grants")
    op.drop_index("uq_internal_agent_catalog_versions_pending_template", table_name="internal_agent_catalog_versions")
    op.drop_index("ix_internal_agent_catalog_versions_queue", table_name="internal_agent_catalog_versions")
    op.drop_table("internal_agent_catalog_versions")
    op.drop_table("internal_agent_catalog_templates")
