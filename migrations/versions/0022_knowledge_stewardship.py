"""Separate immutable knowledge authorship from current organizational ownership.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("knowledge_entries", sa.Column("owner_user_id", sa.UUID(), nullable=True))
    op.execute("UPDATE knowledge_entries SET owner_user_id = author_user_id")
    op.alter_column("knowledge_entries", "owner_user_id", nullable=False)
    op.create_foreign_key(
        "fk_knowledge_entries_owner_user",
        "knowledge_entries",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_knowledge_entries_owner_created", "knowledge_entries", ["owner_user_id", "created_at"]
    )

    # Revisions keep the person who actually authored them. The prior composite FK forced every
    # revision to reuse the entry's original author, making a valid stewardship transfer impossible.
    op.create_foreign_key(
        "fk_knowledge_revisions_entry",
        "knowledge_revisions",
        "knowledge_entries",
        ["entry_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_knowledge_revisions_author_user",
        "knowledge_revisions",
        "users",
        ["author_user_id"],
        ["id"],
    )
    op.drop_constraint("fk_knowledge_revisions_entry_author", "knowledge_revisions", type_="foreignkey")

    # Ownership can change only to an active account. Entry identity/authorship and the current
    # revision's membership remain protected by the existing database trigger.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION knowledge_entries_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'knowledge entries are never deleted' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'UPDATE' AND (NEW.id, NEW.author_user_id, NEW.created_at)
               IS DISTINCT FROM (OLD.id, OLD.author_user_id, OLD.created_at) THEN
                RAISE EXCEPTION 'knowledge entry identity is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.owner_user_id AND is_active) THEN
                RAISE EXCEPTION 'knowledge owner must be active' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.current_revision_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM knowledge_revisions
                WHERE id = NEW.current_revision_id AND entry_id = NEW.id
            ) THEN
                RAISE EXCEPTION 'current revision does not belong to this entry' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM knowledge_entries WHERE owner_user_id <> author_user_id
            ) OR EXISTS (
                SELECT 1 FROM knowledge_revisions r
                JOIN knowledge_entries e ON e.id = r.entry_id
                WHERE r.author_user_id <> e.author_user_id
            ) THEN
                RAISE EXCEPTION 'cannot downgrade after knowledge ownership or revision authorship has transferred';
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION knowledge_entries_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'knowledge entries are never deleted' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'UPDATE' AND (NEW.id, NEW.author_user_id, NEW.created_at)
               IS DISTINCT FROM (OLD.id, OLD.author_user_id, OLD.created_at) THEN
                RAISE EXCEPTION 'knowledge entry identity is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.current_revision_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM knowledge_revisions
                WHERE id = NEW.current_revision_id AND entry_id = NEW.id
            ) THEN
                RAISE EXCEPTION 'current revision does not belong to this entry' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.drop_constraint("fk_knowledge_revisions_author_user", "knowledge_revisions", type_="foreignkey")
    op.drop_constraint("fk_knowledge_revisions_entry", "knowledge_revisions", type_="foreignkey")
    op.create_foreign_key(
        "fk_knowledge_revisions_entry_author",
        "knowledge_revisions",
        "knowledge_entries",
        ["entry_id", "author_user_id"],
        ["id", "author_user_id"],
    )
    op.drop_index("ix_knowledge_entries_owner_created", table_name="knowledge_entries")
    op.drop_constraint("fk_knowledge_entries_owner_user", "knowledge_entries", type_="foreignkey")
    op.drop_column("knowledge_entries", "owner_user_id")
