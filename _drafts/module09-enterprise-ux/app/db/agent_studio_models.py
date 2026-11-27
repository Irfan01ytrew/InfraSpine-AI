"""Independent persistence for user-owned LangGraph agents and runs."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentStudioAgent(Base):
    __tablename__ = "agent_studio_agents"
    __table_args__ = (
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("max_steps BETWEEN 1 AND 8", name="max_steps_valid"),
        CheckConstraint("jsonb_typeof(tool_permissions) = 'array'", name="tools_are_array"),
        CheckConstraint("jsonb_typeof(approval_tools) = 'array'", name="approval_tools_are_array"),
        Index("ix_agent_studio_agents_owner_updated", "owner_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, server_default=text("''"))
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    tool_permissions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    approval_tools: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    delegate_agent_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    rag_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("6"))
    provider_id: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentStudioThread(Base):
    __tablename__ = "agent_studio_threads"
    __table_args__ = (
        Index("ix_agent_studio_threads_owner_updated", "owner_id", "updated_at"),
        Index("ix_agent_studio_threads_agent_updated", "agent_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_studio_agents.id", ondelete="CASCADE"), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False, server_default=text("'New agent run'"))
    history_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    history_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    history_key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentStudioRun(Base):
    __tablename__ = "agent_studio_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'awaiting_approval', 'completed', 'failed')", name="status_valid"),
        Index("ix_agent_studio_runs_owner_created", "owner_id", "created_at"),
        Index("ix_agent_studio_runs_thread_created", "thread_id", "created_at"),
        Index("ix_agent_studio_runs_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_studio_agents.id", ondelete="CASCADE"), nullable=False)
    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_studio_threads.id", ondelete="CASCADE"), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'running'"))
    state_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    state_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    state_key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentStudioApproval(Base):
    __tablename__ = "agent_studio_approvals"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="status_valid"),
        Index("ix_agent_studio_approvals_owner_status", "owner_id", "status"),
        Index("ix_agent_studio_approvals_run", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_studio_runs.id", ondelete="CASCADE"), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    action_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    action_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    action_key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    decision_note: Mapped[str | None] = mapped_column(String(500))
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentStudioMemory(Base):
    __tablename__ = "agent_studio_memories"
    __table_args__ = (
        UniqueConstraint("agent_id", "owner_id", "memory_key", name="uq_agent_studio_memory_key"),
        Index("ix_agent_studio_memories_owner_agent", "owner_id", "agent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_studio_agents.id", ondelete="CASCADE"), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(100), nullable=False)
    value_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    value_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    value_key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
