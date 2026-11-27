"""Merge the personal-task reminder and proposal coverage migration branches.

Revision ID: 0046_merge_reminder_proposal
Revises: 0044_personal_task_reminders, 0045_document_obligations
"""

revision = "0046_merge_reminder_proposal"
down_revision = ("0044_personal_task_reminders", "0045_document_obligations")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge both schema branches without changing stored data."""


def downgrade() -> None:
    """Split the graph without changing stored data."""
