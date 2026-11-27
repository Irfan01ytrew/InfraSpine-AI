"""Enterprise roles, scoped department managers, invitations and durable handoffs.

Existing accounts and their sessions are retained. Legacy ``user`` roles remain valid while
new invitations use ``employee`` or ``manager``.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_users_role_valid"), "users", type_="check")
    op.create_check_constraint(
        op.f("ck_users_role_valid"), "users", "role IN ('admin', 'manager', 'employee', 'user')"
    )

    op.create_table(
        "department_managers",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("manager_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assigned_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["access_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["manager_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assigned_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("group_id", "manager_user_id"),
    )
    op.create_index("ix_department_managers_user", "department_managers", ["manager_user_id"])
    op.execute(
        """
        CREATE FUNCTION department_managers_guard() RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM access_groups WHERE id = NEW.group_id AND kind = 'department' AND source = 'local'
            ) THEN
                RAISE EXCEPTION 'department manager must be assigned to a local department'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.manager_user_id AND role = 'manager') THEN
                RAISE EXCEPTION 'department manager must have the manager role' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER department_managers_guard
        BEFORE INSERT OR UPDATE ON department_managers
        FOR EACH ROW EXECUTE FUNCTION department_managers_guard();
        """
    )
    op.execute(
        """
        CREATE FUNCTION users_manager_role_guard() RETURNS trigger AS $$
        BEGIN
            IF NEW.role <> 'manager' AND EXISTS (
                SELECT 1 FROM department_managers WHERE manager_user_id = NEW.id
            ) THEN
                RAISE EXCEPTION 'remove department assignments before changing manager role'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER users_manager_role_guard
        BEFORE UPDATE OF role ON users
        FOR EACH ROW EXECUTE FUNCTION users_manager_role_guard();
        """
    )

    op.create_table(
        "user_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("department_ids", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('manager', 'employee')", name=op.f("ck_user_invitations_role_valid")),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_user_invitations_expiry_after_creation")),
        sa.CheckConstraint("accepted_at IS NULL OR revoked_at IS NULL", name=op.f("ck_user_invitations_not_accepted_and_revoked")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name=op.f("uq_user_invitations_token_hash")),
    )
    op.create_index("ix_user_invitations_email", "user_invitations", ["email"])
    op.create_index("ix_user_invitations_expires_at", "user_invitations", ["expires_at"])
    op.create_index(
        "uq_user_invitations_active_email",
        "user_invitations",
        ["email"],
        unique=True,
        postgresql_where=sa.text("accepted_at IS NULL AND revoked_at IS NULL"),
    )

    op.create_table(
        "employee_handoffs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("author_email", sa.String(length=320), nullable=False),
        sa.Column("department_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'submitted'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("acknowledged_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('submitted', 'acknowledged')", name=op.f("ck_employee_handoffs_status_valid")),
        sa.CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name=op.f("ck_employee_handoffs_title_length")),
        sa.CheckConstraint("length(btrim(body)) BETWEEN 1 AND 12000", name=op.f("ck_employee_handoffs_body_length")),
        sa.CheckConstraint(
            "(status = 'acknowledged' AND acknowledged_at IS NOT NULL AND acknowledged_by IS NOT NULL) "
            "OR (status = 'submitted' AND acknowledged_at IS NULL AND acknowledged_by IS NULL)",
            name=op.f("ck_employee_handoffs_acknowledgement_complete"),
        ),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["department_group_id"], ["access_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["acknowledged_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_employee_handoffs_department_created", "employee_handoffs", ["department_group_id", "created_at"])

    op.execute(
        """
        CREATE FUNCTION employee_handoffs_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'employee handoffs are retained' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.author_email IS DISTINCT FROM OLD.author_email
               OR NEW.department_group_id IS DISTINCT FROM OLD.department_group_id
               OR NEW.title IS DISTINCT FROM OLD.title
               OR NEW.body IS DISTINCT FROM OLD.body
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'handoffs are immutable except for one acknowledgement'
                    USING ERRCODE = 'check_violation';
            END IF;
            -- A user deletion may detach the foreign key while the author_email snapshot retains attribution.
            IF NEW.author_user_id IS DISTINCT FROM OLD.author_user_id AND NEW.author_user_id IS NOT NULL THEN
                RAISE EXCEPTION 'handoff author cannot be reassigned' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = OLD.status
               AND NEW.acknowledged_by IS NOT DISTINCT FROM OLD.acknowledged_by
               AND NEW.acknowledged_at IS NOT DISTINCT FROM OLD.acknowledged_at THEN
                RETURN NEW;
            END IF;
            IF OLD.status <> 'submitted' OR NEW.status <> 'acknowledged'
               OR NEW.acknowledged_by IS NULL OR NEW.acknowledged_at IS NULL THEN
                RAISE EXCEPTION 'handoff acknowledgement is one-time and complete'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER employee_handoffs_guard
        BEFORE UPDATE OR DELETE ON employee_handoffs
        FOR EACH ROW EXECUTE FUNCTION employee_handoffs_guard();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS employee_handoffs_guard ON employee_handoffs")
    op.execute("DROP FUNCTION IF EXISTS employee_handoffs_guard()")
    op.drop_index("ix_employee_handoffs_department_created", table_name="employee_handoffs")
    op.drop_table("employee_handoffs")

    op.execute("DROP TRIGGER IF EXISTS department_managers_guard ON department_managers")
    op.execute("DROP FUNCTION IF EXISTS department_managers_guard()")
    op.execute("DROP TRIGGER IF EXISTS users_manager_role_guard ON users")
    op.execute("DROP FUNCTION IF EXISTS users_manager_role_guard()")
    op.drop_index("ix_department_managers_user", table_name="department_managers")
    op.drop_table("department_managers")

    op.drop_index("ix_user_invitations_expires_at", table_name="user_invitations")
    op.drop_index("uq_user_invitations_active_email", table_name="user_invitations")
    op.drop_index("ix_user_invitations_email", table_name="user_invitations")
    op.drop_table("user_invitations")

    # An older application understands only admin/user. Keep accounts and make the downgrade safe.
    op.execute("UPDATE users SET role = 'user' WHERE role IN ('manager', 'employee')")
    op.drop_constraint(op.f("ck_users_role_valid"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_role_valid"), "users", "role IN ('admin', 'user')")
