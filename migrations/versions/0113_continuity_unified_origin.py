"""Step G slice 4: continuity activations on the unified runtime (definition origin `continuity`).

Revision ID: 0113_continuity_unified_origin
Revises: 0112_whatsapp_path

Renumbered at integration time (linear chain in INTEGRATION-SNAPSHOT.md, map v4).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0113_continuity_unified_origin"
down_revision: str | None = "0112_whatsapp_path"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_ORIGINS = "origin IN ('workflow','automation','system','studio','governance')"
NEW_ORIGINS = "origin IN ('workflow','automation','system','studio','governance','continuity')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", type_="check")
    op.create_check_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", NEW_ORIGINS)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM wf_runs r JOIN wf_definitions d ON d.id = r.workflow_id "
                           "WHERE d.origin = 'continuity')")):
        raise RuntimeError("Continuity activations exist on the unified runtime. Restore a verified backup "
                           "instead of downgrading.")
    op.execute("ALTER TABLE wf_versions DISABLE TRIGGER wf_versions_immutable")
    op.execute("DELETE FROM wf_versions WHERE workflow_id IN "
               "(SELECT id FROM wf_definitions WHERE origin = 'continuity')")
    op.execute("ALTER TABLE wf_versions ENABLE TRIGGER wf_versions_immutable")
    op.execute("DELETE FROM wf_definitions WHERE origin = 'continuity'")
    op.drop_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", type_="check")
    op.create_check_constraint(op.f("ck_wf_definitions_origin_valid"), "wf_definitions", OLD_ORIGINS)
