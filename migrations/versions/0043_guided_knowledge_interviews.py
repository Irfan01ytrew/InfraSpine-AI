"""Add resumable employee knowledge interviews and reviewed SOP drafts.

Revision ID: 0043_guided_knowledge_interviews
Revises: 0042_personal_tasks
"""

import json
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import uuid


revision = "0043_guided_knowledge_interviews"
down_revision = "0042_personal_tasks"
branch_labels = None
depends_on = None

_TEMPLATE_ID = uuid.UUID("9fa7e967-fdd9-4a2c-9a87-b3857d52bf31")
_QUESTIONS = [
    {"key": "process_name", "prompt": "What is the name of this process?", "required": True},
    {"key": "purpose", "prompt": "What outcome should this process achieve?", "required": True},
    {"key": "prerequisites", "prompt": "What must be ready before work begins?", "required": False},
    {"key": "steps", "prompt": "What steps should the next person follow? Include decision points.", "required": True},
    {"key": "verification", "prompt": "How does the employee verify the work is complete and correct?", "required": True},
    {"key": "exceptions", "prompt": "What common exceptions or failure cases occur?", "required": False},
    {"key": "escalation", "prompt": "When and to whom should an issue be escalated?", "required": False},
]


def upgrade() -> None:
    op.create_table(
        "knowledge_interview_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("description", sa.String(length=800), nullable=False),
        sa.Column("questions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("built_in", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(btrim(slug)) BETWEEN 3 AND 80", name="ck_knowledge_interview_templates_slug_length"),
        sa.CheckConstraint("length(btrim(title)) BETWEEN 1 AND 160", name="ck_knowledge_interview_templates_title_length"),
        sa.CheckConstraint("length(btrim(description)) BETWEEN 1 AND 800", name="ck_knowledge_interview_templates_description_length"),
        sa.CheckConstraint(
            "jsonb_typeof(questions) = 'array' AND jsonb_array_length(questions) BETWEEN 1 AND 20",
            name="ck_knowledge_interview_templates_questions_valid",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_interview_templates"),
        sa.UniqueConstraint("slug", name="uq_knowledge_interview_templates_slug"),
    )
    op.create_index(
        "ix_knowledge_interview_templates_active", "knowledge_interview_templates", ["is_active", "title"]
    )
    op.create_table(
        "knowledge_interview_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("department_group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'in_progress'"), nullable=False),
        sa.Column("answers", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("sop_title", sa.String(length=180), server_default=sa.text("''"), nullable=False),
        sa.Column("sop_body", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("revision_number", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("submitted_revision", sa.Integer(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('in_progress', 'submitted', 'changes_requested', 'approved', 'withdrawn')",
            name="ck_knowledge_interview_sessions_status_valid",
        ),
        sa.CheckConstraint("jsonb_typeof(template_snapshot) = 'object'", name="ck_knowledge_interview_sessions_template_snapshot_object"),
        sa.CheckConstraint("jsonb_typeof(answers) = 'object'", name="ck_knowledge_interview_sessions_answers_object"),
        sa.CheckConstraint("revision_number >= 0", name="ck_knowledge_interview_sessions_revision_nonnegative"),
        sa.CheckConstraint("length(sop_title) <= 180", name="ck_knowledge_interview_sessions_sop_title_length"),
        sa.CheckConstraint("length(sop_body) <= 40000", name="ck_knowledge_interview_sessions_sop_body_length"),
        sa.CheckConstraint("status <> 'submitted' OR submitted_revision IS NOT NULL", name="ck_knowledge_interview_sessions_submission_revision_required"),
        sa.CheckConstraint("submitted_revision IS NULL OR submitted_revision <= revision_number", name="ck_knowledge_interview_sessions_submitted_revision_valid"),
        sa.ForeignKeyConstraint(["template_id"], ["knowledge_interview_templates.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["department_group_id"], ["access_groups.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_interview_sessions"),
    )
    op.create_index(
        "ix_knowledge_interview_sessions_owner", "knowledge_interview_sessions", ["owner_user_id", "updated_at"]
    )
    op.create_index(
        "ix_knowledge_interview_sessions_review", "knowledge_interview_sessions", ["department_group_id", "status", "updated_at"]
    )
    op.create_table(
        "knowledge_interview_reviews",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("reviewer_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("rationale", sa.String(length=2000), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("decision IN ('approved', 'changes_requested')", name="ck_knowledge_interview_reviews_decision_valid"),
        sa.CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="ck_knowledge_interview_reviews_rationale_length"),
        sa.ForeignKeyConstraint(["session_id"], ["knowledge_interview_sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewer_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_interview_reviews"),
        sa.UniqueConstraint("session_id", "revision_number", name="uq_knowledge_interview_review_revision"),
    )
    op.create_index(
        "ix_knowledge_interview_reviews_session", "knowledge_interview_reviews", ["session_id", "reviewed_at"]
    )
    op.create_table(
        "knowledge_interview_sop_revisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("answers_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("submitted_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("revision_number > 0", name="ck_knowledge_interview_sop_revisions_revision_positive"),
        sa.CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="ck_knowledge_interview_sop_revisions_title_length"),
        sa.CheckConstraint("length(body) BETWEEN 1 AND 40000", name="ck_knowledge_interview_sop_revisions_body_length"),
        sa.CheckConstraint("jsonb_typeof(answers_snapshot) = 'object'", name="ck_knowledge_interview_sop_revisions_answers_snapshot_object"),
        sa.ForeignKeyConstraint(["session_id"], ["knowledge_interview_sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["submitted_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_interview_sop_revisions"),
        sa.UniqueConstraint("session_id", "revision_number", name="uq_knowledge_interview_sop_revision_number"),
    )
    op.create_index(
        "ix_knowledge_interview_sop_revisions_session", "knowledge_interview_sop_revisions", ["session_id", "revision_number"]
    )
    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    questions = literal(json.dumps(_QUESTIONS, ensure_ascii=False)) + "::jsonb"
    op.execute(
        "INSERT INTO knowledge_interview_templates "
        "(id, slug, title, description, questions, is_active, built_in) VALUES ("
        f"{literal(str(_TEMPLATE_ID))}::uuid, "
        f"{literal('process-handover')}, "
        f"{literal('Process handover')}, "
        f"{literal('Capture a repeatable process, checks, exceptions and escalation path for a teammate.')}, "
        f"{questions}, true, true)"
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_interview_sop_revisions_session", table_name="knowledge_interview_sop_revisions")
    op.drop_table("knowledge_interview_sop_revisions")
    op.drop_index("ix_knowledge_interview_reviews_session", table_name="knowledge_interview_reviews")
    op.drop_table("knowledge_interview_reviews")
    op.drop_index("ix_knowledge_interview_sessions_review", table_name="knowledge_interview_sessions")
    op.drop_index("ix_knowledge_interview_sessions_owner", table_name="knowledge_interview_sessions")
    op.drop_table("knowledge_interview_sessions")
    op.drop_index("ix_knowledge_interview_templates_active", table_name="knowledge_interview_templates")
    op.drop_table("knowledge_interview_templates")
