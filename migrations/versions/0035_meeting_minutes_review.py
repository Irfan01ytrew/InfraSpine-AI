"""Add source-bound, editable meeting minutes and reviewed action items.

Revision ID: 0035
Revises: 0034_custom_role_templates
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035"
down_revision: str | None = "0034_custom_role_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "meeting_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_document_version", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_email", sa.String(320), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("status", sa.String(24), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("current_revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("submitted_revision_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('draft', 'submitted', 'changes_requested', 'approved')", name="ck_meeting_artifacts_status_valid"),
        sa.CheckConstraint("current_revision > 0", name="ck_meeting_artifacts_revision_positive"),
        sa.CheckConstraint("source_document_version > 0", name="ck_meeting_artifacts_source_version_positive"),
        sa.CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="ck_meeting_artifacts_source_digest_valid"),
        sa.CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="ck_meeting_artifacts_title_length"),
        sa.CheckConstraint("(status = 'submitted') = (submitted_revision_id IS NOT NULL)", name="ck_meeting_artifacts_submission_pair_valid"),
        sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"], ondelete="RESTRICT", name="fk_meeting_artifacts_source_document_id_documents"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL", name="fk_meeting_artifacts_owner_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_meeting_artifacts"),
    )
    op.create_index("ix_meeting_artifacts_source_created", "meeting_artifacts", ["source_document_id", "created_at"])
    op.create_table(
        "meeting_revisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("meeting_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("editor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("editor_email", sa.String(320), nullable=False),
        sa.Column("minutes_markdown", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("decisions", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("proposed_tasks", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_meeting_revisions_version_positive"),
        sa.CheckConstraint("length(minutes_markdown) <= 40000", name="ck_meeting_revisions_minutes_length"),
        sa.CheckConstraint("jsonb_typeof(decisions) = 'array' AND jsonb_array_length(decisions) <= 30", name="ck_meeting_revisions_decisions_bounded"),
        sa.CheckConstraint("jsonb_typeof(proposed_tasks) = 'array' AND jsonb_array_length(proposed_tasks) <= 30", name="ck_meeting_revisions_tasks_bounded"),
        sa.ForeignKeyConstraint(["meeting_id"], ["meeting_artifacts.id"], ondelete="RESTRICT", name="fk_meeting_revisions_meeting_id_meeting_artifacts"),
        sa.ForeignKeyConstraint(["editor_user_id"], ["users.id"], ondelete="SET NULL", name="fk_meeting_revisions_editor_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_meeting_revisions"),
        sa.UniqueConstraint("meeting_id", "version", name="uq_meeting_revisions_meeting_version"),
    )
    op.create_index("ix_meeting_revisions_meeting_created", "meeting_revisions", ["meeting_id", "created_at"])
    op.create_foreign_key(
        "fk_meeting_artifacts_submitted_revision_id_meeting_revisions",
        "meeting_artifacts",
        "meeting_revisions",
        ["submitted_revision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "meeting_reviews",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("meeting_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", sa.BigInteger(), nullable=False),
        sa.Column("reviewer_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewer_email", sa.String(320), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("rationale", sa.String(2000), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("decision IN ('approved', 'changes_requested')", name="ck_meeting_reviews_decision_valid"),
        sa.CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="ck_meeting_reviews_rationale_length"),
        sa.ForeignKeyConstraint(["meeting_id"], ["meeting_artifacts.id"], ondelete="RESTRICT", name="fk_meeting_reviews_meeting_id_meeting_artifacts"),
        sa.ForeignKeyConstraint(["revision_id"], ["meeting_revisions.id"], ondelete="RESTRICT", name="fk_meeting_reviews_revision_id_meeting_revisions"),
        sa.ForeignKeyConstraint(["reviewer_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_meeting_reviews_reviewer_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_meeting_reviews"),
        sa.UniqueConstraint("revision_id", name="uq_meeting_reviews_revision"),
    )
    op.create_index("ix_meeting_reviews_meeting_reviewed", "meeting_reviews", ["meeting_id", "reviewed_at"])
    op.create_table(
        "meeting_action_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("meeting_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", sa.BigInteger(), nullable=False),
        sa.Column("source_document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignee_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("body", sa.String(4000), nullable=False),
        sa.Column("evidence_chunk_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), server_default=sa.text("'open'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open', 'completed')", name="ck_meeting_action_tasks_status_valid"),
        sa.CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="ck_meeting_action_tasks_title_length"),
        sa.CheckConstraint("length(btrim(body)) BETWEEN 1 AND 4000", name="ck_meeting_action_tasks_body_length"),
        sa.CheckConstraint("jsonb_typeof(evidence_chunk_ids) = 'array' AND jsonb_array_length(evidence_chunk_ids) BETWEEN 1 AND 10", name="ck_meeting_action_tasks_evidence_valid"),
        sa.CheckConstraint("(status = 'completed') = (completed_at IS NOT NULL AND completed_by IS NOT NULL)", name="ck_meeting_action_tasks_completion_pair_valid"),
        sa.ForeignKeyConstraint(["meeting_id"], ["meeting_artifacts.id"], ondelete="RESTRICT", name="fk_meeting_action_tasks_meeting_id_meeting_artifacts"),
        sa.ForeignKeyConstraint(["revision_id"], ["meeting_revisions.id"], ondelete="RESTRICT", name="fk_meeting_action_tasks_revision_id_meeting_revisions"),
        sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"], ondelete="RESTRICT", name="fk_meeting_action_tasks_source_document_id_documents"),
        sa.ForeignKeyConstraint(["assignee_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_meeting_action_tasks_assignee_user_id_users"),
        sa.ForeignKeyConstraint(["completed_by"], ["users.id"], ondelete="RESTRICT", name="fk_meeting_action_tasks_completed_by_users"),
        sa.PrimaryKeyConstraint("id", name="pk_meeting_action_tasks"),
    )
    op.create_index("ix_meeting_action_tasks_assignee_created", "meeting_action_tasks", ["assignee_user_id", "created_at"])
    op.create_index("ix_meeting_action_tasks_meeting_created", "meeting_action_tasks", ["meeting_id", "created_at"])

    op.execute(
        """
        CREATE FUNCTION meeting_artifact_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'meeting records are retained for audit history' USING ERRCODE = 'check_violation';
            END IF;
            IF (NEW.id, NEW.source_document_id, NEW.source_document_version, NEW.source_sha256,
                NEW.owner_email, NEW.title, NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.source_document_id, OLD.source_document_version, OLD.source_sha256,
                OLD.owner_email, OLD.title, OLD.created_at) THEN
                RAISE EXCEPTION 'meeting identity and source are immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id AND NEW.owner_user_id IS NOT NULL THEN
                RAISE EXCEPTION 'meeting ownership cannot be reassigned' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.current_revision < OLD.current_revision OR NEW.current_revision > OLD.current_revision + 1 THEN
                RAISE EXCEPTION 'meeting revision must advance one version at a time' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.current_revision <> OLD.current_revision
               AND NOT (OLD.status IN ('draft', 'changes_requested') AND NEW.status = 'draft') THEN
                RAISE EXCEPTION 'minutes can only be edited while drafting' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.current_revision <> OLD.current_revision AND NOT EXISTS (
                SELECT 1 FROM meeting_revisions v WHERE v.meeting_id = NEW.id AND v.version = NEW.current_revision
            ) THEN
                RAISE EXCEPTION 'new meeting revision must exist before advancing the pointer' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status THEN
                IF OLD.status = 'draft' AND NEW.status = 'submitted' THEN
                    IF NEW.submitted_revision_id IS NULL OR NOT EXISTS (
                        SELECT 1 FROM meeting_revisions v
                        WHERE v.id = NEW.submitted_revision_id AND v.meeting_id = NEW.id
                          AND v.version = NEW.current_revision
                    ) THEN
                        RAISE EXCEPTION 'submission must reference the current revision' USING ERRCODE = 'check_violation';
                    END IF;
                ELSIF OLD.status = 'changes_requested' AND NEW.status = 'draft' THEN
                    IF NEW.submitted_revision_id IS NOT NULL THEN
                        RAISE EXCEPTION 'revised draft cannot retain a submitted revision' USING ERRCODE = 'check_violation';
                    END IF;
                ELSIF OLD.status = 'submitted' AND NEW.status IN ('approved', 'changes_requested') THEN
                    IF NEW.submitted_revision_id IS NOT NULL OR NOT EXISTS (
                        SELECT 1 FROM meeting_reviews r
                        WHERE r.meeting_id = OLD.id AND r.revision_id = OLD.submitted_revision_id
                          AND r.decision = NEW.status
                    ) THEN
                        RAISE EXCEPTION 'submitted meeting changes require a matching review' USING ERRCODE = 'check_violation';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'invalid meeting state transition' USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("CREATE TRIGGER meeting_artifact_guard BEFORE UPDATE OR DELETE ON meeting_artifacts FOR EACH ROW EXECUTE FUNCTION meeting_artifact_guard()")
    op.execute(
        """
        CREATE FUNCTION meeting_revision_guard() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'meeting revision history is append-only' USING ERRCODE = 'check_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("CREATE TRIGGER meeting_revision_guard BEFORE UPDATE OR DELETE ON meeting_revisions FOR EACH ROW EXECUTE FUNCTION meeting_revision_guard()")
    op.execute(
        """
        CREATE FUNCTION meeting_review_guard() RETURNS trigger AS $$
        DECLARE m meeting_artifacts%ROWTYPE;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'meeting review history is append-only' USING ERRCODE = 'check_violation';
            END IF;
            SELECT * INTO m FROM meeting_artifacts WHERE id = NEW.meeting_id FOR SHARE;
            IF NOT FOUND OR m.status <> 'submitted' OR m.submitted_revision_id <> NEW.revision_id
               OR m.owner_user_id = NEW.reviewer_user_id
               OR NOT EXISTS (SELECT 1 FROM meeting_revisions v WHERE v.id = NEW.revision_id AND v.meeting_id = m.id)
               OR NOT EXISTS (SELECT 1 FROM users u WHERE u.id = NEW.reviewer_user_id AND u.is_active) THEN
                RAISE EXCEPTION 'meeting review does not match an active submitted meeting' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("CREATE TRIGGER meeting_review_guard BEFORE INSERT OR UPDATE OR DELETE ON meeting_reviews FOR EACH ROW EXECUTE FUNCTION meeting_review_guard()")
    op.execute(
        """
        CREATE FUNCTION meeting_action_task_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM meeting_reviews r
                    WHERE r.meeting_id = NEW.meeting_id AND r.revision_id = NEW.revision_id AND r.decision = 'approved'
                ) OR NOT EXISTS (
                    SELECT 1 FROM meeting_artifacts m
                    WHERE m.id = NEW.meeting_id AND m.source_document_id = NEW.source_document_id AND m.status = 'approved'
                ) OR NOT EXISTS (
                    SELECT 1 FROM users u WHERE u.id = NEW.assignee_user_id AND u.is_active
                ) OR EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(NEW.evidence_chunk_ids) AS evidence(chunk_id)
                    WHERE NOT EXISTS (
                        SELECT 1 FROM document_chunks c
                        WHERE c.id = evidence.chunk_id::bigint AND c.document_id = NEW.source_document_id
                    )
                ) THEN
                    RAISE EXCEPTION 'action tasks require an approved meeting revision' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'meeting action items are retained' USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.status <> 'open' OR NEW.status <> 'completed'
               OR NEW.completed_by IS DISTINCT FROM NEW.assignee_user_id OR NEW.completed_at IS NULL
               OR (NEW.id, NEW.meeting_id, NEW.revision_id, NEW.source_document_id, NEW.assignee_user_id,
                   NEW.title, NEW.body, NEW.evidence_chunk_ids, NEW.due_at, NEW.created_at)
                  IS DISTINCT FROM
                  (OLD.id, OLD.meeting_id, OLD.revision_id, OLD.source_document_id, OLD.assignee_user_id,
                   OLD.title, OLD.body, OLD.evidence_chunk_ids, OLD.due_at, OLD.created_at) THEN
                RAISE EXCEPTION 'meeting action items are retained and can only be completed by their assignee' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("CREATE TRIGGER meeting_action_task_guard BEFORE INSERT OR UPDATE OR DELETE ON meeting_action_tasks FOR EACH ROW EXECUTE FUNCTION meeting_action_task_guard()")


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM meeting_artifacts) OR EXISTS (SELECT 1 FROM meeting_revisions)
               OR EXISTS (SELECT 1 FROM meeting_reviews) OR EXISTS (SELECT 1 FROM meeting_action_tasks) THEN
                RAISE EXCEPTION 'export meeting records and review history before downgrading migration 0035';
            END IF;
        END $$;"""
    )
    op.execute("DROP TRIGGER IF EXISTS meeting_action_task_guard ON meeting_action_tasks")
    op.execute("DROP FUNCTION IF EXISTS meeting_action_task_guard()")
    op.execute("DROP TRIGGER IF EXISTS meeting_review_guard ON meeting_reviews")
    op.execute("DROP FUNCTION IF EXISTS meeting_review_guard()")
    op.execute("DROP TRIGGER IF EXISTS meeting_revision_guard ON meeting_revisions")
    op.execute("DROP FUNCTION IF EXISTS meeting_revision_guard()")
    op.execute("DROP TRIGGER IF EXISTS meeting_artifact_guard ON meeting_artifacts")
    op.execute("DROP FUNCTION IF EXISTS meeting_artifact_guard()")
    op.drop_index("ix_meeting_action_tasks_meeting_created", table_name="meeting_action_tasks")
    op.drop_index("ix_meeting_action_tasks_assignee_created", table_name="meeting_action_tasks")
    op.drop_table("meeting_action_tasks")
    op.drop_index("ix_meeting_reviews_meeting_reviewed", table_name="meeting_reviews")
    op.drop_table("meeting_reviews")
    op.drop_constraint(
        "fk_meeting_artifacts_submitted_revision_id_meeting_revisions",
        "meeting_artifacts",
        type_="foreignkey",
    )
    op.drop_index("ix_meeting_revisions_meeting_created", table_name="meeting_revisions")
    op.drop_table("meeting_revisions")
    op.drop_index("ix_meeting_artifacts_source_created", table_name="meeting_artifacts")
    op.drop_table("meeting_artifacts")
