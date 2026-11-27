"""Unverified claims capture (ADR-004 WP3): knowledge claims, immutable history, claimant-identity
foreign key on governance_approvals, and decision-time checks that the claim is still submitted
and that category/scope match.

Claims are never published into approved knowledge. Review and publish remain WP4+.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATEGORIES = "('policy', 'financial', 'legal', 'client', 'operational', 'general')"


def upgrade() -> None:
    op.create_table(
        "knowledge_claims",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("claimant_user_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("scope_group_id", sa.UUID(), nullable=True),
        sa.Column("sources", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'submitted'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('proposal', 'correction')", name=op.f("ck_knowledge_claims_kind_valid")),
        sa.CheckConstraint("status IN ('submitted', 'withdrawn')", name=op.f("ck_knowledge_claims_status_valid")),
        sa.CheckConstraint(f"category IN {CATEGORIES}", name=op.f("ck_knowledge_claims_category_valid")),
        sa.CheckConstraint(
            "length(btrim(statement)) BETWEEN 1 AND 4000", name=op.f("ck_knowledge_claims_statement_length")
        ),
        sa.CheckConstraint("jsonb_typeof(sources) = 'array'", name=op.f("ck_knowledge_claims_sources_is_array")),
        sa.CheckConstraint(
            "jsonb_array_length(sources) BETWEEN 0 AND 20", name=op.f("ck_knowledge_claims_sources_count")
        ),
        sa.CheckConstraint(
            "(status = 'withdrawn') = (withdrawn_at IS NOT NULL)", name=op.f("ck_knowledge_claims_withdrawn_has_time")
        ),
        sa.ForeignKeyConstraint(
            ["claimant_user_id"], ["users.id"], name=op.f("fk_knowledge_claims_claimant_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["scope_group_id"], ["access_groups.id"], name=op.f("fk_knowledge_claims_scope_group_id_access_groups")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_claims")),
        sa.UniqueConstraint("id", "claimant_user_id", name="uq_knowledge_claims_identity"),
    )
    op.create_index("ix_knowledge_claims_claimant_recent", "knowledge_claims", ["claimant_user_id", "created_at"])

    op.create_table(
        "knowledge_claim_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("action IN ('submitted', 'withdrawn')", name=op.f("ck_knowledge_claim_events_action_valid")),
        sa.CheckConstraint("length(btrim(to_status)) > 0", name=op.f("ck_knowledge_claim_events_to_status_required")),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["knowledge_claims.id"], name=op.f("fk_knowledge_claim_events_claim_id_knowledge_claims")
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], name=op.f("fk_knowledge_claim_events_actor_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_claim_events")),
    )
    op.create_index("ix_knowledge_claim_events_claim", "knowledge_claim_events", ["claim_id", "id"])

    # Claimant, statement, kind, category, scope and sources cannot change. The only mutation is
    # submitted → withdrawn. Deletes are refused so history stays intact.
    op.execute(
        """
        CREATE FUNCTION knowledge_claims_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'submitted' OR NEW.withdrawn_at IS NOT NULL THEN
                    RAISE EXCEPTION 'a claim must be created as submitted' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            ELSIF TG_OP = 'UPDATE' THEN
                IF (NEW.id, NEW.claimant_user_id, NEW.kind, NEW.statement, NEW.category,
                    NEW.scope_group_id, NEW.sources, NEW.created_at)
                   IS DISTINCT FROM
                   (OLD.id, OLD.claimant_user_id, OLD.kind, OLD.statement, OLD.category,
                    OLD.scope_group_id, OLD.sources, OLD.created_at)
                THEN
                    RAISE EXCEPTION 'claim identity and statement are immutable'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF OLD.status <> 'submitted' OR NEW.status <> 'withdrawn' OR NEW.withdrawn_at IS NULL THEN
                    RAISE EXCEPTION 'a claim can only be withdrawn' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'knowledge claims are never deleted' USING ERRCODE = 'check_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_claims_guard
        BEFORE INSERT OR UPDATE OR DELETE ON knowledge_claims
        FOR EACH ROW EXECUTE FUNCTION knowledge_claims_guard();
        """
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_claims_record_event() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                INSERT INTO knowledge_claim_events (claim_id, actor_user_id, action, from_status, to_status)
                VALUES (NEW.id, NEW.claimant_user_id, 'submitted', NULL, NEW.status);
            ELSE
                INSERT INTO knowledge_claim_events (claim_id, actor_user_id, action, from_status, to_status)
                VALUES (NEW.id, NEW.claimant_user_id, 'withdrawn', OLD.status, NEW.status);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_claims_record_event
        AFTER INSERT OR UPDATE ON knowledge_claims
        FOR EACH ROW EXECUTE FUNCTION knowledge_claims_record_event();
        """
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_claim_events_block_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'knowledge_claim_events is append-only' USING ERRCODE = 'check_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_claim_events_append_only
        BEFORE UPDATE OR DELETE ON knowledge_claim_events
        FOR EACH ROW EXECUTE FUNCTION knowledge_claim_events_block_mutation();
        """
    )

    # Bind every decision to a real claim and its true claimant. Orphan WP1 test rows are gone
    # after a full downgrade; a live database must have no approvals before this constraint.
    op.create_foreign_key(
        "fk_governance_approvals_claim_identity",
        "governance_approvals",
        "knowledge_claims",
        ["subject_id", "subject_author_user_id"],
        ["id", "claimant_user_id"],
    )

    # Re-create the decision-time trigger so it also locks the claim and checks submitted /
    # category / scope. Grant checks stay first so existing error messages still apply.
    op.execute("DROP TRIGGER IF EXISTS governance_approvals_check ON governance_approvals")
    op.execute("DROP FUNCTION IF EXISTS governance_approvals_check()")
    op.execute(
        """
        CREATE FUNCTION governance_approvals_check() RETURNS trigger AS $$
        DECLARE
            g permission_grants%ROWTYPE;
            c knowledge_claims%ROWTYPE;
        BEGIN
            SELECT * INTO g FROM permission_grants WHERE id = NEW.permission_grant_id FOR SHARE;
            IF g.revoked_at IS NOT NULL THEN
                RAISE EXCEPTION 'the permission grant is revoked' USING ERRCODE = 'check_violation';
            END IF;
            IF g.category IS NOT NULL AND g.category <> NEW.category THEN
                RAISE EXCEPTION 'the permission grant does not cover this category' USING ERRCODE = 'check_violation';
            END IF;
            IF g.scope_group_id IS NOT NULL AND g.scope_group_id IS DISTINCT FROM NEW.scope_group_id THEN
                RAISE EXCEPTION 'the permission grant does not cover this scope' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.approver_user_id AND is_active) THEN
                RAISE EXCEPTION 'the approver is not an active user' USING ERRCODE = 'check_violation';
            END IF;
            SELECT * INTO c FROM knowledge_claims
            WHERE id = NEW.subject_id AND claimant_user_id = NEW.subject_author_user_id
            FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'approval must reference a matching knowledge claim'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF c.status <> 'submitted' THEN
                RAISE EXCEPTION 'approval requires a submitted claim' USING ERRCODE = 'check_violation';
            END IF;
            IF c.category <> NEW.category THEN
                RAISE EXCEPTION 'approval category must match the claim' USING ERRCODE = 'check_violation';
            END IF;
            IF c.scope_group_id IS DISTINCT FROM NEW.scope_group_id THEN
                RAISE EXCEPTION 'approval scope must match the claim' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.stage = 'publish' AND NOT EXISTS (
                SELECT 1 FROM governance_approvals
                WHERE subject_type = NEW.subject_type AND subject_id = NEW.subject_id AND stage = 'review'
            ) THEN
                RAISE EXCEPTION 'a publish approval requires a prior review approval' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER governance_approvals_check
        BEFORE INSERT ON governance_approvals
        FOR EACH ROW EXECUTE FUNCTION governance_approvals_check();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS governance_approvals_check ON governance_approvals")
    op.execute("DROP FUNCTION IF EXISTS governance_approvals_check()")
    op.drop_constraint("fk_governance_approvals_claim_identity", "governance_approvals", type_="foreignkey")
    # Restore the WP1 decision-time function (grant checks only; claims no longer exist).
    op.execute(
        """
        CREATE FUNCTION governance_approvals_check() RETURNS trigger AS $$
        DECLARE
            g permission_grants%ROWTYPE;
        BEGIN
            SELECT * INTO g FROM permission_grants WHERE id = NEW.permission_grant_id FOR SHARE;
            IF g.revoked_at IS NOT NULL THEN
                RAISE EXCEPTION 'the permission grant is revoked' USING ERRCODE = 'check_violation';
            END IF;
            IF g.category IS NOT NULL AND g.category <> NEW.category THEN
                RAISE EXCEPTION 'the permission grant does not cover this category' USING ERRCODE = 'check_violation';
            END IF;
            IF g.scope_group_id IS NOT NULL AND g.scope_group_id IS DISTINCT FROM NEW.scope_group_id THEN
                RAISE EXCEPTION 'the permission grant does not cover this scope' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.approver_user_id AND is_active) THEN
                RAISE EXCEPTION 'the approver is not an active user' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.stage = 'publish' AND NOT EXISTS (
                SELECT 1 FROM governance_approvals
                WHERE subject_type = NEW.subject_type AND subject_id = NEW.subject_id AND stage = 'review'
            ) THEN
                RAISE EXCEPTION 'a publish approval requires a prior review approval' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER governance_approvals_check
        BEFORE INSERT ON governance_approvals
        FOR EACH ROW EXECUTE FUNCTION governance_approvals_check();
        """
    )

    op.execute("DROP TRIGGER IF EXISTS knowledge_claim_events_append_only ON knowledge_claim_events")
    op.execute("DROP FUNCTION IF EXISTS knowledge_claim_events_block_mutation()")
    op.execute("DROP TRIGGER IF EXISTS knowledge_claims_record_event ON knowledge_claims")
    op.execute("DROP FUNCTION IF EXISTS knowledge_claims_record_event()")
    op.execute("DROP TRIGGER IF EXISTS knowledge_claims_guard ON knowledge_claims")
    op.execute("DROP FUNCTION IF EXISTS knowledge_claims_guard()")
    op.drop_table("knowledge_claim_events")
    op.drop_table("knowledge_claims")
