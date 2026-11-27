"""Add two-admin approval for immutable custom role-template versions.

Revision ID: 0037_role_template_versions
Revises: 0036_expiring_role_assignments
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0037_role_template_versions"
down_revision = "0036_expiring_role_assignments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_role_templates_normalized_name", table_name="role_templates")
    op.create_index(
        "uq_role_templates_active_normalized_name",
        "role_templates",
        [sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "uq_role_templates_name_version",
        "role_templates",
        [sa.text("lower(name)"), "version"],
        unique=True,
    )
    op.add_column(
        "role_templates",
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_role_templates_supersedes",
        "role_templates",
        "role_templates",
        ["supersedes_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "version_parent_matches",
        "role_templates",
        "(version = 1 AND supersedes_id IS NULL) OR (version > 1 AND supersedes_id IS NOT NULL)",
    )
    op.create_table(
        "role_template_change_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("role_template_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("role_templates.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("proposed_description", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("proposed_permission_specs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT")),
        sa.Column("decision_rationale", sa.String(length=2000)),
        sa.Column("new_template_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("role_templates.id", ondelete="RESTRICT")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="status_valid"),
        sa.CheckConstraint(
            "(status = 'pending' AND decided_by IS NULL AND decision_rationale IS NULL AND new_template_id IS NULL AND decided_at IS NULL) OR "
            "(status = 'approved' AND decided_by IS NOT NULL AND decision_rationale IS NOT NULL AND new_template_id IS NOT NULL AND decided_at IS NOT NULL) OR "
            "(status = 'rejected' AND decided_by IS NOT NULL AND decision_rationale IS NOT NULL AND new_template_id IS NULL AND decided_at IS NOT NULL)",
            name="decision_complete",
        ),
        sa.CheckConstraint("length(btrim(decision_rationale)) >= 3 OR decision_rationale IS NULL", name="rationale_minimum"),
        sa.CheckConstraint("jsonb_typeof(proposed_permission_specs) = 'array'", name="proposed_specs_array"),
    )
    op.create_index(
        "uq_role_template_change_pending",
        "role_template_change_requests",
        ["role_template_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_role_template_change_status_created",
        "role_template_change_requests",
        ["status", "created_at"],
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION role_template_immutable_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'role templates are immutable and retained for assignment history' USING ERRCODE = 'check_violation';
            END IF;
            IF (NEW.id, NEW.name, NEW.description, NEW.base_role, NEW.version, NEW.permission_specs, NEW.created_by, NEW.created_at, NEW.supersedes_id)
               IS DISTINCT FROM
               (OLD.id, OLD.name, OLD.description, OLD.base_role, OLD.version, OLD.permission_specs, OLD.created_by, OLD.created_at, OLD.supersedes_id) THEN
                RAISE EXCEPTION 'role template definition is immutable' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION role_template_version_insert_guard() RETURNS trigger AS $$
        DECLARE previous role_templates%ROWTYPE;
        BEGIN
            IF NEW.version = 1 THEN
                IF NEW.supersedes_id IS NOT NULL THEN
                    RAISE EXCEPTION 'first template version cannot supersede another version' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            SELECT * INTO previous FROM role_templates WHERE id = NEW.supersedes_id FOR UPDATE;
            IF previous.id IS NULL OR NOT previous.is_active OR lower(previous.name) <> lower(NEW.name)
               OR previous.base_role <> NEW.base_role OR NEW.version <> previous.version + 1 THEN
                RAISE EXCEPTION 'new role-template version must supersede the current active version' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM role_template_change_requests r
                WHERE r.role_template_id = previous.id AND r.requested_by = NEW.created_by AND r.status = 'pending'
                  AND r.proposed_description = NEW.description AND r.proposed_permission_specs = NEW.permission_specs
            ) THEN
                RAISE EXCEPTION 'new role-template versions require a pending reviewed change request' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER role_template_version_insert_guard BEFORE INSERT ON role_templates "
        "FOR EACH ROW EXECUTE FUNCTION role_template_version_insert_guard()"
    )
    op.execute(
        """
        CREATE FUNCTION role_template_change_request_guard() RETURNS trigger AS $$
        DECLARE
            approver users%ROWTYPE;
            proposed role_templates%ROWTYPE;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'role-template change requests are retained as audit history' USING ERRCODE = 'check_violation';
            END IF;
            IF (NEW.id, NEW.role_template_id, NEW.requested_by, NEW.proposed_description, NEW.proposed_permission_specs, NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.role_template_id, OLD.requested_by, OLD.proposed_description, OLD.proposed_permission_specs, OLD.created_at) THEN
                RAISE EXCEPTION 'role-template change requests are immutable after submission' USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.status <> 'pending' OR NEW.status NOT IN ('approved', 'rejected') THEN
                RAISE EXCEPTION 'role-template change request has already been decided' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.decided_by = NEW.requested_by THEN
                RAISE EXCEPTION 'requester cannot approve their own role-template change' USING ERRCODE = 'check_violation';
            END IF;
            SELECT * INTO approver FROM users WHERE id = NEW.decided_by;
            IF approver.id IS NULL OR approver.role <> 'admin' OR NOT approver.is_active THEN
                RAISE EXCEPTION 'role-template change requires a different active administrator' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'approved' THEN
                SELECT * INTO proposed FROM role_templates WHERE id = NEW.new_template_id;
                IF proposed.id IS NULL OR proposed.supersedes_id <> NEW.role_template_id
                   OR proposed.created_by <> NEW.requested_by OR proposed.description <> NEW.proposed_description
                   OR proposed.permission_specs <> NEW.proposed_permission_specs THEN
                    RAISE EXCEPTION 'approved role-template version does not match the reviewed request' USING ERRCODE = 'check_violation';
                END IF;
            ELSIF NEW.new_template_id IS NOT NULL THEN
                RAISE EXCEPTION 'rejected role-template changes cannot create a version' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER role_template_change_request_guard BEFORE UPDATE OR DELETE ON role_template_change_requests "
        "FOR EACH ROW EXECUTE FUNCTION role_template_change_request_guard()"
    )
    op.execute(
        """
        CREATE FUNCTION role_template_change_request_insert_guard() RETURNS trigger AS $$
        BEGIN
            IF NEW.status <> 'pending' OR NEW.decided_by IS NOT NULL OR NEW.new_template_id IS NOT NULL THEN
                RAISE EXCEPTION 'role-template change requests must start pending' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.requested_by AND role = 'admin' AND is_active) THEN
                RAISE EXCEPTION 'only an active administrator can propose a role-template change' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER role_template_change_request_insert_guard BEFORE INSERT ON role_template_change_requests "
        "FOR EACH ROW EXECUTE FUNCTION role_template_change_request_insert_guard()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM role_templates WHERE version > 1)")).scalar():
        raise RuntimeError("Refusing to downgrade while versioned role templates exist")
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM role_template_change_requests)")).scalar():
        raise RuntimeError("Refusing to downgrade while role-template approval history exists")
    op.execute("DROP TRIGGER IF EXISTS role_template_change_request_insert_guard ON role_template_change_requests")
    op.execute("DROP FUNCTION IF EXISTS role_template_change_request_insert_guard()")
    op.execute("DROP TRIGGER IF EXISTS role_template_change_request_guard ON role_template_change_requests")
    op.execute("DROP FUNCTION IF EXISTS role_template_change_request_guard()")
    op.execute("DROP TRIGGER IF EXISTS role_template_version_insert_guard ON role_templates")
    op.execute("DROP FUNCTION IF EXISTS role_template_version_insert_guard()")
    op.drop_index("ix_role_template_change_status_created", table_name="role_template_change_requests")
    op.drop_index("uq_role_template_change_pending", table_name="role_template_change_requests")
    op.drop_table("role_template_change_requests")
    op.drop_constraint("version_parent_matches", "role_templates", type_="check")
    op.drop_constraint("fk_role_templates_supersedes", "role_templates", type_="foreignkey")
    op.drop_column("role_templates", "supersedes_id")
    op.drop_index("uq_role_templates_name_version", table_name="role_templates")
    op.drop_index("uq_role_templates_active_normalized_name", table_name="role_templates")
    op.create_index("uq_role_templates_normalized_name", "role_templates", [sa.text("lower(name)")], unique=True)
