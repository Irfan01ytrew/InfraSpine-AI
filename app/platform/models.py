"""Tables of the shared platform core: scopes, memberships, roles, channel identities, approvals, executions,
the transactional outbox and organisation settings. Modules adopt them in step D; nothing here replaces a
module table yet."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

DEFAULT_TIME_ZONE = "Asia/Karachi"


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OrgSettings(Base):
    """One row (id = 1) per installation. Times are stored in UTC; business days use `time_zone`."""

    __tablename__ = "org_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("finance_payment_connector IS NULL OR finance_payment_connector IN ('mock','http','http+mock')",
                        name="finance_connector_valid"),
        CheckConstraint("finance_autopilot_max_payment_amount IS NULL OR finance_autopilot_max_payment_amount >= 0",
                        name="autopilot_amounts_valid"),
        CheckConstraint("finance_autopilot_daily_cap IS NULL OR finance_autopilot_daily_cap >= 0",
                        name="autopilot_cap_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    name: Mapped[str | None] = mapped_column(String(200))
    time_zone: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text(f"'{DEFAULT_TIME_ZONE}'"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'PKR'"))
    # Admin choice of payment connector (step E). NULL = use FINANCE_PAYMENT_CONNECTOR / FINANCE_PAYMENT_FALLBACK.
    # Bank URL and token stay in the environment (secrets are never stored here).
    finance_payment_connector: Mapped[str | None] = mapped_column(String(16))
    # Finance autopilot policy (step R slice 2). All default to off / unset: upgrading never starts a sweep.
    # Departments are not stored here; slice 4 adds a per-vendor allow-list.
    finance_autopilot_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    finance_autopilot_dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    finance_autopilot_max_payment_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    finance_autopilot_daily_cap: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    # Step AE: Knowledge documents WhatsApp auto-replies may quote (list of
    # document UUID strings). Default empty: auto-reply answers nothing from
    # documents until the owner flags documents customer-visible.
    whatsapp_customer_visible_document_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    updated_at: Mapped[datetime] = _now()


class PlatformScope(Base):
    __tablename__ = "platform_scopes"
    __table_args__ = (
        CheckConstraint("kind IN ('org', 'department', 'workspace', 'module_space')", name="kind_valid"),
        CheckConstraint("(kind = 'org') = (parent_id IS NULL)", name="only_org_is_root"),
        UniqueConstraint("legacy_table", "legacy_id", name="uq_platform_scopes_legacy"),
        Index("uq_platform_scopes_single_org", "kind", unique=True, postgresql_where=text("kind = 'org'")),
        Index("ix_platform_scopes_parent_id", "parent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("platform_scopes.id", ondelete="RESTRICT"))
    module: Mapped[str | None] = mapped_column(String(40))
    legacy_table: Mapped[str | None] = mapped_column(String(80))
    legacy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = _now()


class PlatformRole(Base):
    __tablename__ = "platform_roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = _now()


class PlatformRolePermission(Base):
    __tablename__ = "platform_role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_roles.id", ondelete="CASCADE"), primary_key=True)
    permission: Mapped[str] = mapped_column(String(80), primary_key=True)  # "*" = every permission


class PlatformMembership(Base):
    __tablename__ = "platform_memberships"
    __table_args__ = (
        Index("uq_platform_memberships_active", "scope_id", "user_id", "role_id", unique=True,
              postgresql_where=text("revoked_at IS NULL")),
        Index("ix_platform_memberships_user_id", "user_id"),
        CheckConstraint("valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from", name="valid_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_scopes.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_roles.id", ondelete="RESTRICT"), nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # time-limited (handover) grants
    granted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    granted_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Where the grant comes from: 'platform' (granted here) or 'legacy:<table>' (mirrored from a module's member
    # table). revoke_reason 'platform:*' blocks re-mirroring; 'legacy:*' (the module removed it) does not.
    source: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'platform'"))
    revoke_reason: Mapped[str | None] = mapped_column(String(120))


class PlatformChannelIdentity(Base):
    """A verified external identity (e.g. a WhatsApp number) that acts as exactly one user."""

    __tablename__ = "platform_channel_identities"
    __table_args__ = (
        CheckConstraint("channel IN ('whatsapp_cloud', 'whatsapp_baileys', 'api')", name="channel_valid"),
        Index("uq_platform_channel_identities_active", "channel", "external_id", unique=True,
              postgresql_where=text("revoked_at IS NULL")),
        Index("ix_platform_channel_identities_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_method: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformApprovalRequest(Base):
    __tablename__ = "platform_approval_requests"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected', 'expired', 'consumed', 'cancelled')",
                        name="status_valid"),
        CheckConstraint("char_length(action_hash) = 64", name="hash_length"),
        Index("ix_platform_approval_requests_scope_status", "scope_id", "status"),
        Index("ix_platform_approval_requests_subject", "subject"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_scopes.id", ondelete="CASCADE"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[dict] = mapped_column(JSONB, nullable=False)  # canonical ActionSpec; never holds secrets
    subject: Mapped[str | None] = mapped_column(String(200))  # e.g. "invoice:INV-204", for separation of duties
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    required_permission: Mapped[str] = mapped_column(String(80), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    requested_via: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(String(500))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class PlatformActionExecution(Base):
    """Exactly-once ledger: one row per idempotency key; dispatch happens only for the row's creator."""

    __tablename__ = "platform_action_executions"
    __table_args__ = (
        CheckConstraint("status IN ('claimed', 'dispatched', 'verified', 'failed')", name="status_valid"),
        Index("ix_platform_action_executions_subject", "subject"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    duty: Mapped[str] = mapped_column(String(80), nullable=False)  # the permission exercised
    subject: Mapped[str | None] = mapped_column(String(200))
    scope_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_scopes.id", ondelete="CASCADE"), nullable=False)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("platform_approval_requests.id", ondelete="SET NULL"))
    executor_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    executor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'claimed'"))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    external_ref: Mapped[str | None] = mapped_column(String(500))
    verified_result: Mapped[dict | None] = mapped_column(JSONB)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = _now()
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class IdempotencyRecord(Base):
    """Generic Idempotency-Key ledger for lightweight HTTP creates (Step U slice 3).

    One row per (user, key). The raw key is never stored — only its SHA-256 digest — so a
    leaked row cannot be replayed as a credential. The request hash binds the key to the
    exact request: the same key with a different request is a 409, never a silent replay.
    A completed row stores the original response (status + body) so a replay returns it
    without executing again. This is deliberately separate from PlatformActionExecution /
    execute_once: that ledger is for privileged, approval-gated dispatches with read-back
    verification; these creates are synchronous, user-scoped and need no duty or approval.
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'completed', 'failed')", name="idempotency_status_valid"),
        UniqueConstraint("user_id", "key_digest", name="uq_idempotency_records_user_key"),
        Index("ix_idempotency_records_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformOutbox(Base):
    __tablename__ = "platform_outbox"
    __table_args__ = (
        Index("ix_platform_outbox_pending", "id", postgresql_where=text("dispatched_at IS NULL")),
        Index("ix_platform_outbox_due", "next_attempt_at", "id",
              postgresql_where=text("dispatched_at IS NULL AND dead_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    trace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = _now()
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Retry with backoff: a failed delivery waits until next_attempt_at; after the last attempt it is dead-lettered
    # (kept for an operator to inspect and requeue, never silently dropped).
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
