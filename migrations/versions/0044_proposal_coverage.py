"""Add evidence-backed proposal coverage cases and review history.

Revision ID: 0044_proposal_coverage
Revises: 0043_guided_knowledge_interviews
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0044_proposal_coverage"
down_revision = "0043_guided_knowledge_interviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proposal_coverage_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("department_group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("revision_number", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('draft', 'submitted', 'changes_requested', 'approved', 'withdrawn')", name="ck_proposal_coverage_cases_status_valid"),
        sa.CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="ck_proposal_coverage_cases_title_valid"),
        sa.CheckConstraint("revision_number >= 0", name="ck_proposal_coverage_cases_revision_nonnegative"),
        sa.ForeignKeyConstraint(["department_group_id"], ["access_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_proposal_coverage_cases"),
    )
    op.create_index("ix_proposal_coverage_cases_owner", "proposal_coverage_cases", ["owner_user_id", "updated_at"])
    op.create_index("ix_proposal_coverage_cases_review", "proposal_coverage_cases", ["department_group_id", "status", "updated_at"])

    op.create_table(
        "proposal_coverage_requirements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("requirement", sa.String(length=2000), nullable=False),
        sa.Column("answer", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("insufficient_evidence", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("retrieved_chunks", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assessment", sa.String(length=16), server_default=sa.text("'not_assessed'"), nullable=False),
        sa.Column("assessment_rationale", sa.String(length=2000), server_default=sa.text("''"), nullable=False),
        sa.Column("assessed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("position BETWEEN 1 AND 30", name="ck_proposal_coverage_requirements_position_valid"),
        sa.CheckConstraint("length(btrim(requirement)) BETWEEN 5 AND 2000", name="ck_proposal_coverage_requirements_requirement_valid"),
        sa.CheckConstraint("length(answer) <= 8000", name="ck_proposal_coverage_requirements_answer_valid"),
        sa.CheckConstraint("jsonb_typeof(citations) = 'array' AND jsonb_array_length(citations) <= 10", name="ck_proposal_coverage_requirements_citations_valid"),
        sa.CheckConstraint("assessment IN ('not_assessed', 'meets', 'partial', 'gap')", name="ck_proposal_coverage_requirements_assessment_valid"),
        sa.CheckConstraint("length(assessment_rationale) <= 2000", name="ck_proposal_coverage_requirements_assessment_rationale_valid"),
        sa.CheckConstraint(
            "assessment = 'not_assessed' OR (assessed_by_user_id IS NOT NULL AND assessed_at IS NOT NULL "
            "AND length(btrim(assessment_rationale)) BETWEEN 3 AND 2000)",
            name="ck_proposal_coverage_requirements_assessment_complete",
        ),
        sa.ForeignKeyConstraint(["assessed_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["case_id"], ["proposal_coverage_cases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_proposal_coverage_requirements"),
        sa.UniqueConstraint("case_id", "position", name="uq_proposal_coverage_requirements_position"),
    )
    op.create_index("ix_proposal_coverage_requirements_case", "proposal_coverage_requirements", ["case_id", "position"])

    op.create_table(
        "proposal_coverage_reviews",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("reviewer_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("rationale", sa.String(length=2000), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("decision IN ('approved', 'changes_requested')", name="ck_proposal_coverage_reviews_decision_valid"),
        sa.CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="ck_proposal_coverage_reviews_rationale_valid"),
        sa.CheckConstraint("revision_number > 0", name="ck_proposal_coverage_reviews_revision_positive"),
        sa.CheckConstraint("jsonb_typeof(snapshot) = 'object'", name="ck_proposal_coverage_reviews_snapshot_object"),
        sa.ForeignKeyConstraint(["case_id"], ["proposal_coverage_cases.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewer_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_proposal_coverage_reviews"),
        sa.UniqueConstraint("case_id", "revision_number", name="uq_proposal_coverage_reviews_revision"),
    )
    op.create_index("ix_proposal_coverage_reviews_case", "proposal_coverage_reviews", ["case_id", "reviewed_at"])


def downgrade() -> None:
    op.drop_index("ix_proposal_coverage_reviews_case", table_name="proposal_coverage_reviews")
    op.drop_table("proposal_coverage_reviews")
    op.drop_index("ix_proposal_coverage_requirements_case", table_name="proposal_coverage_requirements")
    op.drop_table("proposal_coverage_requirements")
    op.drop_index("ix_proposal_coverage_cases_review", table_name="proposal_coverage_cases")
    op.drop_index("ix_proposal_coverage_cases_owner", table_name="proposal_coverage_cases")
    op.drop_table("proposal_coverage_cases")
