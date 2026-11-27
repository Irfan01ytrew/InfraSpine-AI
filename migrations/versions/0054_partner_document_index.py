"""Add isolated partner document jobs and location-bound vector chunks.

Revision ID: 0054_partner_document_index
Revises: 0053_personal_task_email_outbox
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0054_partner_document_index"
down_revision = "0053_personal_task_email_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "partner_knowledge_sources"
    op.add_column(table, sa.Column("source_format", sa.String(12), nullable=False, server_default="text"))
    op.add_column(table, sa.Column("storage_key", sa.String(255)))
    op.add_column(table, sa.Column("index_status", sa.String(16), nullable=False, server_default="pending"))
    op.add_column(table, sa.Column("index_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(table, sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.add_column(table, sa.Column("locked_until", sa.DateTime(timezone=True)))
    op.add_column(table, sa.Column("claim_token", postgresql.UUID(as_uuid=True)))
    op.add_column(table, sa.Column("index_error_code", sa.String(64)))
    op.add_column(table, sa.Column("embedding_model", sa.String(500)))
    op.add_column(table, sa.Column("embedding_dimension", sa.Integer()))
    op.drop_constraint("ck_partner_knowledge_sources_content_valid", table, type_="check")
    op.create_check_constraint(op.f("ck_partner_knowledge_sources_content_valid"), table,
        "(source_format = 'text' AND length(content) BETWEEN 1 AND 50000 AND storage_key IS NULL) OR "
        "(source_format IN ('pdf', 'docx', 'xlsx', 'pptx') AND content = '' AND storage_key IS NOT NULL)")
    op.create_check_constraint(op.f("ck_partner_knowledge_sources_index_status_valid"), table,
        "index_status IN ('pending', 'processing', 'ready', 'failed')")
    op.create_check_constraint(op.f("ck_partner_knowledge_sources_index_attempts_nonnegative"), table, "index_attempts >= 0")
    op.create_unique_constraint("uq_partner_knowledge_sources_id_workspace", table, ["id", "workspace_id"])
    op.create_index("ix_partner_knowledge_sources_queue", table, ["index_status", "next_attempt_at"])
    op.create_table(
        "partner_knowledge_chunks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("locator", postgresql.JSONB(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.Column("search_vector", postgresql.TSVECTOR(), sa.Computed("to_tsvector('simple', content)", persisted=True), nullable=False),
        sa.CheckConstraint("chunk_index >= 0", name="ck_partner_knowledge_chunks_chunk_index_nonnegative"),
        sa.ForeignKeyConstraint(["source_id", "workspace_id"], ["partner_knowledge_sources.id", "partner_knowledge_sources.workspace_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "chunk_index", name="uq_partner_knowledge_chunks_source_index"),
    )
    op.create_index("ix_partner_knowledge_chunks_workspace", "partner_knowledge_chunks", ["workspace_id", "source_id"])
    op.create_index("ix_partner_knowledge_chunks_search", "partner_knowledge_chunks", ["search_vector"], postgresql_using="gin")


def downgrade() -> None:
    # File sources cannot be represented by the previous schema. Refuse data loss.
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM partner_knowledge_sources WHERE source_format <> 'text')
        THEN RAISE EXCEPTION 'Export and remove partner document sources before downgrading 0054'; END IF; END $$""")
    op.drop_table("partner_knowledge_chunks")
    table = "partner_knowledge_sources"
    op.drop_index("ix_partner_knowledge_sources_queue", table_name=table)
    op.drop_constraint("uq_partner_knowledge_sources_id_workspace", table, type_="unique")
    op.drop_constraint(op.f("ck_partner_knowledge_sources_index_attempts_nonnegative"), table, type_="check")
    op.drop_constraint(op.f("ck_partner_knowledge_sources_index_status_valid"), table, type_="check")
    op.drop_constraint(op.f("ck_partner_knowledge_sources_content_valid"), table, type_="check")
    op.create_check_constraint(op.f("ck_partner_knowledge_sources_content_valid"), table, "length(content) BETWEEN 1 AND 50000")
    for name in ("embedding_dimension", "embedding_model", "index_error_code", "claim_token", "locked_until",
                 "next_attempt_at", "index_attempts", "index_status", "storage_key", "source_format"):
        op.drop_column(table, name)
