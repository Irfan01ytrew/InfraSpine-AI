"""Who may use a request form: listed departments and positions (empty means everyone).

Revision ID: 0081_request_form_audience
Revises: 0080_request_attachments
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0081_request_form_audience"
down_revision = "0080_request_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("request_forms", sa.Column("audience", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.create_check_constraint(op.f("ck_request_forms_audience_object"), "request_forms", "jsonb_typeof(audience) = 'object'")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_request_forms_audience_object"), "request_forms", type_="check")
    op.drop_column("request_forms", "audience")
