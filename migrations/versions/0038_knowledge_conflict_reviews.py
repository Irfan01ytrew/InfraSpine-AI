"""Persist scoped potential-conflict triage and append-only human decisions.

Revision ID: 0038_knowledge_conflicts
Revises: 0037_role_template_versions
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0038_knowledge_conflicts"
down_revision = "0037_role_template_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_conflicts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "compared_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("revision_id <> compared_revision_id", name="ck_knowledge_conflicts_different_revisions"),
        sa.CheckConstraint("status IN ('open', 'confirmed', 'dismissed')", name="ck_knowledge_conflicts_status_valid"),
        sa.UniqueConstraint("revision_id", "compared_revision_id", name="uq_knowledge_conflict_pair"),
    )
    op.create_index("ix_knowledge_conflicts_status_detected", "knowledge_conflicts", ["status", "detected_at"])

    op.create_table(
        "knowledge_conflict_reviews",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "conflict_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_conflicts.id", ondelete="RESTRICT"),
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
        sa.Column(
            "compared_permission_grant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("permission_grants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("rationale", sa.String(2000), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("decision IN ('confirmed', 'dismissed')", name="ck_knowledge_conflict_reviews_decision_valid"),
        sa.CheckConstraint(
            "length(btrim(rationale)) BETWEEN 3 AND 2000", name="ck_knowledge_conflict_reviews_rationale_length"
        ),
        sa.UniqueConstraint("conflict_id", name="uq_knowledge_conflict_review_once"),
    )
    op.create_index(
        "ix_knowledge_conflict_reviews_reviewer_time", "knowledge_conflict_reviews", ["reviewer_user_id", "decided_at"]
    )

    op.execute(
        """
        CREATE FUNCTION knowledge_conflict_pair_guard() RETURNS trigger AS $$
        DECLARE
            submitted_revision knowledge_revisions%ROWTYPE;
            published_revision knowledge_revisions%ROWTYPE;
        BEGIN
            SELECT * INTO submitted_revision FROM knowledge_revisions WHERE id = NEW.revision_id FOR SHARE;
            SELECT * INTO published_revision FROM knowledge_revisions WHERE id = NEW.compared_revision_id FOR SHARE;
            IF NOT FOUND OR submitted_revision.id IS NULL OR published_revision.id IS NULL
               OR submitted_revision.status <> 'submitted' OR published_revision.status <> 'published'
               OR submitted_revision.entry_id = published_revision.entry_id
               OR submitted_revision.category <> published_revision.category
               OR submitted_revision.visibility = 'private' OR published_revision.visibility = 'private'
               OR NOT (
                    submitted_revision.visibility = 'shared'
                    OR published_revision.visibility = 'shared'
                    OR (submitted_revision.visibility = 'restricted'
                        AND published_revision.visibility = 'restricted'
                        AND submitted_revision.scope_group_id = published_revision.scope_group_id)
               ) THEN
                RAISE EXCEPTION 'knowledge conflict pair is outside the submitted and published scope' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status <> 'open' THEN
                RAISE EXCEPTION 'knowledge conflict starts open' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_conflict_pair_guard
        BEFORE INSERT ON knowledge_conflicts
        FOR EACH ROW EXECUTE FUNCTION knowledge_conflict_pair_guard()
        """
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_conflict_review_guard() RETURNS trigger AS $$
        DECLARE
            c knowledge_conflicts%ROWTYPE;
            a knowledge_revisions%ROWTYPE;
            b knowledge_revisions%ROWTYPE;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'knowledge conflict review history is append-only' USING ERRCODE = 'check_violation';
            END IF;
            SELECT * INTO c FROM knowledge_conflicts WHERE id = NEW.conflict_id FOR UPDATE;
            IF NOT FOUND OR c.status <> 'open' OR NEW.decision NOT IN ('confirmed', 'dismissed') THEN
                RAISE EXCEPTION 'knowledge conflict is no longer open' USING ERRCODE = 'check_violation';
            END IF;
            SELECT * INTO a FROM knowledge_revisions WHERE id = c.revision_id FOR SHARE;
            SELECT * INTO b FROM knowledge_revisions WHERE id = c.compared_revision_id FOR SHARE;
            IF NOT FOUND OR a.id IS NULL OR b.id IS NULL OR a.status NOT IN ('submitted', 'published')
               OR b.status <> 'published' THEN
                RAISE EXCEPTION 'knowledge conflict sources are no longer reviewable' USING ERRCODE = 'check_violation';
            END IF;
            IF a.author_user_id = NEW.reviewer_user_id OR b.author_user_id = NEW.reviewer_user_id THEN
                RAISE EXCEPTION 'knowledge conflict authors cannot review their own statements' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.reviewer_user_id AND is_active) THEN
                RAISE EXCEPTION 'knowledge conflict reviewer is inactive' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM permission_grants g
                LEFT JOIN role_template_assignments t ON t.id = g.role_template_assignment_id
                WHERE g.id = NEW.permission_grant_id AND g.user_id = NEW.reviewer_user_id
                  AND g.permission = 'knowledge.review' AND g.revoked_at IS NULL
                  AND (g.category IS NULL OR g.category = a.category)
                  AND (g.scope_group_id IS NULL OR g.scope_group_id = a.scope_group_id)
                  AND (g.role_template_assignment_id IS NULL OR
                       (t.revoked_at IS NULL AND (t.expires_at IS NULL OR t.expires_at > statement_timestamp())))
            ) OR NOT EXISTS (
                SELECT 1 FROM permission_grants g
                LEFT JOIN role_template_assignments t ON t.id = g.role_template_assignment_id
                WHERE g.id = NEW.compared_permission_grant_id AND g.user_id = NEW.reviewer_user_id
                  AND g.permission = 'knowledge.review' AND g.revoked_at IS NULL
                  AND (g.category IS NULL OR g.category = b.category)
                  AND (g.scope_group_id IS NULL OR g.scope_group_id = b.scope_group_id)
                  AND (g.role_template_assignment_id IS NULL OR
                       (t.revoked_at IS NULL AND (t.expires_at IS NULL OR t.expires_at > statement_timestamp())))
            ) THEN
                RAISE EXCEPTION 'knowledge conflict reviewer grant is invalid or revoked' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_conflict_review_guard
        BEFORE INSERT OR UPDATE OR DELETE ON knowledge_conflict_reviews
        FOR EACH ROW EXECUTE FUNCTION knowledge_conflict_review_guard()
        """
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_conflict_state_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'knowledge conflict history cannot be deleted' USING ERRCODE = 'check_violation';
            END IF;
            IF (NEW.id, NEW.revision_id, NEW.compared_revision_id, NEW.detected_at) IS DISTINCT FROM
               (OLD.id, OLD.revision_id, OLD.compared_revision_id, OLD.detected_at) THEN
                RAISE EXCEPTION 'knowledge conflict identity is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.status <> 'open' OR NEW.status NOT IN ('confirmed', 'dismissed')
               OR NOT EXISTS (
                    SELECT 1 FROM knowledge_conflict_reviews r
                    WHERE r.conflict_id = OLD.id AND r.decision = NEW.status
               ) THEN
                RAISE EXCEPTION 'knowledge conflict status requires a recorded review event' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_conflict_state_guard
        BEFORE UPDATE OR DELETE ON knowledge_conflicts
        FOR EACH ROW EXECUTE FUNCTION knowledge_conflict_state_guard()
        """
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM knowledge_conflicts) OR EXISTS (SELECT 1 FROM knowledge_conflict_reviews) THEN
                RAISE EXCEPTION 'cannot downgrade while knowledge conflict review history exists';
            END IF;
        END $$;"""
    )
    op.execute("DROP TRIGGER IF EXISTS knowledge_conflict_state_guard ON knowledge_conflicts")
    op.execute("DROP FUNCTION IF EXISTS knowledge_conflict_state_guard()")
    op.execute("DROP TRIGGER IF EXISTS knowledge_conflict_review_guard ON knowledge_conflict_reviews")
    op.execute("DROP FUNCTION IF EXISTS knowledge_conflict_review_guard()")
    op.execute("DROP TRIGGER IF EXISTS knowledge_conflict_pair_guard ON knowledge_conflicts")
    op.execute("DROP FUNCTION IF EXISTS knowledge_conflict_pair_guard()")
    op.drop_index("ix_knowledge_conflict_reviews_reviewer_time", table_name="knowledge_conflict_reviews")
    op.drop_table("knowledge_conflict_reviews")
    op.drop_index("ix_knowledge_conflicts_status_detected", table_name="knowledge_conflicts")
    op.drop_table("knowledge_conflicts")
