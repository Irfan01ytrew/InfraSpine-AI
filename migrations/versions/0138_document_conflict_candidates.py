"""Step AI slice 3: human-review candidates for contradictory document passages.

`document_conflict_candidates` stores chunk-id pairs only — never passage text —
so detection alone exposes nothing; a reviewer re-checks document ACL before
reading either passage (slice 5 queue). `document_conflict_reviews` is the
append-only human decision, mirroring `knowledge_conflict_reviews`.

Revision ID: 0138_document_conflict_candidates
Revises: 0104_document_freshness
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0138_document_conflict_candidates"
down_revision = "0137_document_freshness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_conflict_candidates",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "left_chunk_id",
            sa.BigInteger(),
            sa.ForeignKey("document_chunks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "right_chunk_id",
            sa.BigInteger(),
            sa.ForeignKey("document_chunks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("detection_source", sa.String(32), nullable=False, server_default="nightly_scan"),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("left_chunk_id < right_chunk_id", name="ck_document_conflict_candidate_ordered"),
        sa.CheckConstraint(
            "status IN ('open', 'confirmed', 'dismissed')",
            name="ck_document_conflict_candidate_status_valid",
        ),
        sa.UniqueConstraint("left_chunk_id", "right_chunk_id", name="uq_document_conflict_candidate_pair"),
    )
    op.create_index(
        "ix_document_conflict_candidates_status_detected",
        "document_conflict_candidates",
        ["status", "detected_at"],
    )

    op.create_table(
        "document_conflict_reviews",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "candidate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_conflict_candidates.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reviewer_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("rationale", sa.String(2000), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('confirmed', 'dismissed')",
            name="ck_document_conflict_reviews_decision_valid",
        ),
        sa.CheckConstraint(
            "length(btrim(rationale)) BETWEEN 3 AND 2000",
            name="ck_document_conflict_reviews_rationale_length",
        ),
        sa.UniqueConstraint("candidate_id", name="uq_document_conflict_review_once"),
    )
    op.create_index(
        "ix_document_conflict_reviews_reviewer_time",
        "document_conflict_reviews",
        ["reviewer_user_id", "decided_at"],
    )

    op.execute(
        """
        CREATE FUNCTION document_conflict_pair_guard() RETURNS trigger AS $$
        DECLARE
            left_doc documents%ROWTYPE;
            right_doc documents%ROWTYPE;
        BEGIN
            SELECT d.* INTO left_doc FROM documents d
              JOIN document_chunks c ON c.document_id = d.id
              WHERE c.id = NEW.left_chunk_id FOR SHARE;
            SELECT d.* INTO right_doc FROM documents d
              JOIN document_chunks c ON c.document_id = d.id
              WHERE c.id = NEW.right_chunk_id FOR SHARE;
            IF NOT FOUND OR left_doc.id IS NULL OR right_doc.id IS NULL
               OR left_doc.status <> 'ready' OR right_doc.status <> 'ready' THEN
                RAISE EXCEPTION 'document conflict chunks must belong to ready documents' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status <> 'open' THEN
                RAISE EXCEPTION 'document conflict starts open' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER document_conflict_pair_guard
        BEFORE INSERT ON document_conflict_candidates
        FOR EACH ROW EXECUTE FUNCTION document_conflict_pair_guard()
        """
    )
    op.execute(
        """
        CREATE FUNCTION document_conflict_review_guard() RETURNS trigger AS $$
        DECLARE
            c document_conflict_candidates%ROWTYPE;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'document conflict review history is append-only' USING ERRCODE = 'check_violation';
            END IF;
            SELECT * INTO c FROM document_conflict_candidates WHERE id = NEW.candidate_id FOR UPDATE;
            IF NOT FOUND OR c.status <> 'open' OR NEW.decision NOT IN ('confirmed', 'dismissed') THEN
                RAISE EXCEPTION 'document conflict is no longer open' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.reviewer_user_id AND is_active) THEN
                RAISE EXCEPTION 'document conflict reviewer is inactive' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER document_conflict_review_guard
        BEFORE INSERT OR UPDATE OR DELETE ON document_conflict_reviews
        FOR EACH ROW EXECUTE FUNCTION document_conflict_review_guard()
        """
    )
    op.execute(
        """
        CREATE FUNCTION document_conflict_state_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'document conflict history cannot be deleted' USING ERRCODE = 'check_violation';
            END IF;
            IF (NEW.id, NEW.left_chunk_id, NEW.right_chunk_id, NEW.detected_at) IS DISTINCT FROM
               (OLD.id, OLD.left_chunk_id, OLD.right_chunk_id, OLD.detected_at) THEN
                RAISE EXCEPTION 'document conflict identity is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.status <> 'open' OR NEW.status NOT IN ('confirmed', 'dismissed')
               OR NOT EXISTS (
                    SELECT 1 FROM document_conflict_reviews r
                    WHERE r.candidate_id = OLD.id AND r.decision = NEW.status
               ) THEN
                RAISE EXCEPTION 'document conflict status requires a recorded review event' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER document_conflict_state_guard
        BEFORE UPDATE OR DELETE ON document_conflict_candidates
        FOR EACH ROW EXECUTE FUNCTION document_conflict_state_guard()
        """
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM document_conflict_candidates)
               OR EXISTS (SELECT 1 FROM document_conflict_reviews) THEN
                RAISE EXCEPTION 'cannot downgrade while document conflict review history exists';
            END IF;
        END $$;"""
    )
    op.execute("DROP TRIGGER IF EXISTS document_conflict_state_guard ON document_conflict_candidates")
    op.execute("DROP FUNCTION IF EXISTS document_conflict_state_guard()")
    op.execute("DROP TRIGGER IF EXISTS document_conflict_review_guard ON document_conflict_reviews")
    op.execute("DROP FUNCTION IF EXISTS document_conflict_review_guard()")
    op.execute("DROP TRIGGER IF EXISTS document_conflict_pair_guard ON document_conflict_candidates")
    op.execute("DROP FUNCTION IF EXISTS document_conflict_pair_guard()")
    op.drop_index("ix_document_conflict_reviews_reviewer_time", table_name="document_conflict_reviews")
    op.drop_table("document_conflict_reviews")
    op.drop_index("ix_document_conflict_candidates_status_detected", table_name="document_conflict_candidates")
    op.drop_table("document_conflict_candidates")
