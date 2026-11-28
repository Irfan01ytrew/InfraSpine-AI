"""Step H: founder voice-note intake records.

A record is the intake receipt for one parsed voice-note command. It is
**untrusted input, never authority**: it holds the intent, the sha256 hash of
the normalised transcript (``action_hash``), and the routing ids — never the
transcript itself (that travels only inside the workflow run's sealed input,
like every other run input) and never a permission, a payee, or an approval.

One record maps to at most one workflow run (``origin='founder_voice'``,
``origin_ref=action_hash``); the unique hash makes a re-sent voice note
idempotent instead of a second run.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

FOUNDER_VOICE_INTENTS = (
    "finance_spend_chart",
    "invoice_verification",
    "continuity_replacement",
    "approval_request",
    "unknown",
)


class FounderVoiceCommandRecord(Base):
    __tablename__ = "founder_voice_commands"
    __table_args__ = (
        CheckConstraint("status IN ('received','mirrored','cancelled','failed')", name="status_valid"),
        CheckConstraint(
            "intent IN ('finance_spend_chart','invoice_verification','continuity_replacement',"
            "'approval_request','unknown')",
            name="intent_valid",
        ),
        Index("uq_founder_voice_commands_action_hash", "action_hash", unique=True),
        Index("ix_founder_voice_commands_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    scope_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("access_groups.id", ondelete="RESTRICT"), nullable=False)
    intent: Mapped[str] = mapped_column(String(32), nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # The transcript itself, sealed with the workflow cipher (AAD binds it to
    # this record + scope). It is untrusted input: it is never read as
    # authority, only re-parsed and hashed again at mirror/execution time.
    transcript_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    transcript_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    transcript_key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # First required platform permission for the intent (None for continuity,
    # which is authorized by its own tenant scope). Stored so the engine-tick
    # sweep can build the spec without re-reading the transcript.
    required_permission: Mapped[str | None] = mapped_column(String(64))
    # Routing ids only; validated against their own modules at execution time.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("fin_invoice_batches.id", ondelete="SET NULL"))
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    node_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'received'"))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
