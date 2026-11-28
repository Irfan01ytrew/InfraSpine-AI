"""Step R slice 4: per-vendor autopilot allow-list on the vendor master.

Only vendors an administrator has marked ``autopilot_allowed`` may be
auto-picked-up by the sweep. New vendors are never allow-listed by default:
a batch whose invoices name an unlisted vendor (or no vendor yet) is left
for a human to start. The flag is only ever set through the existing vendor
save endpoint (``finance.manage``), never by the sweep and never from
document/invoice text.

Revision ID: 0105_finance_autopilot_vendor_allowlist
Revises: 0104_finance_autopilot

Merge note: sibling branches also mint 0105 (AK, AI contradiction scan, AB
expiry alerts, AA provider ref, G handover grants). Whoever merges second
must renumber; no data is affected.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0105_finance_autopilot_vendor_allowlist"
down_revision: str | None = "0104_finance_autopilot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fin_vendors", sa.Column(
        "autopilot_allowed", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade() -> None:
    op.drop_column("fin_vendors", "autopilot_allowed")
