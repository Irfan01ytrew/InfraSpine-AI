"""Separate QR connector; never shares Cloud API credentials or contacts."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BaileysConnection(Base):
    __tablename__ = "agent_studio_baileys_connections"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_studio_agents.id", ondelete="CASCADE"))
    thread_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_studio_threads.id", ondelete="SET NULL"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(40), default="starting")
    auth_sealed: Mapped[str | None] = mapped_column(Text)
    qr_sealed: Mapped[str | None] = mapped_column(Text)
    qr_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_suffix: Mapped[str | None] = mapped_column(String(8))
    last_error: Mapped[str | None] = mapped_column(String(200))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BaileysMessage(Base):
    __tablename__ = "agent_studio_baileys_messages"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_studio_baileys_connections.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(160), unique=True)
    payload_sealed: Mapped[str] = mapped_column(Text)
    reply_sealed: Mapped[str | None] = mapped_column(Text)
    receipt_status: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    last_error: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
