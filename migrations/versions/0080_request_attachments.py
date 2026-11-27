"""Files attached to requests through form file fields.

Revision ID: 0080_request_attachments
Revises: 0079_request_forms
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0080_request_attachments"
down_revision = "0079_request_forms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("uploader_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE", name=op.f("fk_request_attachments_uploader_user_id_users")), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("service_requests.id", ondelete="CASCADE", name=op.f("fk_request_attachments_request_id_service_requests")), nullable=True),
        sa.Column("field_key", sa.String(40), nullable=True),
        sa.Column("filename", sa.String(220), nullable=False),
        sa.Column("content_type", sa.String(120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_request_attachments_size_positive")),
        sa.CheckConstraint("(request_id IS NULL) = (linked_at IS NULL)", name=op.f("ck_request_attachments_linked_consistent")),
    )
    op.create_index("ix_request_attachments_request", "request_attachments", ["request_id"])
    op.create_index("ix_request_attachments_pending", "request_attachments", ["uploader_user_id", "created_at"], postgresql_where=sa.text("request_id IS NULL"))


def downgrade() -> None:
    op.drop_index("ix_request_attachments_pending", table_name="request_attachments")
    op.drop_index("ix_request_attachments_request", table_name="request_attachments")
    op.drop_table("request_attachments")
