"""Add admin-managed, department-scoped permission templates.

Revision ID: 0034_custom_role_templates
Revises: 0033_knowledge_freshness_reviews
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0034_custom_role_templates"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "role_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=500), server_default="", nullable=False),
        sa.Column("base_role", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("permission_specs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT", name="fk_role_templates_created_by_users"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("base_role IN ('manager', 'employee')", name="ck_role_templates_base_role_valid"),
        sa.CheckConstraint("version >= 1", name="ck_role_templates_version_positive"),
        sa.CheckConstraint("jsonb_typeof(permission_specs) = 'array'", name="ck_role_templates_permission_specs_array"),
    )
    op.create_index("uq_role_templates_normalized_name", "role_templates", [sa.text("lower(name)")], unique=True)

    op.create_table(
        "role_template_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("role_template_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("role_templates.id", ondelete="RESTRICT", name="fk_role_template_assignments_role_template_id_role_templates"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT", name="fk_role_template_assignments_user_id_users"), nullable=False),
        sa.Column("assigned_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT", name="fk_role_template_assignments_assigned_by_users"), nullable=False),
        sa.Column("department_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT", name="fk_role_template_assignments_revoked_by_users")),
        sa.CheckConstraint("(revoked_at IS NULL) = (revoked_by IS NULL)", name="ck_role_template_assignments_revocation_complete"),
        sa.CheckConstraint("jsonb_typeof(department_ids) = 'array'", name="ck_role_template_assignments_department_ids_array"),
    )
    op.create_index("ix_role_template_assignments_role_template_id", "role_template_assignments", ["role_template_id"])
    op.create_index("ix_role_template_assignments_user_id", "role_template_assignments", ["user_id"])
    op.create_index(
        "ix_role_template_assignments_active_user",
        "role_template_assignments",
        ["user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.add_column(
        "permission_grants",
        sa.Column(
            "role_template_assignment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("role_template_assignments.id", ondelete="RESTRICT", name="fk_permission_grants_role_template_assignment"),
        ),
    )
    op.create_index(
        "ix_permission_grants_role_template_assignment_id",
        "permission_grants",
        ["role_template_assignment_id"],
    )
    op.execute(
        """
        CREATE FUNCTION role_template_immutable_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'role templates are immutable and retained for assignment history' USING ERRCODE = 'check_violation';
            END IF;
            IF (NEW.id, NEW.name, NEW.description, NEW.base_role, NEW.version, NEW.permission_specs, NEW.created_by, NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.name, OLD.description, OLD.base_role, OLD.version, OLD.permission_specs, OLD.created_by, OLD.created_at) THEN
                RAISE EXCEPTION 'role template definition is immutable' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER role_template_immutable_guard
        BEFORE UPDATE OR DELETE ON role_templates
        FOR EACH ROW EXECUTE FUNCTION role_template_immutable_guard()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS role_template_immutable_guard ON role_templates")
    op.execute("DROP FUNCTION IF EXISTS role_template_immutable_guard()")
    op.drop_index("ix_permission_grants_role_template_assignment_id", table_name="permission_grants")
    op.drop_column("permission_grants", "role_template_assignment_id")
    op.drop_index("ix_role_template_assignments_active_user", table_name="role_template_assignments")
    op.drop_index("ix_role_template_assignments_user_id", table_name="role_template_assignments")
    op.drop_index("ix_role_template_assignments_role_template_id", table_name="role_template_assignments")
    op.drop_table("role_template_assignments")
    op.drop_index("uq_role_templates_normalized_name", table_name="role_templates")
    op.drop_table("role_templates")
