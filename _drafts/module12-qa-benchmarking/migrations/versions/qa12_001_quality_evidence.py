"""Add independent QA evidence storage. No production migration is executed by this change."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "qa12_001_quality_evidence"
down_revision = "0084_immutable_conversation_messages"
branch_labels = None
depends_on = None

def scope():
    return [sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
            sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)]

def upgrade():
    op.create_table("qa_runs", *scope(),
        sa.Column("suite_id", sa.String(80), nullable=False), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False), sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False), sa.Column("verifies", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)), sa.Column("claim_token", UUID(as_uuid=True)),
        sa.Column("results", JSONB, nullable=False), sa.Column("metrics", JSONB, nullable=False), sa.Column("provenance", JSONB, nullable=False),
        sa.Column("evidence_sha256", sa.String(64)),
        sa.Column("rerun_of", UUID(as_uuid=True), sa.ForeignKey("qa_runs.id", ondelete="RESTRICT")),
        sa.CheckConstraint("status IN ('queued','running','passed','failed','blocked','cancelled','unverified')", name="qa_run_status"))
    op.create_index("ix_qa_runs_scope", "qa_runs", ["tenant_id", "owner_id", "created_at"])
    op.create_table("qa_defects", *scope(), sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("test_id", sa.String(500), nullable=False), sa.Column("module", sa.Integer, nullable=False),
        sa.Column("severity", sa.String(20), nullable=False), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("occurrences", sa.Integer, nullable=False), sa.Column("history", JSONB, nullable=False),
        sa.Column("last_run_id", UUID(as_uuid=True), sa.ForeignKey("qa_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.UniqueConstraint("tenant_id", "owner_id", "fingerprint", name="uq_qa_defect_scope"))
    op.create_table("qa_decisions", *scope(), sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False), sa.Column("report", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "owner_id", "snapshot_sha256", name="uq_qa_decision_snapshot"))
    op.execute("""CREATE FUNCTION qa_preserve_terminal_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.status NOT IN ('queued','running') THEN
        RAISE EXCEPTION 'Terminal QA evidence is immutable; create a rerun';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER qa_terminal_immutable BEFORE UPDATE ON qa_runs
    FOR EACH ROW EXECUTE FUNCTION qa_preserve_terminal_evidence();""
    )

def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM qa_runs) OR EXISTS (SELECT 1 FROM qa_decisions) THEN
        RAISE EXCEPTION 'QA evidence exists; archive and authorize retention migration first';
      END IF;
    END $$""")
    op.drop_table("qa_decisions")
    op.drop_table("qa_defects")
    op.drop_table("qa_runs")
    op.execute("DROP FUNCTION qa_preserve_terminal_evidence()")
