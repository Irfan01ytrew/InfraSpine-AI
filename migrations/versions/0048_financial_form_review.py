"""Persist source-version finance extractions and append-only human reviews.

Revision ID: 0048_financial_form_review
Revises: 0047_proposal_requirement_origins
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0048_financial_form_review"
down_revision = "0047_proposal_requirement_origins"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "financial_form_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("profile", sa.String(length=24), nullable=False),
        sa.Column("date_order", sa.String(length=3), nullable=False),
        sa.Column("extraction_status", sa.String(length=16), nullable=False),
        sa.Column("extraction_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("document_version >= 1", name="ck_financial_form_records_document_version_positive"),
        sa.CheckConstraint("length(source_sha256) = 64", name="ck_financial_form_records_source_sha256_length"),
        sa.CheckConstraint("profile IN ('invoice', 'purchase_order', 'form')", name="ck_financial_form_records_profile_valid"),
        sa.CheckConstraint("date_order IN ('dmy', 'mdy', 'ymd')", name="ck_financial_form_records_date_order_valid"),
        sa.CheckConstraint("extraction_status IN ('ready', 'needs_review')", name="ck_financial_form_records_extraction_status_valid"),
        sa.CheckConstraint("jsonb_typeof(extraction_data) = 'object'", name="ck_financial_form_records_extraction_data_object"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "document_version", "profile", "date_order",
            name="uq_financial_form_records_source_profile_date_order",
        ),
    )
    op.create_index("ix_financial_form_records_source", "financial_form_records", ["document_id", "document_version"])
    op.create_index("ix_financial_form_records_queue", "financial_form_records", ["extraction_status", "created_at"])
    op.create_table(
        "financial_form_review_revisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reviewed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rationale", sa.String(length=2000), nullable=False),
        sa.Column("reviewed_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reviewed_line_items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reviewed_exception_indexes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_financial_form_review_revisions_revision_positive"),
        sa.CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="ck_financial_form_review_revisions_rationale_length"),
        sa.CheckConstraint("jsonb_typeof(reviewed_fields) = 'object'", name="ck_financial_form_review_revisions_reviewed_fields_object"),
        sa.CheckConstraint("jsonb_typeof(reviewed_line_items) = 'array'", name="ck_financial_form_review_revisions_reviewed_line_items_array"),
        sa.CheckConstraint("jsonb_typeof(reviewed_exception_indexes) = 'array'", name="ck_financial_form_review_revisions_exception_indexes_array"),
        sa.ForeignKeyConstraint(["record_id"], ["financial_form_records.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("record_id", "revision", name="uq_financial_form_review_revisions_record_revision"),
    )
    op.create_index(
        "ix_financial_form_review_revisions_record",
        "financial_form_review_revisions",
        ["record_id", "revision"],
    )
    op.execute("""
        CREATE FUNCTION reject_financial_form_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Financial form source snapshots and review revisions are immutable'
                USING ERRCODE = '55000';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER financial_form_records_immutable
        BEFORE UPDATE ON financial_form_records
        FOR EACH ROW EXECUTE FUNCTION reject_financial_form_update()
    """)
    op.execute("""
        CREATE TRIGGER financial_form_review_revisions_immutable
        BEFORE UPDATE ON financial_form_review_revisions
        FOR EACH ROW EXECUTE FUNCTION reject_financial_form_update()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER financial_form_review_revisions_immutable ON financial_form_review_revisions")
    op.execute("DROP TRIGGER financial_form_records_immutable ON financial_form_records")
    op.execute("DROP FUNCTION reject_financial_form_update()")
    op.drop_index("ix_financial_form_review_revisions_record", table_name="financial_form_review_revisions")
    op.drop_table("financial_form_review_revisions")
    op.drop_index("ix_financial_form_records_queue", table_name="financial_form_records")
    op.drop_index("ix_financial_form_records_source", table_name="financial_form_records")
    op.drop_table("financial_form_records")
