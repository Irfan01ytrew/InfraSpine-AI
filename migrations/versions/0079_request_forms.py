"""Custom request forms and the answers stored on each request.

Revision ID: 0079_request_forms
Revises: 0078_parallel_approval_stages
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0079_request_forms"
down_revision = "0078_parallel_approval_stages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_forms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("department_group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("access_groups.id", ondelete="SET NULL", name=op.f("fk_request_forms_department_group_id_access_groups")), nullable=True),
        sa.Column("fields", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL", name=op.f("fk_request_forms_created_by_users")), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("category IN ('it', 'hr', 'access', 'facilities', 'other', 'purchase', 'expense')", name=op.f("ck_request_forms_category_valid")),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 2 AND 120", name=op.f("ck_request_forms_name_valid")),
        sa.CheckConstraint("description IS NULL OR length(description) <= 500", name=op.f("ck_request_forms_description_length")),
        sa.CheckConstraint("jsonb_typeof(fields) = 'array'", name=op.f("ck_request_forms_fields_array")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_request_forms_version_valid")),
    )
    op.create_index("uq_request_forms_active_name", "request_forms", [sa.text("lower(name)")], unique=True, postgresql_where=sa.text("is_active"))
    op.add_column("service_requests", sa.Column(
        "form_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("request_forms.id", ondelete="SET NULL", name=op.f("fk_service_requests_form_id_request_forms")), nullable=True,
    ))
    op.add_column("service_requests", sa.Column("form_name", sa.String(120), nullable=True))
    op.add_column("service_requests", sa.Column("form_version", sa.Integer(), nullable=True))
    op.add_column("service_requests", sa.Column("form_answers", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    for column in ("form_answers", "form_version", "form_name", "form_id"):
        op.drop_column("service_requests", column)
    op.drop_index("uq_request_forms_active_name", table_name="request_forms")
    op.drop_table("request_forms")
