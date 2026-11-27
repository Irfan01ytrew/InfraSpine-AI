"""Require bounded expiry for new custom permission assignments and enforce it at use time.

Revision ID: 0036_expiring_role_assignments
Revises: 0035
"""

from alembic import op
import sqlalchemy as sa


revision = "0036_expiring_role_assignments"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing assignments keep their original effective lifetime. New API assignments require an expiry.
    op.add_column("role_template_assignments", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_role_template_assignments_expiring",
        "role_template_assignments",
        ["expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL AND expires_at IS NOT NULL"),
    )

    op.execute(
        """
        CREATE FUNCTION role_template_assignment_lifecycle_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL THEN
                    RAISE EXCEPTION 'a role assignment cannot be created revoked' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'role assignment history is retained' USING ERRCODE = 'check_violation';
            END IF;
            IF (NEW.id, NEW.role_template_id, NEW.user_id, NEW.assigned_by, NEW.department_ids, NEW.created_at, NEW.expires_at)
               IS DISTINCT FROM
               (OLD.id, OLD.role_template_id, OLD.user_id, OLD.assigned_by, OLD.department_ids, OLD.created_at, OLD.expires_at) THEN
                RAISE EXCEPTION 'role assignment scope and expiry are immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR NEW.revoked_by IS NULL THEN
                RAISE EXCEPTION 'a role assignment may only be revoked once' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER role_template_assignment_lifecycle_guard
        BEFORE INSERT OR UPDATE OR DELETE ON role_template_assignments
        FOR EACH ROW EXECUTE FUNCTION role_template_assignment_lifecycle_guard()
        """
    )

    # These triggers complement find_active_grant(): direct SQL insertions cannot use an expired or
    # revoked role-template grant to create an approval or freshness-review decision.
    op.execute(
        """
        CREATE FUNCTION role_template_grant_expiry_guard() RETURNS trigger AS $$
        DECLARE
            assignment_expiry timestamptz;
            assignment_revoked_at timestamptz;
        BEGIN
            SELECT a.expires_at, a.revoked_at INTO assignment_expiry, assignment_revoked_at
            FROM permission_grants g
            JOIN role_template_assignments a ON a.id = g.role_template_assignment_id
            WHERE g.id = NEW.permission_grant_id
            FOR SHARE OF g, a;
            IF FOUND AND (
                assignment_revoked_at IS NOT NULL
                OR (assignment_expiry IS NOT NULL AND assignment_expiry <= statement_timestamp())
            ) THEN
                RAISE EXCEPTION 'the role assignment has expired or been revoked' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER governance_approval_role_assignment_expiry_guard
        BEFORE INSERT ON governance_approvals
        FOR EACH ROW EXECUTE FUNCTION role_template_grant_expiry_guard()
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_freshness_role_assignment_expiry_guard
        BEFORE INSERT ON knowledge_freshness_reviews
        FOR EACH ROW EXECUTE FUNCTION role_template_grant_expiry_guard()
        """
    )


def downgrade() -> None:
    # Do not silently discard active expiry terms during rollback. An operator must first choose an
    # explicit data migration/rollback plan on a disposable clone and then retry the downgrade.
    has_expiring_assignments = op.get_bind().execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM role_template_assignments WHERE expires_at IS NOT NULL)")
    ).scalar_one()
    if has_expiring_assignments:
        raise RuntimeError("Cannot downgrade: expiring role assignments exist; preserve them before rollback.")
    op.execute("DROP TRIGGER IF EXISTS knowledge_freshness_role_assignment_expiry_guard ON knowledge_freshness_reviews")
    op.execute("DROP TRIGGER IF EXISTS governance_approval_role_assignment_expiry_guard ON governance_approvals")
    op.execute("DROP FUNCTION IF EXISTS role_template_grant_expiry_guard()")
    op.execute("DROP TRIGGER IF EXISTS role_template_assignment_lifecycle_guard ON role_template_assignments")
    op.execute("DROP FUNCTION IF EXISTS role_template_assignment_lifecycle_guard()")
    op.drop_index("ix_role_template_assignments_expiring", table_name="role_template_assignments")
    op.drop_column("role_template_assignments", "expires_at")
