"""Persist bounded, source-bound adaptive practice scenarios.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "training_practice_scenarios",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("question_indexes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_digest", sa.String(length=64), nullable=False),
        sa.Column("attempt_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("jsonb_typeof(question_indexes) = 'array'", name="question_indexes_are_array"),
        sa.CheckConstraint("jsonb_array_length(question_indexes) BETWEEN 1 AND 5", name="question_count_valid"),
        sa.CheckConstraint("source_digest ~ '^[0-9a-f]{64}$'", name="source_digest_valid"),
        sa.CheckConstraint(
            "(attempt_id IS NOT NULL) = (completed_at IS NOT NULL)", name="completion_pair_valid"
        ),
        sa.ForeignKeyConstraint(["course_id"], ["training_courses.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["assignment_id"], ["training_assignments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["attempt_id"], ["training_quiz_attempts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_training_practice_user_course",
        "training_practice_scenarios",
        ["user_id", "course_id", "created_at"],
    )
    op.create_index(
        "uq_training_practice_attempt",
        "training_practice_scenarios",
        ["attempt_id"],
        unique=True,
        postgresql_where=sa.text("attempt_id IS NOT NULL"),
    )
    op.execute(
        """
        CREATE FUNCTION training_practice_scenarios_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'training practice scenarios are append-only' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.course_id IS DISTINCT FROM OLD.course_id
               OR NEW.user_id IS DISTINCT FROM OLD.user_id
               OR NEW.assignment_id IS DISTINCT FROM OLD.assignment_id
               OR NEW.question_indexes IS DISTINCT FROM OLD.question_indexes
               OR NEW.source_digest IS DISTINCT FROM OLD.source_digest
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR OLD.attempt_id IS NOT NULL
               OR NEW.attempt_id IS NULL
               OR NEW.completed_at IS NULL THEN
                RAISE EXCEPTION 'training practice scenarios are immutable except for one completion link'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER training_practice_scenarios_guard
        BEFORE UPDATE OR DELETE ON training_practice_scenarios
        FOR EACH ROW EXECUTE FUNCTION training_practice_scenarios_guard()
        """
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM training_practice_scenarios) THEN
                RAISE EXCEPTION 'export adaptive practice scenarios before downgrading migration 0028';
            END IF;
        END $$;"""
    )
    op.execute("DROP TRIGGER IF EXISTS training_practice_scenarios_guard ON training_practice_scenarios")
    op.execute("DROP FUNCTION IF EXISTS training_practice_scenarios_guard()")
    op.drop_index("uq_training_practice_attempt", table_name="training_practice_scenarios")
    op.drop_index("ix_training_practice_user_course", table_name="training_practice_scenarios")
    op.drop_table("training_practice_scenarios")
