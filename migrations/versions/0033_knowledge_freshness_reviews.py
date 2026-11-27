"""Add evidence-backed freshness deadlines for published organizational knowledge.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("knowledge_revisions", sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("knowledge_revisions", sa.Column("review_due_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        """
        UPDATE knowledge_revisions
        SET review_due_at = statement_timestamp() + interval '365 days'
        WHERE status = 'published'
        """
    )
    op.create_index(
        "ix_knowledge_revisions_review_due",
        "knowledge_revisions",
        ["review_due_at"],
        postgresql_where=sa.text("status = 'published'"),
    )
    op.create_table(
        "knowledge_freshness_reviews",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reviewer_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "permission_grant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("permission_grants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rationale", sa.String(2000), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("next_review_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="ck_knowledge_freshness_reviews_rationale_length"),
        sa.CheckConstraint("next_review_due_at > reviewed_at", name="ck_knowledge_freshness_reviews_next_due_after_review"),
    )
    op.create_index(
        "ix_knowledge_freshness_reviews_revision", "knowledge_freshness_reviews", ["revision_id", "reviewed_at"]
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_freshness_review_guard() RETURNS trigger AS $$
        DECLARE
            r knowledge_revisions%ROWTYPE;
            g permission_grants%ROWTYPE;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'knowledge freshness review history is append-only' USING ERRCODE = 'check_violation';
            END IF;
            SELECT * INTO r FROM knowledge_revisions WHERE id = NEW.revision_id FOR SHARE;
            IF NOT FOUND OR r.status <> 'published' OR r.review_due_at IS NULL OR r.review_due_at > now() THEN
                RAISE EXCEPTION 'freshness review requires published knowledge that is due' USING ERRCODE = 'check_violation';
            END IF;
            IF r.author_user_id = NEW.reviewer_user_id THEN
                RAISE EXCEPTION 'a knowledge author cannot review their own statement freshness' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.reviewer_user_id AND is_active) THEN
                RAISE EXCEPTION 'freshness reviewer is not active' USING ERRCODE = 'check_violation';
            END IF;
            SELECT * INTO g FROM permission_grants WHERE id = NEW.permission_grant_id FOR SHARE;
            IF NOT FOUND OR g.revoked_at IS NOT NULL OR g.user_id <> NEW.reviewer_user_id
               OR g.permission <> 'knowledge.review'
               OR (g.category IS NOT NULL AND g.category <> r.category)
               OR (g.scope_group_id IS NOT NULL AND g.scope_group_id IS DISTINCT FROM r.scope_group_id) THEN
                RAISE EXCEPTION 'freshness reviewer grant is invalid or revoked' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.next_review_due_at < NEW.reviewed_at + interval '364 days'
               OR NEW.next_review_due_at > NEW.reviewed_at + interval '366 days' THEN
                RAISE EXCEPTION 'freshness review deadline must be one year after review' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_freshness_review_guard
        BEFORE INSERT OR UPDATE OR DELETE ON knowledge_freshness_reviews
        FOR EACH ROW EXECUTE FUNCTION knowledge_freshness_review_guard()
        """
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_revision_initialize_review_deadline() RETURNS trigger AS $$
        BEGIN
            IF OLD.status <> 'published' AND NEW.status = 'published' THEN
                NEW.last_reviewed_at := statement_timestamp();
                NEW.review_due_at := statement_timestamp() + interval '365 days';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_revision_initialize_review_deadline
        BEFORE UPDATE OF status ON knowledge_revisions
        FOR EACH ROW EXECUTE FUNCTION knowledge_revision_initialize_review_deadline()
        """
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_revision_freshness_fields_guard() RETURNS trigger AS $$
        BEGIN
            IF (NEW.review_due_at, NEW.last_reviewed_at) IS DISTINCT FROM (OLD.review_due_at, OLD.last_reviewed_at) THEN
                IF NEW.status <> 'published' OR NEW.last_reviewed_at IS NULL OR NEW.review_due_at IS NULL
                   OR NEW.last_reviewed_at <= COALESCE(OLD.last_reviewed_at, '-infinity'::timestamptz)
                   OR NOT EXISTS (
                       SELECT 1 FROM knowledge_freshness_reviews f
                       WHERE f.revision_id = OLD.id
                         AND f.reviewed_at = NEW.last_reviewed_at
                         AND f.next_review_due_at = NEW.review_due_at
                   ) THEN
                    RAISE EXCEPTION 'freshness deadlines require an authorized review event' USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_revision_freshness_fields_guard
        BEFORE UPDATE OF review_due_at, last_reviewed_at ON knowledge_revisions
        FOR EACH ROW EXECUTE FUNCTION knowledge_revision_freshness_fields_guard()
        """
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM knowledge_freshness_reviews) THEN
                RAISE EXCEPTION 'cannot downgrade while freshness review history exists';
            END IF;
        END $$;"""
    )
    op.execute("DROP TRIGGER IF EXISTS knowledge_revision_freshness_fields_guard ON knowledge_revisions")
    op.execute("DROP FUNCTION IF EXISTS knowledge_revision_freshness_fields_guard()")
    op.execute("DROP TRIGGER IF EXISTS knowledge_revision_initialize_review_deadline ON knowledge_revisions")
    op.execute("DROP FUNCTION IF EXISTS knowledge_revision_initialize_review_deadline()")
    op.execute("DROP TRIGGER IF EXISTS knowledge_freshness_review_guard ON knowledge_freshness_reviews")
    op.execute("DROP FUNCTION IF EXISTS knowledge_freshness_review_guard()")
    op.drop_index("ix_knowledge_freshness_reviews_revision", table_name="knowledge_freshness_reviews")
    op.drop_table("knowledge_freshness_reviews")
    op.drop_index("ix_knowledge_revisions_review_due", table_name="knowledge_revisions")
    op.drop_column("knowledge_revisions", "review_due_at")
    op.drop_column("knowledge_revisions", "last_reviewed_at")
