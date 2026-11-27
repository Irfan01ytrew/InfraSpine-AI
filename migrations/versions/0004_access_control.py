"""Access control (ADR-004 WP1): groups, resource ACLs, source-ACL quarantine, permission grants and
database-enforced separation of duties for review/publish decisions.

Existing documents keep their behaviour: they become `source_system = 'upload'`, `acl_status = 'active'`,
and private/shared visibility is unchanged.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATEGORIES = "('policy', 'financial', 'legal', 'client', 'operational', 'general')"


def upgrade() -> None:
    # ------------------------------------------------------------------ principals
    op.add_column("users", sa.Column("external_id", sa.String(length=256), nullable=True))
    op.create_index(
        "uq_users_external_id", "users", ["external_id"], unique=True, postgresql_where=sa.text("external_id IS NOT NULL")
    )

    op.create_table(
        "access_groups",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), server_default=sa.text("'local'"), nullable=False),
        sa.Column("external_id", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('department', 'project', 'client', 'custom')", name=op.f("ck_access_groups_kind_valid")
        ),
        sa.CheckConstraint("source IN ('local', 'directory')", name=op.f("ck_access_groups_source_valid")),
        sa.CheckConstraint(
            "source = 'local' OR external_id IS NOT NULL", name=op.f("ck_access_groups_directory_has_external_id")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_access_groups_created_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_groups")),
    )
    op.create_index("uq_access_groups_kind_name", "access_groups", ["kind", sa.text("lower(name)")], unique=True)
    op.create_index(
        "uq_access_groups_external_id",
        "access_groups",
        ["external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )

    op.create_table(
        "access_group_members",
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("added_by", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["group_id"], ["access_groups.id"], name=op.f("fk_access_group_members_group_id_access_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_access_group_members_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["added_by"], ["users.id"], name=op.f("fk_access_group_members_added_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("group_id", "user_id", name=op.f("pk_access_group_members")),
    )
    op.create_index(op.f("ix_access_group_members_user_id"), "access_group_members", ["user_id"])

    # ------------------------------------------------------------------ documents
    op.drop_constraint(op.f("ck_documents_visibility_valid"), "documents", type_="check")
    op.create_check_constraint(
        op.f("ck_documents_visibility_valid"), "documents", "visibility IN ('private', 'shared', 'restricted')"
    )
    op.add_column(
        "documents", sa.Column("source_system", sa.String(length=16), server_default=sa.text("'upload'"), nullable=False)
    )
    op.add_column("documents", sa.Column("source_uri", sa.String(length=1024), nullable=True))
    op.add_column(
        "documents", sa.Column("acl_status", sa.String(length=16), server_default=sa.text("'active'"), nullable=False)
    )
    op.add_column("documents", sa.Column("acl_quarantine_reason", sa.String(length=64), nullable=True))
    op.add_column("documents", sa.Column("acl_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_documents_source_system_valid"), "documents", "source_system IN ('upload', 'file_server')"
    )
    op.create_check_constraint(
        op.f("ck_documents_acl_status_valid"), "documents", "acl_status IN ('active', 'quarantined')"
    )
    op.create_check_constraint(
        op.f("ck_documents_quarantine_has_reason"),
        "documents",
        "(acl_status = 'active') = (acl_quarantine_reason IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_documents_synced_is_restricted"), "documents", "source_system = 'upload' OR visibility = 'restricted'"
    )
    op.create_index(
        "ix_documents_quarantined", "documents", ["created_at"], postgresql_where=sa.text("acl_status = 'quarantined'")
    )
    # Fail closed: a synced document is unreadable until its first valid ACL sync, whatever the writer sent.
    op.execute(
        """
        CREATE FUNCTION documents_synced_start_quarantined() RETURNS trigger AS $$
        BEGIN
            IF NEW.source_system <> 'upload'
               AND (TG_OP = 'INSERT' OR NEW.source_system IS DISTINCT FROM OLD.source_system) THEN
                NEW.acl_status := 'quarantined';
                NEW.acl_quarantine_reason := 'acl_not_synced';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER documents_synced_start_quarantined
        BEFORE INSERT OR UPDATE OF source_system ON documents
        FOR EACH ROW EXECUTE FUNCTION documents_synced_start_quarantined();
        """
    )

    # ------------------------------------------------------------------ resource ACL
    op.create_table(
        "resource_acl",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=True),
        sa.Column("principal_user_id", sa.UUID(), nullable=True),
        sa.Column("principal_group_id", sa.UUID(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.CheckConstraint("resource_type IN ('document')", name=op.f("ck_resource_acl_resource_type_valid")),
        sa.CheckConstraint(
            "(resource_type = 'document') = (document_id IS NOT NULL)", name=op.f("ck_resource_acl_resource_matches_type")
        ),
        sa.CheckConstraint(
            "num_nonnulls(principal_user_id, principal_group_id) = 1", name=op.f("ck_resource_acl_one_principal")
        ),
        sa.CheckConstraint("origin IN ('manual', 'source_sync')", name=op.f("ck_resource_acl_origin_valid")),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name=op.f("fk_resource_acl_document_id_documents"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["principal_user_id"], ["users.id"], name=op.f("fk_resource_acl_principal_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["principal_group_id"], ["access_groups.id"],
            name=op.f("fk_resource_acl_principal_group_id_access_groups"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_resource_acl_created_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resource_acl")),
        sa.UniqueConstraint(
            "document_id", "principal_user_id", "principal_group_id",
            name="uq_resource_acl_entry", postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(op.f("ix_resource_acl_principal_user_id"), "resource_acl", ["principal_user_id"])
    op.create_index(op.f("ix_resource_acl_principal_group_id"), "resource_acl", ["principal_group_id"])

    # ------------------------------------------------------------------ permission grants
    op.create_table(
        "permission_grants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("permission", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=True),
        sa.Column("scope_group_id", sa.UUID(), nullable=True),
        sa.Column("granted_by", sa.UUID(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "permission IN ('knowledge.review', 'knowledge.publish', 'data.steward')",
            name=op.f("ck_permission_grants_permission_valid"),
        ),
        sa.CheckConstraint(
            f"category IS NULL OR category IN {CATEGORIES}", name=op.f("ck_permission_grants_category_valid")
        ),
        sa.CheckConstraint("granted_by <> user_id", name=op.f("ck_permission_grants_no_self_grant")),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by IS NULL)", name=op.f("ck_permission_grants_revocation_complete")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_permission_grants_user_id_users")),
        sa.ForeignKeyConstraint(
            ["scope_group_id"], ["access_groups.id"], name=op.f("fk_permission_grants_scope_group_id_access_groups")
        ),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], name=op.f("fk_permission_grants_granted_by_users")),
        sa.ForeignKeyConstraint(["revoked_by"], ["users.id"], name=op.f("fk_permission_grants_revoked_by_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permission_grants")),
        sa.UniqueConstraint("id", "user_id", "permission", name="uq_permission_grants_owner"),
    )
    op.create_index(op.f("ix_permission_grants_user_id"), "permission_grants", ["user_id"])
    op.create_index(
        "uq_permission_grants_active",
        "permission_grants",
        ["user_id", "permission", "category", "scope_group_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        postgresql_nulls_not_distinct=True,
    )
    # One-time checks a CHECK constraint cannot express (they read other rows): the grantor is an
    # active administrator at grant time; afterwards a grant can only be revoked, once, and never deleted.
    op.execute(
        """
        CREATE FUNCTION permission_grants_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.revoked_at IS NOT NULL THEN
                    RAISE EXCEPTION 'a permission grant cannot be created revoked' USING ERRCODE = 'check_violation';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM users WHERE id = NEW.granted_by AND role = 'admin' AND is_active
                ) THEN
                    RAISE EXCEPTION 'permission grants must be issued by an active administrator'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            ELSIF TG_OP = 'UPDATE' THEN
                IF OLD.revoked_at IS NOT NULL THEN
                    RAISE EXCEPTION 'permission grant is already revoked' USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.revoked_at IS NULL
                   OR (NEW.id, NEW.user_id, NEW.permission, NEW.category, NEW.scope_group_id, NEW.granted_by, NEW.granted_at)
                      IS DISTINCT FROM
                      (OLD.id, OLD.user_id, OLD.permission, OLD.category, OLD.scope_group_id, OLD.granted_by, OLD.granted_at)
                THEN
                    RAISE EXCEPTION 'a permission grant can only be revoked' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'permission grants are never deleted' USING ERRCODE = 'check_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER permission_grants_guard
        BEFORE INSERT OR UPDATE OR DELETE ON permission_grants
        FOR EACH ROW EXECUTE FUNCTION permission_grants_guard();
        """
    )

    # ------------------------------------------------------------------ separation of duties
    op.create_table(
        "governance_approvals",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.UUID(), nullable=False),
        sa.Column("subject_author_user_id", sa.UUID(), nullable=False),
        sa.Column("approver_user_id", sa.UUID(), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("permission", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("scope_group_id", sa.UUID(), nullable=True),
        sa.Column("permission_grant_id", sa.UUID(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "subject_type IN ('knowledge_claim')", name=op.f("ck_governance_approvals_subject_type_valid")
        ),
        sa.CheckConstraint("stage IN ('review', 'publish')", name=op.f("ck_governance_approvals_stage_valid")),
        sa.CheckConstraint(
            "(stage = 'review' AND permission = 'knowledge.review') "
            "OR (stage = 'publish' AND permission = 'knowledge.publish')",
            name=op.f("ck_governance_approvals_stage_matches_permission"),
        ),
        sa.CheckConstraint(f"category IN {CATEGORIES}", name=op.f("ck_governance_approvals_category_valid")),
        sa.CheckConstraint(
            "approver_user_id <> subject_author_user_id", name=op.f("ck_governance_approvals_no_self_approval")
        ),
        sa.CheckConstraint(
            "length(btrim(rationale)) > 0", name=op.f("ck_governance_approvals_rationale_required")
        ),
        sa.ForeignKeyConstraint(
            ["subject_author_user_id"], ["users.id"], name=op.f("fk_governance_approvals_subject_author_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["approver_user_id"], ["users.id"], name=op.f("fk_governance_approvals_approver_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["scope_group_id"], ["access_groups.id"],
            name=op.f("fk_governance_approvals_scope_group_id_access_groups"),
        ),
        sa.ForeignKeyConstraint(
            ["permission_grant_id", "approver_user_id", "permission"],
            ["permission_grants.id", "permission_grants.user_id", "permission_grants.permission"],
            name="fk_governance_approvals_own_grant",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_governance_approvals")),
        sa.UniqueConstraint(
            "subject_type", "subject_id", "approver_user_id", name="uq_governance_approvals_one_role"
        ),
        sa.UniqueConstraint("subject_type", "subject_id", "stage", name="uq_governance_approvals_one_stage"),
    )
    # Decision-time checks across rows and tables. FOR SHARE on the grant serialises against a
    # concurrent revocation: whichever commits first wins, and a revoked grant is never used.
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
    op.execute(
        """
        CREATE FUNCTION governance_approvals_block_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'governance_approvals is append-only' USING ERRCODE = 'check_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER governance_approvals_append_only
        BEFORE UPDATE OR DELETE ON governance_approvals
        FOR EACH ROW EXECUTE FUNCTION governance_approvals_block_mutation();
        """
    )


def downgrade() -> None:
    # Never widen access on the way down: synced documents have no pre-0004 equivalent.
    synced = op.get_bind().scalar(sa.text("SELECT count(*) FROM documents WHERE source_system <> 'upload'"))
    if synced:
        raise RuntimeError("Downgrade refused: delete synced (file_server) documents first; 0003 cannot restrict them.")

    op.execute("DROP TRIGGER IF EXISTS governance_approvals_append_only ON governance_approvals")
    op.execute("DROP TRIGGER IF EXISTS governance_approvals_check ON governance_approvals")
    op.execute("DROP FUNCTION IF EXISTS governance_approvals_block_mutation()")
    op.execute("DROP FUNCTION IF EXISTS governance_approvals_check()")
    op.drop_table("governance_approvals")
    op.execute("DROP TRIGGER IF EXISTS permission_grants_guard ON permission_grants")
    op.execute("DROP FUNCTION IF EXISTS permission_grants_guard()")
    op.drop_table("permission_grants")
    op.drop_table("resource_acl")

    op.execute("DROP TRIGGER IF EXISTS documents_synced_start_quarantined ON documents")
    op.execute("DROP FUNCTION IF EXISTS documents_synced_start_quarantined()")
    op.drop_index("ix_documents_quarantined", table_name="documents")
    for name in ("synced_is_restricted", "quarantine_has_reason", "acl_status_valid", "source_system_valid"):
        op.drop_constraint(op.f(f"ck_documents_{name}"), "documents", type_="check")
    for column in ("acl_synced_at", "acl_quarantine_reason", "acl_status", "source_uri", "source_system"):
        op.drop_column("documents", column)
    # Group-restricted uploads fall back to owner-only, the narrowest pre-0004 visibility.
    op.execute("UPDATE documents SET visibility = 'private' WHERE visibility = 'restricted'")
    op.drop_constraint(op.f("ck_documents_visibility_valid"), "documents", type_="check")
    op.create_check_constraint(op.f("ck_documents_visibility_valid"), "documents", "visibility IN ('private', 'shared')")

    op.drop_table("access_group_members")
    op.drop_table("access_groups")
    op.drop_index("uq_users_external_id", table_name="users")
    op.drop_column("users", "external_id")
