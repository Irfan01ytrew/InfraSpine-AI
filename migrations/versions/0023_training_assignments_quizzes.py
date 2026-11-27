"""Manager assignments and private, append-only employee quiz scores.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "training_courses",
        sa.Column(
            "quiz_answer_key",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_table(
        "training_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignee_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assigned_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("department_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'assigned'"), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('assigned', 'completed')", name=op.f("ck_training_assignments_status_valid")),
        sa.CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)",
            name=op.f("ck_training_assignments_completion_timestamp_valid"),
        ),
        sa.ForeignKeyConstraint(["course_id"], ["training_courses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assignee_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["assigned_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["department_group_id"], ["access_groups.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("course_id", "assignee_user_id", name="uq_training_assignments_course_assignee"),
    )
    op.create_index(
        "ix_training_assignments_assignee_created", "training_assignments", ["assignee_user_id", "created_at"]
    )
    op.create_index(
        "ix_training_assignments_department_due", "training_assignments", ["department_group_id", "due_at"]
    )
    op.create_table(
        "training_quiz_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("answers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("max_score", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("score >= 0 AND max_score > 0 AND score <= max_score", name=op.f("ck_training_quiz_attempts_score_valid")),
        sa.CheckConstraint("jsonb_typeof(answers) = 'array'", name=op.f("ck_training_quiz_attempts_answers_are_array")),
        sa.ForeignKeyConstraint(["course_id"], ["training_courses.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["assignment_id"], ["training_assignments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_training_quiz_attempts_user_course",
        "training_quiz_attempts",
        ["user_id", "course_id", "created_at"],
    )

    # asyncpg prepares one statement at a time. Keep each function and trigger
    # definition separate so migrations also work on the application's async driver.
    op.execute(
        """
        CREATE FUNCTION training_course_quiz_immutable() RETURNS trigger AS $$
        BEGIN
            IF NEW.quiz_answer_key IS DISTINCT FROM OLD.quiz_answer_key THEN
                RAISE EXCEPTION 'training quiz answer keys are immutable' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER training_course_quiz_immutable
        BEFORE UPDATE ON training_courses
        FOR EACH ROW EXECUTE FUNCTION training_course_quiz_immutable();
        """
    )
    op.execute(
        """
        CREATE FUNCTION training_quiz_attempts_guard() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'training quiz attempts are append-only' USING ERRCODE = 'check_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER training_quiz_attempts_guard
        BEFORE UPDATE OR DELETE ON training_quiz_attempts
        FOR EACH ROW EXECUTE FUNCTION training_quiz_attempts_guard();
        """
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM training_assignments)
               OR EXISTS (SELECT 1 FROM training_quiz_attempts)
               OR EXISTS (SELECT 1 FROM training_courses WHERE quiz_answer_key <> '[]'::jsonb) THEN
                RAISE EXCEPTION 'export training assignments and scored attempts before downgrading migration 0023';
            END IF;
        END $$;"""
    )
    op.execute("DROP TRIGGER IF EXISTS training_quiz_attempts_guard ON training_quiz_attempts")
    op.execute("DROP FUNCTION IF EXISTS training_quiz_attempts_guard()")
    op.execute("DROP TRIGGER IF EXISTS training_course_quiz_immutable ON training_courses")
    op.execute("DROP FUNCTION IF EXISTS training_course_quiz_immutable()")
    op.drop_index("ix_training_quiz_attempts_user_course", table_name="training_quiz_attempts")
    op.drop_table("training_quiz_attempts")
    op.drop_index("ix_training_assignments_department_due", table_name="training_assignments")
    op.drop_index("ix_training_assignments_assignee_created", table_name="training_assignments")
    op.drop_table("training_assignments")
    op.drop_column("training_courses", "quiz_answer_key")
