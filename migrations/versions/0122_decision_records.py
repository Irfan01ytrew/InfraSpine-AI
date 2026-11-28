"""Step M (slice 2): decision records + append-only decision events.

Revision ID: 0122_decision_records
Revises: 0103_unified_studio_governance

Migration-number note: sibling branches already claim 0104 (HR leave,
WhatsApp leads, AI freshness, continuity) and 0105 (AK controls, AI
conflict candidates, AB expiry alerts) off the same 0103 base, so this
slice mints the next free number, 0106. At merge time, renumber this
revision (and rebase ``down_revision`` onto the merge-time head) if the
merge order lands 0104/0105 first; the table set here does not overlap
with any of them.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0122_decision_records"
down_revision: str | None = "0121_ao_backup_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decision_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("scope_id", UUID(as_uuid=True),
                  sa.ForeignKey("platform_scopes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="other"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("decided_by", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("alternatives_considered", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("evidence", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("supersedes", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("depends_on", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("review_at", sa.Date),
        sa.Column("superseded_by", UUID(as_uuid=True),
                  sa.ForeignKey("decision_records.id", ondelete="SET NULL")),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.Column("needs_review", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "kind IN ('policy', 'finance', 'operations', 'supplier', 'people', 'technology', 'other')",
            name="ck_decision_records_kind_valid"),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'revoked', 'expired')",
            name="ck_decision_records_status_valid"),
        sa.CheckConstraint("jsonb_typeof(alternatives_considered) = 'array'",
                           name="ck_decision_records_alternatives_are_array"),
        sa.CheckConstraint("jsonb_typeof(evidence) = 'array'",
                           name="ck_decision_records_evidence_is_array"),
        sa.CheckConstraint("jsonb_typeof(supersedes) = 'array'",
                           name="ck_decision_records_supersedes_is_array"),
        sa.CheckConstraint("jsonb_typeof(depends_on) = 'array'",
                           name="ck_decision_records_depends_on_is_array"),
        sa.CheckConstraint(
            "(status = 'superseded' AND superseded_by IS NOT NULL AND superseded_at IS NOT NULL)"
            " OR (status <> 'superseded' AND superseded_by IS NULL AND superseded_at IS NULL)",
            name="ck_decision_records_superseded_details_valid"),
    )
    op.create_index("ix_decision_records_scope_status", "decision_records", ["scope_id", "status"])
    op.create_index("ix_decision_records_scope_decided", "decision_records", ["scope_id", "decided_at"])
    op.create_index("ix_decision_records_fingerprint", "decision_records", ["fingerprint"])

    op.create_table(
        "decision_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("decision_id", UUID(as_uuid=True),
                  sa.ForeignKey("decision_records.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("actor_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reason", sa.String(500), nullable=False, server_default=""),
        sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "event IN ('recorded', 'superseded', 'revoked', 'reviewed')",
            name="ck_decision_events_event_valid"),
    )
    op.create_index("ix_decision_events_decision_created", "decision_events", ["decision_id", "created_at"])
    op.create_index("ix_decision_events_event_created", "decision_events", ["event", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM decision_records)")):
        raise RuntimeError("Decision records exist. Decisions are the company's memory: export them "
                           "(with a verified backup) instead of downgrading, so no decision history "
                           "is silently lost.")
    op.drop_table("decision_events")
    op.drop_table("decision_records")
