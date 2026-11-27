"""Two-person approval and ACL-filtered search for organizational knowledge.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_knowledge_revisions_status_valid"), "knowledge_revisions", type_="check")
    op.create_check_constraint(
        op.f("ck_knowledge_revisions_status_valid"),
        "knowledge_revisions",
        "status IN ('draft', 'submitted', 'published', 'superseded', 'withdrawn')",
    )
    op.drop_constraint(op.f("ck_governance_approvals_subject_type_valid"), "governance_approvals", type_="check")
    op.create_check_constraint(
        op.f("ck_governance_approvals_subject_type_valid"),
        "governance_approvals",
        "subject_type IN ('knowledge_claim', 'knowledge_revision')",
    )
    op.drop_constraint(op.f("fk_governance_approvals_claim_identity"), "governance_approvals", type_="foreignkey")

    # Conditional subject identity is enforced here because the approval table serves two immutable tables.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION governance_approvals_check() RETURNS trigger AS $$
        DECLARE
            g permission_grants%ROWTYPE;
            c knowledge_claims%ROWTYPE;
            r knowledge_revisions%ROWTYPE;
        BEGIN
            SELECT * INTO g FROM permission_grants WHERE id = NEW.permission_grant_id FOR SHARE;
            IF NOT FOUND OR g.revoked_at IS NOT NULL THEN
                RAISE EXCEPTION 'the permission grant is revoked or missing' USING ERRCODE = 'check_violation';
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

            IF NEW.subject_type = 'knowledge_claim' THEN
                SELECT * INTO c FROM knowledge_claims
                WHERE id = NEW.subject_id AND claimant_user_id = NEW.subject_author_user_id FOR SHARE;
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
            ELSIF NEW.subject_type = 'knowledge_revision' THEN
                SELECT * INTO r FROM knowledge_revisions
                WHERE id = NEW.subject_id AND author_user_id = NEW.subject_author_user_id FOR SHARE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'approval must reference a matching knowledge revision'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF r.status <> 'submitted' THEN
                    RAISE EXCEPTION 'approval requires a submitted knowledge revision' USING ERRCODE = 'check_violation';
                END IF;
                IF r.category <> NEW.category THEN
                    RAISE EXCEPTION 'approval category must match the knowledge revision' USING ERRCODE = 'check_violation';
                END IF;
                IF r.scope_group_id IS DISTINCT FROM NEW.scope_group_id THEN
                    RAISE EXCEPTION 'approval scope must match the knowledge revision' USING ERRCODE = 'check_violation';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM knowledge_evidence e
                    LEFT JOIN documents d ON d.id = e.document_id
                    WHERE e.revision_id = r.id AND (
                        e.document_id IS NULL OR e.orphan_reason IS NOT NULL OR d.id IS NULL
                        OR d.acl_status <> 'active' OR d.version <> e.document_version
                        OR d.indexed_version <> e.document_version OR d.sha256 <> e.document_sha256
                    )
                ) OR NOT EXISTS (SELECT 1 FROM knowledge_evidence WHERE revision_id = r.id) THEN
                    RAISE EXCEPTION 'knowledge evidence must be current at approval time'
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSE
                RAISE EXCEPTION 'unknown governance subject type' USING ERRCODE = 'check_violation';
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
        CREATE OR REPLACE FUNCTION knowledge_revisions_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'knowledge revisions are never deleted' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status = 'published' THEN
                    RAISE EXCEPTION 'knowledge cannot be published outside the approval workflow'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.status <> 'draft' THEN
                    RAISE EXCEPTION 'a knowledge revision must be created as a draft' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF (NEW.id, NEW.entry_id, NEW.author_user_id, NEW.revision_number, NEW.statement, NEW.category,
                NEW.visibility, NEW.scope_group_id, NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.entry_id, OLD.author_user_id, OLD.revision_number, OLD.statement, OLD.category,
                OLD.visibility, OLD.scope_group_id, OLD.created_at) THEN
                RAISE EXCEPTION 'knowledge revision content is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status THEN
                IF NEW.status = 'published' AND OLD.status <> 'submitted' THEN
                    RAISE EXCEPTION 'knowledge cannot be published outside the approval workflow'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF OLD.status = 'draft' AND NEW.status IN ('submitted', 'superseded', 'withdrawn') THEN
                    IF NEW.status = 'submitted' THEN
                        IF NEW.visibility = 'private' THEN
                            RAISE EXCEPTION 'private knowledge cannot be submitted for approval'
                                USING ERRCODE = 'check_violation';
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM knowledge_evidence WHERE revision_id = NEW.id) OR EXISTS (
                            SELECT 1 FROM knowledge_evidence e
                            LEFT JOIN documents d ON d.id = e.document_id
                            WHERE e.revision_id = NEW.id AND (
                                e.document_id IS NULL OR e.orphan_reason IS NOT NULL OR d.id IS NULL
                                OR d.acl_status <> 'active' OR d.version <> e.document_version
                                OR d.indexed_version <> e.document_version OR d.sha256 <> e.document_sha256
                            )
                        ) THEN
                            RAISE EXCEPTION 'knowledge evidence must be current before submission'
                                USING ERRCODE = 'check_violation';
                        END IF;
                    END IF;
                ELSIF OLD.status = 'submitted' AND NEW.status = 'withdrawn' THEN
                    NULL;
                ELSIF OLD.status = 'submitted' AND NEW.status = 'published' THEN
                    IF NOT EXISTS (
                        SELECT 1 FROM governance_approvals a
                        WHERE a.subject_type = 'knowledge_revision' AND a.subject_id = OLD.id AND a.stage = 'review'
                    ) OR NOT EXISTS (
                        SELECT 1 FROM governance_approvals a
                        WHERE a.subject_type = 'knowledge_revision' AND a.subject_id = OLD.id AND a.stage = 'publish'
                    ) THEN
                        RAISE EXCEPTION 'knowledge publication requires review and publish approvals'
                            USING ERRCODE = 'check_violation';
                    END IF;
                ELSIF OLD.status = 'published' AND NEW.status = 'withdrawn' THEN
                    NULL;
                ELSIF OLD.status = 'published' AND NEW.status = 'superseded' AND EXISTS (
                    SELECT 1 FROM knowledge_revisions newer
                    WHERE newer.entry_id = OLD.entry_id AND newer.status = 'published'
                      AND newer.revision_number > OLD.revision_number
                ) THEN
                    NULL;
                ELSE
                    RAISE EXCEPTION 'knowledge revision status transition is not allowed'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE FUNCTION governance_approvals_publish_knowledge_revision() RETURNS trigger AS $$
        DECLARE
            r knowledge_revisions%ROWTYPE;
        BEGIN
            IF NEW.subject_type = 'knowledge_revision' AND NEW.stage = 'publish' THEN
                UPDATE knowledge_revisions SET status = 'published'
                WHERE id = NEW.subject_id AND status = 'submitted';
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'knowledge revision is no longer awaiting publication'
                        USING ERRCODE = 'check_violation';
                END IF;
                SELECT * INTO r FROM knowledge_revisions WHERE id = NEW.subject_id;
                UPDATE knowledge_revisions SET status = 'superseded'
                WHERE entry_id = r.entry_id AND id <> r.id AND status = 'published'
                  AND revision_number < r.revision_number;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER governance_approvals_publish_knowledge_revision
        AFTER INSERT ON governance_approvals
        FOR EACH ROW EXECUTE FUNCTION governance_approvals_publish_knowledge_revision();
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_revisions_published_search
        ON knowledge_revisions USING gin (to_tsvector('simple', statement))
        WHERE status = 'published'
        """
    )


