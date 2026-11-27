"""Keep source references for requirements extracted from RFP documents.

Revision ID: 0047_proposal_requirement_origins
Revises: 0046_merge_reminder_proposal
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0047_proposal_requirement_origins"
down_revision = "0046_merge_reminder_proposal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Alembic defaults version_num to varchar(32); this revision identifier exceeds that limit.
    # Widen before Alembic writes the new revision ID. Keep the wider column on downgrade so a
    # later upgrade can safely pass through this revision again.
    op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE varchar(64)")
    op.add_column(
        "proposal_coverage_requirements",
        sa.Column(
            "origin_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_proposal_coverage_requirements_origin_evidence_valid",
        "proposal_coverage_requirements",
        "jsonb_typeof(origin_evidence) = 'array' AND jsonb_array_length(origin_evidence) <= 5",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_proposal_coverage_requirements_origin_evidence_valid",
        "proposal_coverage_requirements",
        type_="check",
    )
    op.drop_column("proposal_coverage_requirements", "origin_evidence")
