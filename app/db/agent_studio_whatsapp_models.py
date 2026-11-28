"""Encrypted WhatsApp Cloud API credentials and durable inbound task queue."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentStudioWhatsAppConnection(Base):
    __tablename__ = "agent_studio_whatsapp_connections"
    __table_args__ = (
        UniqueConstraint("owner_id", name="uq_agent_studio_whatsapp_owner"),
        CheckConstraint("api_version ~ '^v[0-9]+\\.0$'", name="api_version_valid"),
        Index("ix_agent_studio_whatsapp_phone", "phone_number_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_studio_agents.id", ondelete="CASCADE"), nullable=False)
    phone_number_id: Mapped[str] = mapped_column(String(64), nullable=False)
    business_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_senders: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    api_version: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'v26.0'"))
    access_token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    access_token_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    access_token_key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    app_secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    app_secret_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    app_secret_key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    verify_token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    verify_token_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    verify_token_key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    verified_name: Mapped[str | None] = mapped_column(String(200))
    display_phone_number: Mapped[str | None] = mapped_column(String(40))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentStudioWhatsAppContact(Base):
    __tablename__ = "agent_studio_whatsapp_contacts"
    __table_args__ = (
        UniqueConstraint("connection_id", "wa_id", name="uq_agent_studio_whatsapp_contact"),
        Index("ix_agent_studio_whatsapp_contacts_owner", "owner_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_studio_whatsapp_connections.id", ondelete="CASCADE"), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    wa_id: Mapped[str] = mapped_column(String(32), nullable=False)
    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_studio_threads.id", ondelete="CASCADE"), nullable=False, unique=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentStudioWhatsAppInbound(Base):
    __tablename__ = "agent_studio_whatsapp_inbound"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'processing', 'completed', 'failed')", name="status_valid"),
        CheckConstraint("attempts BETWEEN 0 AND 5", name="attempts_valid"),
        Index("ix_agent_studio_whatsapp_inbound_queue", "status", "next_attempt_at", "created_at"),
        Index("ix_agent_studio_whatsapp_inbound_owner", "owner_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_studio_whatsapp_connections.id", ondelete="CASCADE"), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    external_message_id: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    from_wa_id: Mapped[str] = mapped_column(String(32), nullable=False)
    message_type: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'text'"))
    text_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    text_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    text_key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    reply_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    reply_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    reply_key_id: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'queued'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WhatsAppPathSetting(Base):
    """Per-owner WhatsApp transport path preference (Step F slice 3).

    ``cloud``, ``baileys`` or ``auto`` (default). The setting only picks the
    *primary* transport; ``auto`` prefers the channel a message arrived on.
    A primary that is not configured or not connected is skipped, and a
    primary that fails with a retryable provider error is retried exactly
    once on the other path (see ``app/integrations/whatsapp_failover.py``).
    No row means ``auto``.
    """
    __tablename__ = "whatsapp_path_settings"
    __table_args__ = (
        CheckConstraint("path IN ('cloud', 'baileys', 'auto')", name="whatsapp_path_valid"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    path: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'auto'"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
