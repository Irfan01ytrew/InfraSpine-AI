"""One workflow runtime (step E): plan/execute/verify/recover phases, recorded plan, run timeout, permissioned
action steps with checkpoints and read-back verification, origins for legacy paths, and automation rules mirrored
as workflow definitions.

Revision ID: 0101_unified_workflow_runtime
Revises: 0100_platform_membership_backfill
"""
from collections.abc import Sequence
import hashlib
import json
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0101_unified_workflow_runtime"
down_revision: str | None = "0100_platform_membership_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STEP_STATUSES = ("blocked", "ready", "running", "human", "approval", "timer", "event", "child", "dispatched",
                     "completed", "skipped", "failed", "cancelled", "compensated")
NEW_STEP_STATUSES = OLD_STEP_STATUSES[:9] + ("action", "waiting") + OLD_STEP_STATUSES[9:]
PHASES = "('plan','execute','verify','recover','done')"


def _in(values):
    return "status IN (" + ",".join(f"'{v}'" for v in values) + ")"


def _automation_spec(name: str) -> dict:
    """Frozen copy of app.workflows.unified.automation_spec (fully expanded Definition JSON)."""
    from app.workflows.unified import automation_spec
    return automation_spec(name)


def upgrade() -> None:
    op.add_column("wf_definitions", sa.Column("origin", sa.String(32), server_default=sa.text("'workflow'"), nullable=False))
    op.add_column("wf_definitions", sa.Column("origin_ref", sa.String(160), nullable=True))
    op.create_check_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", "origin IN ('workflow','automation','system')")
    op.create_index(op.f("uq_wf_definitions_origin_ref"), "wf_definitions", ["scope_id", "origin", "origin_ref"], unique=True,
                    postgresql_where=sa.text("origin_ref IS NOT NULL"))

    op.add_column("wf_runs", sa.Column("phase", sa.String(16), server_default=sa.text("'plan'"), nullable=False))
    op.add_column("wf_runs", sa.Column("plan", postgresql.JSONB(), nullable=True))
    op.add_column("wf_runs", sa.Column("origin", sa.String(32), server_default=sa.text("'workflow'"), nullable=False))
    op.add_column("wf_runs", sa.Column("origin_ref", sa.String(160), nullable=True))
    op.add_column("wf_runs", sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("wf_runs", sa.Column("blocked_reason", sa.String(120), nullable=True))
    op.add_column("wf_runs", sa.Column("last_verified_node", sa.String(64), nullable=True))
    op.create_check_constraint(op.f("ck_wf_runs_phase_valid"), "wf_runs", f"phase IN {PHASES}")
    op.create_index(op.f("ix_wf_runs_origin"), "wf_runs", ["origin", "origin_ref"])
    # Existing runs: finished ones are done; running ones are executing.
    op.execute("UPDATE wf_runs SET phase = CASE WHEN status IN ('completed','cancelled','compensated') THEN 'done' "
               "WHEN status = 'failed' THEN 'recover' WHEN status = 'queued' THEN 'plan' ELSE 'execute' END")

    op.add_column("wf_steps", sa.Column("phase", sa.String(16), server_default=sa.text("'plan'"), nullable=False))
    op.add_column("wf_steps", sa.Column("acting_user_id", sa.UUID(), nullable=True))
    op.add_column("wf_steps", sa.Column("acting_auth_version", sa.Integer(), nullable=True))
    op.add_column("wf_steps", sa.Column("checkpoint", postgresql.JSONB(), nullable=True))
    op.add_column("wf_steps", sa.Column("recoveries", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column("wf_steps", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("wf_steps", sa.Column("verification", postgresql.JSONB(), nullable=True))
    op.create_foreign_key(op.f("fk_wf_steps_acting_user_id_users"), "wf_steps", "users", ["acting_user_id"], ["id"],
                          ondelete="RESTRICT")
    op.drop_constraint(op.f("ck_wf_steps_status_valid"), "wf_steps", type_="check")
    op.create_check_constraint(op.f("ck_wf_steps_status_valid"), "wf_steps", _in(NEW_STEP_STATUSES))
    op.create_check_constraint(op.f("ck_wf_steps_phase_valid"), "wf_steps", f"phase IN {PHASES}")
    op.execute("UPDATE wf_steps SET phase = CASE WHEN status IN ('completed','skipped','cancelled','compensated') "
               "THEN 'done' WHEN status = 'blocked' THEN 'plan' ELSE 'execute' END")

    # Mirror every automation rule (one per definition key, latest version) as a published workflow definition.
    bind = op.get_bind()
    rules = bind.execute(sa.text(
        "SELECT DISTINCT ON (definition_key) definition_key, name, department_group_id, created_by_user_id, enabled "
        "FROM automation_workflows WHERE created_by_user_id IS NOT NULL ORDER BY definition_key, version DESC"
    )).all()
    for key, name, group_id, owner_id, enabled in rules:
        spec = _automation_spec(name)
        spec_digest = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                                allow_nan=False).encode()).hexdigest()
        workflow_id = uuid.uuid4()
        bind.execute(sa.text(
            "INSERT INTO wf_definitions (id, scope_id, owner_id, name, description, latest_version, active_version, "
            "enabled, origin, origin_ref) VALUES (:id, :scope, :owner, :name, :description, 1, 1, :enabled, "
            "'automation', :ref) ON CONFLICT DO NOTHING"
        ), {"id": workflow_id, "scope": group_id, "owner": owner_id, "name": name[:160], "enabled": bool(enabled),
            "description": "Automation rule on the unified workflow runtime (legacy API facade).", "ref": str(key)})
        bind.execute(sa.text(
            "INSERT INTO wf_versions (id, scope_id, workflow_id, number, spec, digest, status, author_id, published_at) "
            "SELECT :id, :scope, :wf, 1, CAST(:spec AS jsonb), :digest, 'published', :owner, now() "
            "WHERE EXISTS (SELECT 1 FROM wf_definitions WHERE id = :wf)"
        ), {"id": uuid.uuid4(), "scope": group_id, "wf": workflow_id, "spec": json.dumps(spec), "digest": spec_digest,
            "owner": owner_id})


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM wf_runs r JOIN wf_definitions d ON d.id = r.workflow_id "
                           "WHERE d.origin <> 'workflow')")):
        raise RuntimeError("Unified-runtime runs exist (automation mirrors or system templates). Restore a verified "
                           "backup instead of downgrading.")
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM wf_steps WHERE status IN ('action','waiting'))")):
        raise RuntimeError("Action steps exist. Restore a verified backup instead of downgrading.")
    op.execute("ALTER TABLE wf_versions DISABLE TRIGGER wf_versions_immutable")
    op.execute("DELETE FROM wf_versions WHERE workflow_id IN (SELECT id FROM wf_definitions WHERE origin <> 'workflow')")
    op.execute("ALTER TABLE wf_versions ENABLE TRIGGER wf_versions_immutable")
    op.execute("DELETE FROM wf_definitions WHERE origin <> 'workflow'")

    op.drop_constraint(op.f("ck_wf_steps_phase_valid"), "wf_steps", type_="check")
    op.drop_constraint(op.f("ck_wf_steps_status_valid"), "wf_steps", type_="check")
    op.create_check_constraint(op.f("ck_wf_steps_status_valid"), "wf_steps", _in(OLD_STEP_STATUSES))
    op.drop_constraint(op.f("fk_wf_steps_acting_user_id_users"), "wf_steps", type_="foreignkey")
    for column in ("verification", "verified_at", "recoveries", "checkpoint", "acting_auth_version", "acting_user_id", "phase"):
        op.drop_column("wf_steps", column)

    op.drop_index(op.f("ix_wf_runs_origin"), table_name="wf_runs")
    op.drop_constraint(op.f("ck_wf_runs_phase_valid"), "wf_runs", type_="check")
    for column in ("last_verified_node", "blocked_reason", "deadline_at", "origin_ref", "origin", "plan", "phase"):
        op.drop_column("wf_runs", column)

    op.drop_index(op.f("uq_wf_definitions_origin_ref"), table_name="wf_definitions")
    op.drop_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", type_="check")
    op.drop_column("wf_definitions", "origin_ref")
    op.drop_column("wf_definitions", "origin")
