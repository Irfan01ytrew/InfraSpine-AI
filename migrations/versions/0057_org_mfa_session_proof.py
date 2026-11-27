"""Track whether a user session completed a second factor.

Revision ID: 0057_org_mfa_session_proof
Revises: 0056_legacy_browser_approval
"""
from alembic import op
import sqlalchemy as sa

revision = "0057_org_mfa_session_proof"
down_revision = "0056_legacy_browser_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_sessions", sa.Column("mfa_verified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # Removing proof while an organization policy may be enabled weakens access decisions.
    bind = op.get_bind()
    has_proof = bind.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM user_sessions WHERE mfa_verified_at IS NOT NULL)")
    )
    if has_proof:
        raise RuntimeError("MFA session proof is retained; downgrade requires a separately reviewed maintenance plan.")
    op.drop_column("user_sessions", "mfa_verified_at")
