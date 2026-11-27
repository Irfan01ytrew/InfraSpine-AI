"""Add employee-authored expertise tags with a searchable PostgreSQL index.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "expertise_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_users_expertise_tags_gin",
        "users",
        ["expertise_tags"],
        postgresql_using="gin",
        postgresql_ops={"expertise_tags": "jsonb_path_ops"},
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM users WHERE expertise_tags <> '[]'::jsonb) THEN
                RAISE EXCEPTION 'clear employee expertise tags before downgrading migration 0032';
            END IF;
        END $$;"""
    )
    op.drop_index("ix_users_expertise_tags_gin", table_name="users")
    op.drop_column("users", "expertise_tags")