def downgrade() -> None:
    # Approval history is append-only. Refuse a downgrade that would orphan decisions or
    # discard the new lifecycle states; callers must export/migrate these records first.
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM governance_approvals WHERE subject_type = 'knowledge_revision')
               OR EXISTS (SELECT 1 FROM knowledge_revisions WHERE status IN ('submitted', 'published')) THEN
                RAISE EXCEPTION 'cannot downgrade while approved organizational knowledge exists';
            END IF;
        END $$
        """
    )
    op.drop_index("ix_knowledge_revisions_published_search", table_name="knowledge_revisions")
    op.execute("DROP TRIGGER IF EXISTS governance_approvals_publish_knowledge_revision ON governance_approvals")
    op.execute("DROP FUNCTION IF EXISTS governance_approvals_publish_knowledge_revision()")
    # The prior guard's approved-state branch was the WP4 publication refusal.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION knowledge_revisions_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'knowledge revisions are never deleted' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'published' THEN
                RAISE EXCEPTION 'knowledge cannot be published by this workflow' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'draft' THEN
                    RAISE EXCEPTION 'a knowledge revision must be created as a draft' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF (NEW.id, NEW.entry_id, NEW.author_user_id, NEW.revision_number, NEW.statement, NEW.category,
                NEW.visibility, NEW.scope_group_id, NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.entry_id, OLD.author_user_id, OLD.revision_number, OLD.statement, OLD.category,
                OLD.visibility, OLD.scope_group_id, OLD.created_at) THEN
                RAISE EXCEPTION 'knowledge revision content is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                OLD.status = 'draft' AND NEW.status IN ('superseded', 'withdrawn')
            ) THEN
                RAISE EXCEPTION 'a draft revision can only be superseded or withdrawn' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.drop_constraint(op.f("ck_governance_approvals_subject_type_valid"), "governance_approvals", type_="check")
    op.create_check_constraint(
        op.f("ck_governance_approvals_subject_type_valid"),
        "governance_approvals",
        "subject_type IN ('knowledge_claim')",
    )
    op.create_foreign_key(
        op.f("fk_governance_approvals_claim_identity"), "governance_approvals", "knowledge_claims",
        ["subject_id", "subject_author_user_id"], ["id", "claimant_user_id"],
    )
    # Restore the previous claim-only decision trigger.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION governance_approvals_check() RETURNS trigger AS $$
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
            WHERE id = NEW.subject_id AND claimant_user_id = NEW.subject_author_user_id FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'approval must reference a matching knowledge claim' USING ERRCODE = 'check_violation';
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
    op.drop_constraint(op.f("ck_knowledge_revisions_status_valid"), "knowledge_revisions", type_="check")
    op.create_check_constraint(
        op.f("ck_knowledge_revisions_status_valid"),
        "knowledge_revisions",
        "status IN ('draft', 'published', 'superseded', 'withdrawn')",
    )
