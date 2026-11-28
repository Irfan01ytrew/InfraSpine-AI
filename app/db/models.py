"""ORM models. Critical invariants are enforced by the database, not only by code."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.core.permission_catalog import (
    KNOWLEDGE_CATEGORIES,
    PERMISSION_PUBLISH,
    PERMISSION_REVIEW,
    PERMISSION_STEWARD,
    PERMISSIONS,
)

ROLE_ADMIN = "admin"
# ``user`` remains accepted for accounts and test fixtures created before the enterprise role
# names were introduced. New accounts use ``employee``.
ROLE_USER = "user"
ROLE_MANAGER = "manager"
ROLE_EMPLOYEE = "employee"

CREDENTIAL_PENDING = "pending"  # verified, staged, not yet serving traffic
CREDENTIAL_ACTIVE = "active"  # the credential used when its provider is active
CREDENTIAL_RETIRED = "retired"  # replaced; ciphertext is wiped

VISIBILITY_PRIVATE = "private"  # owner only
VISIBILITY_SHARED = "shared"  # every signed-in user; only admins may publish
VISIBILITY_RESTRICTED = "restricted"  # the owner (uploads only) + the principals in `resource_acl`

SOURCE_UPLOAD = "upload"  # uploaded through InfraSpine; the owner may read it
SOURCE_FILE_SERVER = "file_server"  # synced; access comes only from the source ACL, never from ownership

ACL_ACTIVE = "active"
ACL_QUARANTINED = "quarantined"  # nobody can read it (administrators included) until a valid ACL is synced

GROUP_KINDS = ("department", "project", "client", "custom")
GROUP_LOCAL = "local"  # managed by administrators in InfraSpine
GROUP_DIRECTORY = "directory"  # owned by the company directory; membership is read-only here

STAGE_REVIEW = "review"
STAGE_PUBLISH = "publish"

SUBJECT_KNOWLEDGE_CLAIM = "knowledge_claim"
SUBJECT_KNOWLEDGE_REVISION = "knowledge_revision"

CLAIM_PROPOSAL = "proposal"
CLAIM_CORRECTION = "correction"
CLAIM_KINDS = (CLAIM_PROPOSAL, CLAIM_CORRECTION)

CLAIM_SUBMITTED = "submitted"
CLAIM_WITHDRAWN = "withdrawn"
CLAIM_STATUSES = (CLAIM_SUBMITTED, CLAIM_WITHDRAWN)

CLAIM_SOURCE_DOCUMENT = "document"
CLAIM_SOURCE_URL = "url"
CLAIM_SOURCE_NOTE = "note"
CLAIM_SOURCE_KINDS = (CLAIM_SOURCE_DOCUMENT, CLAIM_SOURCE_URL, CLAIM_SOURCE_NOTE)
CLAIM_STATEMENT_MAX = 4000
CLAIM_SOURCES_MAX = 20

KNOWLEDGE_DRAFT = "draft"
KNOWLEDGE_PUBLISHED = "published"  # searchable after scoped two-person approval
KNOWLEDGE_SUPERSEDED = "superseded"
KNOWLEDGE_WITHDRAWN = "withdrawn"
KNOWLEDGE_SUBMITTED = "submitted"
KNOWLEDGE_STATUSES = (
    KNOWLEDGE_DRAFT, KNOWLEDGE_SUBMITTED, KNOWLEDGE_PUBLISHED, KNOWLEDGE_SUPERSEDED, KNOWLEDGE_WITHDRAWN
)

KNOWLEDGE_PRIVATE = "private"
KNOWLEDGE_RESTRICTED = "restricted"
KNOWLEDGE_SHARED = "shared"

EVIDENCE_REVISION_CHANGED = "document_revision_changed"
EVIDENCE_SOURCE_DELETED = "source_deleted"
EVIDENCE_PASSAGE_REMOVED = "passage_removed"
EVIDENCE_ORPHAN_REASONS = (EVIDENCE_REVISION_CHANGED, EVIDENCE_SOURCE_DELETED, EVIDENCE_PASSAGE_REMOVED)

OUTBOX_INDEX = "knowledge.index"
OUTBOX_UNINDEX = "knowledge.unindex"
OUTBOX_RECONCILE = "knowledge.reconcile"
OUTBOX_PENDING = "pending"
OUTBOX_PROCESSING = "processing"
OUTBOX_COMPLETED = "completed"
OUTBOX_FAILED = "failed"

DOC_PENDING = "pending"
DOC_PROCESSING = "processing"
DOC_READY = "ready"
DOC_FAILED = "failed"

SERVICE_REQUEST_CATEGORIES = ("it", "hr", "access", "facilities", "other")
SERVICE_REQUEST_PRIORITIES = ("low", "normal", "high", "urgent")
SERVICE_REQUEST_STATUSES = (
    "submitted", "acknowledged", "in_progress", "waiting_requester", "resolved", "rejected", "cancelled"
)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'manager', 'employee', 'user')", name="role_valid"),
        Index("uq_users_external_id", "external_id", unique=True, postgresql_where=text("external_id IS NOT NULL")),
        Index("ix_users_reports_to_id", "reports_to_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)  # stored lowercase
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Directory object ID (e.g. an Active Directory SID), used only to map synced source ACLs.
    external_id: Mapped[str | None] = mapped_column(String(256))
    # Voluntary, employee-authored expertise tags; search visibility is department scoped.
    expertise_tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # Organization structure, separate from access: a position (job title) and a manager. Neither grants permissions.
    position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("positions.id", ondelete="RESTRICT"))
    reports_to_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Bumped by platform.revoke_user_access; a Principal resolved under an older version is no longer valid.
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = _now()


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mfa_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserMfaFactor(Base):
    """Per-user TOTP factor; the secret is authenticated-encrypted with user-bound AAD."""

    __tablename__ = "user_mfa_factors"
    __table_args__ = (CheckConstraint("last_totp_counter >= -1", name="last_counter_valid"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = _now()
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_totp_counter: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("-1"))


class UserMfaRecoveryCode(Base):
    """One-use recovery code digests; raw recovery values are shown only when issued."""

    __tablename__ = "user_mfa_recovery_codes"
    __table_args__ = (
        UniqueConstraint("user_id", "code_hash", name="uq_user_mfa_recovery_digest"),
        Index("ix_user_mfa_recovery_unused", "user_id", "used_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _now()
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserMfaChallenge(Base):
    """Short-lived, single-use password-to-MFA login challenge."""

    __tablename__ = "user_mfa_challenges"
    __table_args__ = (
        CheckConstraint("attempts BETWEEN 0 AND 5", name="attempts_bounded"),
        Index("ix_user_mfa_challenges_user_expiry", "user_id", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PartnerWorkspace(Base):
    """Tenant boundary for external customer/partner identities and their future corpus."""

    __tablename__ = "partner_workspaces"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'suspended')", name="status_valid"),
        Index("uq_partner_workspaces_slug", "slug", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()


class PartnerIdentity(Base):
    """External principal kept outside the employee users and their authorization graph."""

    __tablename__ = "partner_identities"
    __table_args__ = (
        UniqueConstraint("workspace_id", "email", name="uq_partner_identities_workspace_email"),
        CheckConstraint("failed_login_attempts >= 0", name="failed_login_attempts_nonnegative"),
        Index("ix_partner_identities_workspace_active", "workspace_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("partner_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class PartnerInvitation(Base):
    """Single-use external invitation. Only the SHA-256 digest of its bearer token is stored."""

    __tablename__ = "partner_invitations"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint("length(token_hash) = 64", name="token_hash_length"),
        CheckConstraint("(accepted_at IS NULL) OR (revoked_at IS NULL)", name="not_accepted_and_revoked"),
        Index("ix_partner_invitations_workspace_email", "workspace_id", "email"),
        Index("ix_partner_invitations_expires_at", "expires_at"),
        Index(
            "uq_partner_invitations_active_email",
            "workspace_id", "email", unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("partner_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PartnerSession(Base):
    """External login session with its own cookie and token namespace."""

    __tablename__ = "partner_sessions"
    __table_args__ = (Index("ix_partner_sessions_identity_expiry", "identity_id", "expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    identity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("partner_identities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PartnerKnowledgeSource(Base):
    """Partner-only source; its content and derived index stay outside employee knowledge."""

    __tablename__ = "partner_knowledge_sources"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="status_valid"),
        CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="title_valid"),
        CheckConstraint("(source_format = 'text' AND length(content) BETWEEN 1 AND 50000 AND storage_key IS NULL) OR "
                        "(source_format IN ('pdf', 'docx', 'xlsx', 'pptx') AND content = '' AND storage_key IS NOT NULL)", name="content_valid"),
        CheckConstraint("length(content_sha256) = 64", name="content_sha256_length"),
        CheckConstraint("index_status IN ('pending', 'processing', 'ready', 'failed')", name="index_status_valid"),
        CheckConstraint("index_attempts >= 0", name="index_attempts_nonnegative"),
        UniqueConstraint("id", "workspace_id", name="uq_partner_knowledge_sources_id_workspace"),
        Index("ix_partner_knowledge_sources_workspace_status", "workspace_id", "status"),
        Index("ix_partner_knowledge_sources_queue", "index_status", "next_attempt_at"),
        Index("ix_partner_knowledge_sources_search", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("partner_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_format: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'text'"))
    storage_key: Mapped[str | None] = mapped_column(String(255))
    index_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    index_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    index_error_code: Mapped[str | None] = mapped_column(String(64))
    embedding_model: Mapped[str | None] = mapped_column(String(500))
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True), nullable=False
    )


class PartnerKnowledgeChunk(Base):
    """A location-bound embedding whose source and workspace are linked by a composite foreign key."""

    __tablename__ = "partner_knowledge_chunks"
    __table_args__ = (
        ForeignKeyConstraint(["source_id", "workspace_id"],
                             ["partner_knowledge_sources.id", "partner_knowledge_sources.workspace_id"],
                             ondelete="CASCADE"),
        UniqueConstraint("source_id", "chunk_index", name="uq_partner_knowledge_chunks_source_index"),
        CheckConstraint("chunk_index >= 0", name="chunk_index_nonnegative"),
        Index("ix_partner_knowledge_chunks_workspace", "workspace_id", "source_id"),
        Index("ix_partner_knowledge_chunks_search", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[dict] = mapped_column(JSONB, nullable=False)
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True), nullable=False
    )


class UserInvitation(Base):
    """Single-use onboarding invitation. Only the SHA-256 digest of the bearer token is stored."""

    __tablename__ = "user_invitations"
    __table_args__ = (
        CheckConstraint("role IN ('manager', 'employee')", name="role_valid"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint("(accepted_at IS NULL) OR (revoked_at IS NULL)", name="not_accepted_and_revoked"),
        Index("ix_user_invitations_email", "email"),
        Index("ix_user_invitations_expires_at", "expires_at"),
        Index(
            "uq_user_invitations_active_email",
            "email",
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    department_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # Optional custom role assigned in the invitation's departments when the invitee signs up.
    role_template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("role_templates.id", ondelete="SET NULL"))
    # Optional organization placement applied at sign-up: a position (job title) and a manager. No access.
    position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("positions.id", ondelete="SET NULL"))
    reports_to_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderConfig(Base):
    __tablename__ = "provider_configs"
    __table_args__ = (
        # At most one provider can be active platform-wide, even under concurrent requests.
        Index(
            "uq_provider_configs_single_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        CheckConstraint(
            "(base_url IS NULL AND display_name IS NULL) OR "
            "(base_url IS NOT NULL AND display_name IS NOT NULL)",
            name="custom_provider_fields_together",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    # Custom OpenAI-compatible providers are created in the admin UI. Built-in providers
    # keep these fields NULL and continue to use their checked-in registry definitions.
    display_name: Mapped[str | None] = mapped_column(String(80))
    base_url: Mapped[str | None] = mapped_column(String(500))
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # An admin's standing authorization to send pictures from documents to this provider for description.
    # NULL: never sent. Cleared when a custom provider's endpoint changes.
    image_sharing_authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    image_sharing_authorized_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Optimistic concurrency token: every change increments it; clients send the version they saw.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class ProviderCredential(Base):
    __tablename__ = "provider_credentials"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'active', 'retired')", name="status_valid"),
        # Anything that can still be used must have ciphertext; retired rows are crypto-shredded.
        CheckConstraint(
            "status = 'retired' OR (ciphertext IS NOT NULL AND nonce IS NOT NULL)",
            name="usable_has_ciphertext",
        ),
        Index(
            "uq_provider_credentials_one_active",
            "provider_config_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "uq_provider_credentials_one_pending",
            "provider_config_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_configs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    key_hint: Mapped[str] = mapped_column(String(8), nullable=False)  # last 4 characters only
    # Custom providers: the normalized origin this key was verified against; it is also bound into the
    # ciphertext's associated data. NULL for built-in providers (fixed endpoint in code).
    endpoint_origin: Mapped[str | None] = mapped_column(String(300))
    # NOT NULL: a credential row only ever exists after a successful live verification.
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class AuditLog(Base):
    """Append-only (enforced by a trigger in the migration). Never stores secrets."""

    __tablename__ = "audit_logs"
    __table_args__ = (CheckConstraint("outcome IN ('success', 'failure')", name="outcome_valid"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    # No foreign key: audit history must survive user deletion untouched.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_email: Mapped[str | None] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target: Mapped[str | None] = mapped_column(String(100))
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    request_id: Mapped[str | None] = mapped_column(String(64))


class LegacyBrowserRun(Base):
    """Approval-gated read-only browser request; captured page text is never persisted."""

    __tablename__ = "legacy_browser_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_approval', 'running', 'completed', 'failed', 'rejected', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint("attempts BETWEEN 0 AND 2", name="attempts_valid"),
        Index("ix_legacy_browser_runs_requester_status", "requester_user_id", "status"),
        Index("ix_legacy_browser_runs_status_created", "status", "requested_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requester_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approver_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    start_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    link_labels: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'pending_approval'"))
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    failure_code: Mapped[str | None] = mapped_column(String(60))
    requested_at: Mapped[datetime] = _now()
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Document(Base):
    """An uploaded file (`file_format`: pdf, docx, xlsx, pptx, txt, csv, png, jpeg, audio or video). `version`/`storage_key` describe the latest submitted file;
    `indexed_*` describe the content currently searchable (which survives a failed replacement)."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("visibility IN ('private', 'shared', 'restricted')", name="visibility_valid"),
        CheckConstraint("status IN ('pending', 'processing', 'ready', 'failed')", name="status_valid"),
        CheckConstraint("source_system IN ('upload', 'file_server')", name="source_system_valid"),
        CheckConstraint(
            "file_format IN ('pdf', 'docx', 'xlsx', 'pptx', 'txt', 'csv', 'md', 'json', 'html', 'png', 'jpeg', 'webp', 'audio', 'video')",
            name="file_format_valid",
        ),
        CheckConstraint("acl_status IN ('active', 'quarantined')", name="acl_status_valid"),
        CheckConstraint("(acl_status = 'active') = (acl_quarantine_reason IS NULL)", name="quarantine_has_reason"),
        # Synced documents are never private to their (technical) owner and never organization-wide.
        CheckConstraint("source_system = 'upload' OR visibility = 'restricted'", name="synced_is_restricted"),
        Index("ix_documents_job_queue", "status", "next_attempt_at"),
        Index("ix_documents_sha256", "sha256"),
        Index("ix_documents_source_uri", "source_uri"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    visibility: Mapped[str] = mapped_column(String(16), nullable=False)
    source_system: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'upload'"))
    source_uri: Mapped[str | None] = mapped_column(String(1024))
    # Last scheduled folder scan that observed this path; used for bounded, SQL-side removal detection.
    folder_sync_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("folder_sync_runs.id", ondelete="SET NULL"), index=True
    )
    # A trigger forces synced documents to start quarantined (fail closed until the first valid ACL sync).
    acl_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    acl_quarantine_reason: Mapped[str | None] = mapped_column(String(64))
    acl_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    filename: Mapped[str] = mapped_column(String(255), nullable=False)  # display only, sanitized
    # Detected from the file's content (and required to match its extension); selects the parser.
    file_format: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pdf'"))
    storage_key: Mapped[str] = mapped_column(String(64), nullable=False)  # random; never user input
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(255))
    page_count: Mapped[int | None] = mapped_column(Integer)  # pages, slides or sheets; NULL for flowing text

    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    indexed_version: Mapped[int | None] = mapped_column(Integer)
    indexed_filename: Mapped[str | None] = mapped_column(String(255))
    # Document identity for discovery (app.rag.discovery): normalised filename words, dates, title and headings.
    # Never sent to the model. `descriptor_search` is a generated tsvector over it (migration 0016), read via SQL.
    descriptor: Mapped[str | None] = mapped_column(Text)
    descriptor_version: Mapped[int | None] = mapped_column(SmallInteger)
    embedding_model: Mapped[str | None] = mapped_column(String(200))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class IngestionWorkerHeartbeat(Base):
    """A running document worker (or embedded worker loop). Rows are refreshed every heartbeat interval and
    removed on clean shutdown; a stale row means the worker stopped. Used for status only, never for
    claiming jobs (claims use the document row lock and lease)."""

    __tablename__ = "ingestion_workers"
    __table_args__ = (
        CheckConstraint("mode IN ('worker', 'embedded')", name="mode_valid"),
        Index("ix_ingestion_workers_heartbeat", "heartbeat_at"),
    )

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    active_jobs: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    started_at: Mapped[datetime] = _now()
    heartbeat_at: Mapped[datetime] = _now()


class DocumentVersion(Base):
    """Immutable archived source file retained when a document is replaced or folder-synced."""

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_document_versions_document_version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "file_format IN ('pdf', 'docx', 'xlsx', 'pptx', 'txt', 'csv', 'md', 'json', 'html', 'png', 'jpeg', 'webp', 'audio', 'video')",
            name="file_format_valid",
        ),
        Index("ix_document_versions_document_archived", "document_id", "archived_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_format: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    archived_at: Mapped[datetime] = _now()
    archived_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reason: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'replacement'"))


class DocumentObligation(Base):
    """A source-quoted action or duty tracked against the exact document revision that states it."""

    __tablename__ = "document_obligations"
    __table_args__ = (
        CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="title_length"),
        CheckConstraint("length(btrim(source_quote)) BETWEEN 8 AND 1200", name="source_quote_length"),
        CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="rationale_length"),
        CheckConstraint("source_version >= 1", name="source_version_positive"),
        CheckConstraint("source_page_number >= 1", name="source_page_positive"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("status IN ('open', 'acknowledged', 'completed')", name="status_valid"),
        CheckConstraint("length(source_sha256) = 64", name="source_hash_length"),
        CheckConstraint("(status = 'completed') = (completed_at IS NOT NULL)", name="completion_timestamp_valid"),
        Index("ix_document_obligations_assignee_status", "assigned_to_user_id", "status", "due_at"),
        Index("ix_document_obligations_document_status", "document_id", "status", "due_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_quote: Mapped[str] = mapped_column(Text, nullable=False)
    source_page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_locator: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    assigned_to_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'open'"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("'1'"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class DocumentObligationEvent(Base):
    """Append-only history for obligation assignment, scheduling and status transitions."""

    __tablename__ = "document_obligation_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('created', 'assigned', 'reassigned', 'due_date_changed', 'acknowledged', 'completed', 'reopened')",
            name="action_valid",
        ),
        CheckConstraint("from_status IS NULL OR from_status IN ('open', 'acknowledged', 'completed')", name="from_status_valid"),
        CheckConstraint("to_status IS NULL OR to_status IN ('open', 'acknowledged', 'completed')", name="to_status_valid"),
        CheckConstraint("rationale IS NULL OR length(btrim(rationale)) BETWEEN 3 AND 2000", name="rationale_length"),
        Index("ix_document_obligation_events_obligation", "obligation_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    obligation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_obligations.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    rationale: Mapped[str | None] = mapped_column(String(2000))
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _now()


class FinancialFormRecord(Base):
    """Immutable source-version extraction draft; human corrections live in append-only revisions."""

    __tablename__ = "financial_form_records"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "document_version", "profile", "date_order",
            name="uq_financial_form_records_source_profile_date_order",
        ),
        CheckConstraint("document_version >= 1", name="document_version_positive"),
        CheckConstraint("length(source_sha256) = 64", name="source_sha256_length"),
        CheckConstraint("profile IN ('invoice', 'purchase_order', 'form')", name="profile_valid"),
        CheckConstraint("date_order IN ('dmy', 'mdy', 'ymd')", name="date_order_valid"),
        CheckConstraint("extraction_status IN ('ready', 'needs_review')", name="extraction_status_valid"),
        CheckConstraint("jsonb_typeof(extraction_data) = 'object'", name="extraction_data_object"),
        Index("ix_financial_form_records_source", "document_id", "document_version"),
        Index("ix_financial_form_records_queue", "extraction_status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    profile: Mapped[str] = mapped_column(String(24), nullable=False)
    date_order: Mapped[str] = mapped_column(String(3), nullable=False)
    extraction_status: Mapped[str] = mapped_column(String(16), nullable=False)
    extraction_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()


class FinancialFormReviewRevision(Base):
    """Append-only reviewer corrections and exception acknowledgement for one extraction draft."""

    __tablename__ = "financial_form_review_revisions"
    __table_args__ = (
        UniqueConstraint("record_id", "revision", name="uq_financial_form_review_revisions_record_revision"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="rationale_length"),
        CheckConstraint("jsonb_typeof(reviewed_fields) = 'object'", name="reviewed_fields_object"),
        CheckConstraint("jsonb_typeof(reviewed_line_items) = 'array'", name="reviewed_line_items_array"),
        CheckConstraint("jsonb_typeof(reviewed_exception_indexes) = 'array'", name="exception_indexes_array"),
        Index("ix_financial_form_review_revisions_record", "record_id", "revision"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("financial_form_records.id", ondelete="CASCADE"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    reviewed_fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reviewed_line_items: Mapped[list] = mapped_column(JSONB, nullable=False)
    reviewed_exception_indexes: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _now()


class TrainingCourse(Base):
    """A source-backed learning plan and personal completion state for the employee who created it."""

    __tablename__ = "training_courses"
    __table_args__ = (
        CheckConstraint("status IN ('in_progress', 'completed')", name="status_valid"),
        CheckConstraint("(status = 'completed') = (completed_at IS NOT NULL)", name="completion_timestamp_valid"),
        Index("ix_training_courses_owner_created", "owner_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    topic: Mapped[str] = mapped_column(String(2000), nullable=False)
    plan: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Source versions are stored per module so reads can enforce current ACLs and freshness before returning text.
    source_snapshots: Mapped[list[list[dict]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # The public questions live with the plan; correct answers are stored separately and never serialized.
    quiz_answer_key: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'in_progress'"))
    created_at: Mapped[datetime] = _now()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrainingAssignment(Base):
    """A manager-assigned course, visible only to the assigned employee and its authorized manager."""

    __tablename__ = "training_assignments"
    __table_args__ = (
        UniqueConstraint("course_id", "assignee_user_id", name="uq_training_assignments_course_assignee"),
        CheckConstraint("status IN ('assigned', 'completed')", name="status_valid"),
        CheckConstraint("(status = 'completed') = (completed_at IS NOT NULL)", name="completion_timestamp_valid"),
        Index("ix_training_assignments_assignee_created", "assignee_user_id", "created_at"),
        Index("ix_training_assignments_department_due", "department_group_id", "due_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("training_courses.id", ondelete="CASCADE"), nullable=False)
    assignee_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    assigned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    department_group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("access_groups.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'assigned'"))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrainingQuizAttempt(Base):
    """Append-only score record; answers are choice indexes and never contain lesson source text."""

    __tablename__ = "training_quiz_attempts"
    __table_args__ = (
        CheckConstraint("score >= 0 AND max_score > 0 AND score <= max_score", name="score_valid"),
        CheckConstraint("jsonb_typeof(answers) = 'array'", name="answers_are_array"),
        Index("ix_training_quiz_attempts_user_course", "user_id", "course_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("training_courses.id", ondelete="RESTRICT"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("training_assignments.id", ondelete="RESTRICT")
    )
    answers: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    max_score: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _now()


class TrainingPracticeScenario(Base):
    """Immutable, owner-scoped adaptive question selection and its optional scored attempt."""

    __tablename__ = "training_practice_scenarios"
    __table_args__ = (
        CheckConstraint("jsonb_typeof(question_indexes) = 'array'", name="question_indexes_are_array"),
        CheckConstraint("jsonb_array_length(question_indexes) BETWEEN 1 AND 5", name="question_count_valid"),
        CheckConstraint("source_digest ~ '^[0-9a-f]{64}$'", name="source_digest_valid"),
        CheckConstraint("(attempt_id IS NOT NULL) = (completed_at IS NOT NULL)", name="completion_pair_valid"),
        Index("ix_training_practice_user_course", "user_id", "course_id", "created_at"),
        Index(
            "uq_training_practice_attempt",
            "attempt_id",
            unique=True,
            postgresql_where=text("attempt_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("training_courses.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("training_assignments.id", ondelete="RESTRICT")
    )
    # Zero-based indexes into this course's reviewed, source-cited quiz; question content is not copied.
    question_indexes: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("training_quiz_attempts.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = _now()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrainingReminder(Base):
    """Idempotent, internal due-date reminder; no email or external notification is sent."""

    __tablename__ = "training_reminders"
    __table_args__ = (
        UniqueConstraint("assignment_id", "reminder_type", name="uq_training_reminders_assignment_type"),
        CheckConstraint("reminder_type IN ('due_soon', 'overdue')", name="reminder_type_valid"),
        Index("ix_training_reminders_recipient_created", "recipient_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("training_assignments.id", ondelete="RESTRICT"), nullable=False
    )
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    reminder_type: Mapped[str] = mapped_column(String(20), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _now()
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AutomationWorkflow(Base):
    """Immutable version of a department-scoped workflow; only enabled/next_run_at may change."""

    __tablename__ = "automation_workflows"
    __table_args__ = (
        UniqueConstraint("definition_key", "version", name="uq_automation_workflows_key_version"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("trigger_type IN ('manual', 'interval', 'service_request', 'request_sla')", name="trigger_valid"),
        CheckConstraint(
            "action_type IN ('create_task', 'notify_member', 'assign_personal_task')",
            name="action_allowlisted",
        ),
        CheckConstraint(
            "(trigger_type IN ('manual', 'service_request', 'request_sla') AND interval_minutes IS NULL) OR "
            "(trigger_type = 'interval' AND interval_minutes BETWEEN 60 AND 10080)",
            name="trigger_interval_valid",
        ),
        CheckConstraint("jsonb_typeof(action_config) = 'object'", name="action_config_object"),
        Index(
            "uq_automation_workflows_one_enabled_version",
            "definition_key",
            unique=True,
            postgresql_where=text("enabled"),
        ),
        Index("ix_automation_workflows_due", "enabled", "next_run_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    definition_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    department_group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("access_groups.id", ondelete="RESTRICT"), nullable=False
    )
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False)
    interval_minutes: Mapped[int | None] = mapped_column(Integer)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'create_task'"))
    action_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AutomationRun(Base):
    """Approval-gated workflow run with a lease-based retry state machine."""

    __tablename__ = "automation_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_automation_runs_idempotency_key"),
        CheckConstraint(
            "status IN ('pending_approval', 'queued', 'processing', 'completed', 'rejected', 'cancelled', 'failed')",
            name="status_valid",
        ),
        CheckConstraint("attempts >= 0 AND attempts <= 3", name="attempts_valid"),
        CheckConstraint("(status = 'completed') = (completed_at IS NOT NULL)", name="completed_timestamp_valid"),
        Index("ix_automation_runs_due", "status", "available_at"),
        Index("ix_automation_runs_department_created", "department_group_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("automation_workflows.id", ondelete="RESTRICT"), nullable=False
    )
    requester_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    department_group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("access_groups.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'pending_approval'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    available_at: Mapped[datetime] = _now()
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AutomationApproval(Base):
    """One immutable decision per workflow run."""

    __tablename__ = "automation_approvals"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_automation_approvals_run"),
        CheckConstraint("decision IN ('approved', 'rejected')", name="decision_valid"),
        CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="rationale_length"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("automation_runs.id", ondelete="RESTRICT"), nullable=False)
    approver_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = _now()


class AutomationTask(Base):
    """Internal task created transactionally by an approved workflow; visible under department ACLs."""

    __tablename__ = "automation_tasks"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_automation_tasks_run"),
        CheckConstraint("status IN ('open', 'completed')", name="status_valid"),
        CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="title_length"),
        CheckConstraint("length(btrim(body)) BETWEEN 1 AND 4000", name="body_length"),
        CheckConstraint("(status = 'completed') = (completed_at IS NOT NULL)", name="completion_timestamp_valid"),
        Index("ix_automation_tasks_assignee_created", "assignee_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("automation_runs.id", ondelete="RESTRICT"), nullable=False)
    assignee_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    department_group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("access_groups.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    body: Mapped[str] = mapped_column(String(4000), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'open'"))
    created_at: Mapped[datetime] = _now()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PersonalTask(Base):
    """A direct, audited assignment kept separate from approval-gated workflow tasks."""

    __tablename__ = "personal_tasks"
    __table_args__ = (
        UniqueConstraint("automation_run_id", name="uq_personal_tasks_automation_run_id"),
        CheckConstraint("status IN ('open', 'completed', 'cancelled')", name="status_valid"),
        CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="title_length"),
        CheckConstraint("length(btrim(body)) BETWEEN 1 AND 4000", name="body_length"),
        CheckConstraint(
            "(status = 'open' AND completed_at IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND completed_at IS NULL AND cancelled_at IS NOT NULL)",
            name="terminal_timestamp_valid",
        ),
        Index("ix_personal_tasks_assignee_deadline", "assignee_user_id", "status", "due_at"),
        Index("ix_personal_tasks_creator_created", "created_by_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    automation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("automation_runs.id", ondelete="RESTRICT")
    )
    assignee_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    department_group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_groups.id", ondelete="RESTRICT"))
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    body: Mapped[str] = mapped_column(String(4000), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'open'"))
    created_at: Mapped[datetime] = _now()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[User] = relationship(foreign_keys=[created_by_user_id])
    assignee: Mapped[User] = relationship(foreign_keys=[assignee_user_id])
    department: Mapped["AccessGroup | None"] = relationship("AccessGroup")

    @property
    def created_by_email(self) -> str:
        return self.created_by.email

    @property
    def assignee_email(self) -> str:
        return self.assignee.email


class PersonalTaskReminder(Base):
    """Idempotent in-app reminder and durable SMTP outbox item for an assigned task."""

    __tablename__ = "personal_task_reminders"
    __table_args__ = (
        UniqueConstraint("task_id", "reminder_type", name="uq_personal_task_reminders_task_type"),
        CheckConstraint("reminder_type IN ('due_soon', 'overdue')", name="reminder_type_valid"),
        CheckConstraint(
            "email_status IN ('disabled', 'pending', 'sending', 'sent', 'retry', 'failed', 'cancelled')",
            name="email_status_valid",
        ),
        Index("ix_personal_task_reminders_recipient_created", "recipient_user_id", "created_at"),
        Index("ix_personal_task_reminders_email_queue", "email_status", "email_next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("personal_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    reminder_type: Mapped[str] = mapped_column(String(20), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _now()
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'disabled'"))
    email_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("'0'"))
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_last_error_code: Mapped[str | None] = mapped_column(String(64))


class WorkflowNotification(Base):
    """Recipient-scoped in-app workflow event; payload contains only opaque record IDs."""

    __tablename__ = "workflow_notifications"
    __table_args__ = (
        CheckConstraint(
            "notification_type IN ('run_approved', 'run_rejected', 'task_assigned', 'approval_required', 'workflow_message')",
            name="notification_type_valid",
        ),
        CheckConstraint(
            "(notification_type = 'task_assigned' AND task_id IS NOT NULL AND message_title IS NULL AND message_body IS NULL) OR "
            "(notification_type = 'workflow_message' AND task_id IS NULL AND message_title IS NOT NULL AND message_body IS NOT NULL) OR "
            "(notification_type IN ('run_approved', 'run_rejected', 'approval_required') AND task_id IS NULL AND message_title IS NULL AND message_body IS NULL)",
            name="task_reference_valid",
        ),
        UniqueConstraint(
            "run_id",
            "recipient_user_id",
            "notification_type",
            name="uq_workflow_notifications_run_recipient_type",
        ),
        Index("ix_workflow_notifications_recipient_created", "recipient_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("automation_runs.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("automation_tasks.id", ondelete="RESTRICT")
    )
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    notification_type: Mapped[str] = mapped_column(String(24), nullable=False)
    message_title: Mapped[str | None] = mapped_column(String(180))
    message_body: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = _now()
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentProfile(Base):
    """Immutable, versioned department-scoped agent configuration.

    Profiles only select capabilities from the application's fixed tool registry; they do not
    contain prompts, credentials, executable code, or document content.
    """

    __tablename__ = "agent_profiles"
    __table_args__ = (
        UniqueConstraint("agent_key", "version", name="uq_agent_profiles_key_version"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "agent_type IN ('knowledge_research', 'market_research', 'company_analysis', 'employee_training')",
            name="agent_type_valid",
        ),
        CheckConstraint("knowledge_scope IN ('requester_acl', 'department_only')", name="knowledge_scope_valid"),
        CheckConstraint(
            "jsonb_typeof(tool_policy) = 'array' AND "
            "tool_policy <@ '[\"knowledge_search\", \"public_web_research\", "
            "\"company_data_analysis\", \"training_plan\"]'::jsonb",
            name="tool_policy_allowlisted",
        ),
        Index(
            "uq_agent_profiles_one_enabled_version",
            "agent_key",
            unique=True,
            postgresql_where=text("enabled"),
        ),
        Index("ix_agent_profiles_department", "department_group_id", "enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    agent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    department_group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("access_groups.id", ondelete="RESTRICT"), nullable=False
    )
    knowledge_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    tool_policy: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()


class AgentProfileRun(Base):
    """Private encrypted result history for synchronous and queued agent capability runs."""

    __tablename__ = "agent_profile_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="status_valid",
        ),
        CheckConstraint("attempts BETWEEN 0 AND 2", name="attempts_valid"),
        CheckConstraint("execution_mode IN ('sync', 'queue')", name="execution_mode_valid"),
        CheckConstraint(
            "capability IN ('knowledge_search', 'public_web_research', 'company_data_analysis', 'training_plan')",
            name="capability_valid",
        ),
        CheckConstraint(
            "(result_ciphertext IS NULL AND result_nonce IS NULL AND result_key_id IS NULL) OR "
            "(result_ciphertext IS NOT NULL AND result_nonce IS NOT NULL AND result_key_id IS NOT NULL)",
            name="result_encryption_complete",
        ),
        CheckConstraint(
            "(task_ciphertext IS NULL AND task_nonce IS NULL AND task_key_id IS NULL) OR "
            "(task_ciphertext IS NOT NULL AND task_nonce IS NOT NULL AND task_key_id IS NOT NULL)",
            name="task_encryption_complete",
        ),
        CheckConstraint(
            "content_purged_at IS NULL OR (task_ciphertext IS NULL AND task_nonce IS NULL AND task_key_id IS NULL "
            "AND result_ciphertext IS NULL AND result_nonce IS NULL AND result_key_id IS NULL)",
            name="content_purged_empty",
        ),
        Index("ix_agent_profile_runs_requester_recent", "requester_user_id", "requested_at"),
        Index("ix_agent_profile_runs_department_recent", "department_group_id", "requested_at"),
        Index(
            "ix_agent_profile_runs_retention_requested_at",
            "requested_at",
            postgresql_where=text("content_purged_at IS NULL AND status IN ('completed', 'failed')"),
        ),
        Index(
            "ix_agent_profile_runs_queue_available",
            "available_at",
            "requested_at",
            postgresql_where=text("status = 'queued' AND execution_mode = 'queue'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    department_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("access_groups.id", ondelete="SET NULL")
    )
    requester_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    capability: Mapped[str] = mapped_column(String(40), nullable=False)
    task_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    task_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    task_key_id: Mapped[str | None] = mapped_column(String(32))
    result_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    result_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    result_key_id: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'running'"))
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'sync'"))
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    failure_code: Mapped[str | None] = mapped_column(String(60))
    requested_at: Mapped[datetime] = _now()
    available_at: Mapped[datetime] = _now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentChunk(Base):
    """A searchable passage. `id` (never reused) is also the vector ID in the vector store."""

    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_position"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 1-based position of the chunk's top-level unit: the page for PDFs; for other formats the section,
    # sheet, slide or line/row block ordinal (see `locator`).
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Structured source location, e.g. {"kind": "sheet", "sheet": "Q1", "range": "A5:C40", "label": ...}.
    # NULL for chunks indexed before locators existed (PDF pages).
    locator: Mapped[dict | None] = mapped_column(JSONB)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # float32 vector bytes: lets the vector index be rebuilt without re-reading PDFs.
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = _now()


class DocumentTable(Base):
    """A table of a PDF or DOCX kept as rows and columns (see `app.rag.parsers.table_structure`), so questions that
    need every row are computed over all of them (`app.rag.tables`). Replaced together with the document's chunks
    and readable only through the document's access rules. Unstructured tables (`structured` false: header or
    shape unclear) are kept for the record but never computed over."""

    __tablename__ = "document_tables"
    __table_args__ = (UniqueConstraint("document_id", "table_index", name="uq_document_tables_position"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_index: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based, as in chunk locators
    title: Mapped[str] = mapped_column(String(300), nullable=False, server_default=text("''"))
    locator_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # "page" (PDF) or "section" (DOCX)
    columns: Mapped[list] = mapped_column(JSONB, nullable=False)
    rows: Mapped[list] = mapped_column(JSONB, nullable=False)  # data rows only (no header or total rows)
    row_positions: Mapped[list] = mapped_column(JSONB, nullable=False)  # page or section of each row
    summary_rows: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    structured: Mapped[bool] = mapped_column(Boolean, nullable=False)
    note: Mapped[str] = mapped_column(String(200), nullable=False, server_default=text("''"))
    created_at: Mapped[datetime] = _now()


class Conversation(Base):
    """A user's knowledge-chat conversation. Private to its owner (no admin read path).
    Kept separate from the RAG knowledge index: chat text is never embedded or retrieved as knowledge."""

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_user_recent", "user_id", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ConversationMessage(Base):
    """One user question or assistant answer. Assistant rows record which provider/model produced them
    and the RAG response details (citations, chart, page images) as provider-independent JSON."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="role_valid"),
        CheckConstraint("feedback IN (-1, 1)", name="feedback_valid"),
        Index("ix_conversation_messages_conversation_order", "conversation_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    strategy: Mapped[str | None] = mapped_column(String(40))
    insufficient_evidence: Mapped[bool | None] = mapped_column(Boolean)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # The owner's rating of an assistant answer: 1 helpful, -1 not helpful, NULL not rated.
    feedback: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = _now()


class RagTrace(Base):
    """How one knowledge-chat question was answered: strategy, retrieved chunk IDs and scores, the evidence
    decision, per-stage latency, token usage, and the error type and stack if it failed.

    Metadata only, like the logs: no question, answer, prompt or document text is stored here, so an admin
    can diagnose answers without reading a user's conversation. Rows expire after RAG_TRACE_RETENTION_DAYS."""

    __tablename__ = "rag_traces"
    __table_args__ = (
        CheckConstraint("status IN ('ok', 'error')", name="status_valid"),
        Index("ix_rag_traces_created", "created_at"),
        Index("ix_rag_traces_message", "message_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    request_id: Mapped[str | None] = mapped_column(String(64))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("conversation_messages.id", ondelete="SET NULL")
    )
    route: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy: Mapped[str | None] = mapped_column(String(40))
    decision: Mapped[str | None] = mapped_column(String(60))  # why this answer was shown, refused or general
    provider: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    insufficient_evidence: Mapped[bool | None] = mapped_column(Boolean)
    general_knowledge: Mapped[bool | None] = mapped_column(Boolean)
    question_chars: Mapped[int] = mapped_column(Integer, nullable=False)
    history_turns: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    retrieved: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    timings: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(120))
    error_stack: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class ModelRoutingRecord(Base):
    """One routed LLM call: task class, chosen model, fallback chain, token
    usage, latency and cost. Metadata only -- no prompt, answer, key or
    endpoint is stored here, so spend can be audited without reading a
    user's conversation. Written by the chat/RAG routes after a routed call
    (or with status 'refused' when the budget guard refused it)."""

    __tablename__ = "model_routing_records"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ok', 'downgraded', 'refused', 'error')", name="status_valid"
        ),
        CheckConstraint(
            "budget_action IN ('allow', 'downgrade', 'refuse')", name="budget_action_valid"
        ),
        Index("ix_model_routing_records_created", "created_at"),
        Index("ix_model_routing_records_task_created", "task_class", "created_at"),
        Index("ix_model_routing_records_department_created", "department_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    request_id: Mapped[str | None] = mapped_column(String(64))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    task_class: Mapped[str] = mapped_column(String(32), nullable=False)
    policy: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'cheapest'"))
    provider: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    alternates: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    alternates_tried: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'ok'"))
    budget_action: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'allow'"))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    created_at: Mapped[datetime] = _now()



class RoutingPolicySetting(Base):
    """Step T slice 4: the admin-editable routing policy. One row (id = 1)
    per installation, like OrgSettings. No credentials, keys or endpoints
    are stored here -- only which model the router picks (default policy,
    per-task-class overrides, per-model capability tiers) and how much
    routed calls may spend each month (org-wide budget plus per-department
    overrides). A missing row means the default policy: cheapest, no
    overrides, no budget enforced."""

    __tablename__ = "routing_policy_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint(
            "default_policy IN ('cheapest', 'fastest', 'balanced')",
            name="default_policy_valid",
        ),
        CheckConstraint(
            "monthly_budget_usd IS NULL OR monthly_budget_usd >= 0",
            name="monthly_budget_non_negative",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    default_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'cheapest'")
    )
    # {"complex": "balanced"} -- task class -> policy. Empty = use the default.
    task_class_policies: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # {"provider:model": 1|2|3} -- admin reasoning-tier overrides.
    capability_tiers: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    monthly_budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    # {department AccessGroup id: "12.50"} -- a department's own budget wins
    # over the org-wide one; amounts are stored as strings to keep exact
    # decimals through JSONB.
    department_budgets: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = _now()
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class MemoryItem(Base):
    """A user's personal memory (ADR-004 WP2): a preference, terminology or personal fact the user asked
    InfraSpine to remember. Owner-only (no admin read path), never embedded in the knowledge index,
    never cited and never used as evidence. Edits create a new version that supersedes the old one;
    forgetting deletes the item together with its earlier versions."""

    __tablename__ = "memory_items"
    __table_args__ = (
        CheckConstraint("kind IN ('preference', 'terminology', 'fact')", name="kind_valid"),
        CheckConstraint("status IN ('active', 'superseded')", name="status_valid"),
        CheckConstraint("source IN ('chat', 'manual')", name="source_valid"),
        CheckConstraint("length(btrim(content)) BETWEEN 1 AND 500", name="content_length"),
        CheckConstraint("(subject IS NULL) = (subject_key IS NULL)", name="subject_has_key"),
        CheckConstraint("(status = 'superseded') = (superseded_at IS NOT NULL)", name="superseded_has_time"),
        # No duplicate and no conflicting active memories, even under concurrent requests.
        Index(
            "uq_memory_items_active_content", "owner_user_id", "content_key",
            unique=True, postgresql_where=text("status = 'active'"),
        ),
        Index(
            "uq_memory_items_active_subject", "owner_user_id", "subject_key",
            unique=True, postgresql_where=text("status = 'active' AND subject_key IS NOT NULL"),
        ),
        Index("ix_memory_items_owner_recent", "owner_user_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(100))
    subject_key: Mapped[str | None] = mapped_column(String(100))  # normalised subject, for supersession
    content: Mapped[str] = mapped_column(String(500), nullable=False)
    content_key: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 of normalised content
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("memory_items.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _now()
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ------------------------------------------------------------------ access control (ADR-004 WP1)


class AccessGroup(Base):
    """A department, project, client or custom group. Local groups are managed here; a directory group
    (`source = 'directory'`, with `external_id`) is owned by the company directory."""

    __tablename__ = "access_groups"
    __table_args__ = (
        CheckConstraint("kind IN ('department', 'project', 'client', 'custom')", name="kind_valid"),
        CheckConstraint("source IN ('local', 'directory')", name="source_valid"),
        CheckConstraint("source = 'local' OR external_id IS NOT NULL", name="directory_has_external_id"),
        Index("uq_access_groups_kind_name", "kind", text("lower(name)"), unique=True),
        Index(
            "uq_access_groups_external_id", "external_id", unique=True, postgresql_where=text("external_id IS NOT NULL")
        ),
        Index("ix_access_groups_parent_id", "parent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'local'"))
    external_id: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = _now()
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_groups.id", ondelete="SET NULL"))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    lifecycle_status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'active'"))


class AccessGroupMember(Base):
    __tablename__ = "access_group_members"

    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("access_groups.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    added_at: Mapped[datetime] = _now()
    added_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class DepartmentManager(Base):
    """A manager assignment is scoped to one local department and never grants global admin rights."""

    __tablename__ = "department_managers"
    __table_args__ = (Index("ix_department_managers_user", "manager_user_id"),)

    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("access_groups.id", ondelete="CASCADE"), primary_key=True)
    manager_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    assigned_at: Mapped[datetime] = _now()


class EmployeeHandoff(Base):
    """Durable employee process knowledge, visible to its author and the scoped department managers."""

    __tablename__ = "employee_handoffs"
    __table_args__ = (
        CheckConstraint("status IN ('submitted', 'acknowledged')", name="status_valid"),
        CheckConstraint("length(btrim(title)) BETWEEN 1 AND 180", name="title_length"),
        CheckConstraint("length(btrim(body)) BETWEEN 1 AND 12000", name="body_length"),
        CheckConstraint(
            "(status = 'acknowledged' AND acknowledged_at IS NOT NULL AND acknowledged_by IS NOT NULL) "
            "OR (status = 'submitted' AND acknowledged_at IS NULL AND acknowledged_by IS NULL)",
            name="acknowledgement_complete",
        ),
        Index("ix_employee_handoffs_department_created", "department_group_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    author_email: Mapped[str] = mapped_column(String(320), nullable=False)
    department_group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("access_groups.id", ondelete="RESTRICT"))
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'submitted'"))
    created_at: Mapped[datetime] = _now()
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ServiceRequest(Base):
    """Employee-submitted internal support request with HR privacy and department-scoped triage."""

    __tablename__ = "service_requests"
    __table_args__ = (
        CheckConstraint("category IN ('it', 'hr', 'access', 'facilities', 'other', 'purchase', 'expense')", name="category_valid"),
        # Purchase requests and expense claims carry a positive amount; other requests never do.
        CheckConstraint(
            "(category IN ('purchase', 'expense')) = (amount IS NOT NULL AND currency IS NOT NULL) AND (amount IS NULL OR amount > 0)",
            name="amount_matches_category",
        ),
        CheckConstraint("priority IN ('low', 'normal', 'high', 'urgent')", name="priority_valid"),
        CheckConstraint(
            "status IN ('submitted', 'acknowledged', 'in_progress', 'waiting_requester', 'resolved', 'rejected', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint("length(btrim(subject)) BETWEEN 1 AND 160", name="subject_length"),
        CheckConstraint("length(btrim(description)) BETWEEN 1 AND 8000", name="description_length"),
        CheckConstraint(
            "(status IN ('resolved', 'rejected', 'cancelled')) = (closed_at IS NOT NULL)",
            name="terminal_status_has_closed_at",
        ),
        Index("ix_service_requests_requester_created", "requester_user_id", "created_at"),
        Index("ix_service_requests_department_status", "department_group_id", "status", "created_at"),
        Index(
            "ix_service_requests_response_sla_due",
            "response_due_at",
            "id",
            postgresql_where=text(
                "status = 'submitted' AND response_sla_met_at IS NULL AND sla_escalated_at IS NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requester_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    department_group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_groups.id", ondelete="SET NULL"))
    assigned_to_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'normal'"))
    subject: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'submitted'"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    response_sla_met_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sla_escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Custom form used, with the labels and answers as submitted (a later form edit never changes them).
    form_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("request_forms.id", ondelete="SET NULL"))
    form_name: Mapped[str | None] = mapped_column(String(120))
    form_version: Mapped[int | None] = mapped_column()
    form_answers: Mapped[list | None] = mapped_column(JSONB)


class ServiceRequestEvent(Base):
    """Append-only request timeline. Content is never copied into the general audit log."""

    __tablename__ = "service_request_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('submitted', 'status_changed', 'commented', 'cancelled', 'sla_escalated')",
            name="action_valid",
        ),
        CheckConstraint(
            "from_status IS NULL OR from_status IN "
            "('submitted', 'acknowledged', 'in_progress', 'waiting_requester', 'resolved', 'rejected', 'cancelled')",
            name="from_status_valid",
        ),
        CheckConstraint(
            "to_status IN ('submitted', 'acknowledged', 'in_progress', 'waiting_requester', 'resolved', 'rejected', 'cancelled')",
            name="to_status_valid",
        ),
        CheckConstraint("comment IS NULL OR length(comment) <= 2000", name="comment_length"),
        Index("ix_service_request_events_request_created", "request_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("service_requests.id", ondelete="RESTRICT"), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_email: Mapped[str] = mapped_column(String(320), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class ServiceRequestNotification(Base):
    """In-app escalation notification; only opaque identifiers and fixed notification types are stored."""

    __tablename__ = "service_request_notifications"
    __table_args__ = (
        CheckConstraint(
            "notification_type IN ('response_sla_escalated', 'approval_step_pending', 'approval_step_reminder', 'approval_step_escalated')",
            name="notification_type_valid"
        ),
        UniqueConstraint(
            "request_id",
            "recipient_user_id",
            "notification_type",
            name="uq_service_request_notifications_request_recipient_type",
        ),
        Index("ix_service_request_notifications_recipient_created", "recipient_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("service_requests.id", ondelete="RESTRICT"), nullable=False
    )
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    notification_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = _now()
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FolderSyncSource(Base):
    """An administrator-owned schedule rooted below the operator-configured import directory."""

    __tablename__ = "folder_sync_sources"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) BETWEEN 1 AND 120", name="name_length_valid"),
        CheckConstraint("length(relative_path) BETWEEN 1 AND 1024", name="relative_path_length_valid"),
        CheckConstraint("visibility IN ('private', 'shared', 'restricted')", name="visibility_valid"),
        CheckConstraint("(visibility = 'restricted') = (department_group_id IS NOT NULL)", name="department_scope_valid"),
        CheckConstraint("interval_minutes BETWEEN 5 AND 10080", name="interval_valid"),
        CheckConstraint("consecutive_failures >= 0", name="failures_nonnegative"),
        Index("ix_folder_sync_sources_due", "enabled", "next_sync_at", "locked_until"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[str] = mapped_column(String(24), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False)
    department_group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_groups.id", ondelete="RESTRICT"))
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    next_sync_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("folder_sync_runs.id", ondelete="SET NULL")
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class FolderSyncRun(Base):
    """Bounded count-only history for a scheduled synchronization attempt."""

    __tablename__ = "folder_sync_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'completed', 'partial', 'failed')", name="status_valid"),
        CheckConstraint("attempt >= 1", name="attempt_valid"),
        CheckConstraint(
            "scanned >= 0 AND created >= 0 AND updated >= 0 AND unchanged >= 0 AND removed >= 0 AND duplicates >= 0 AND failed >= 0",
            name="counts_nonnegative",
        ),
        Index("ix_folder_sync_runs_source_started", "source_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("folder_sync_sources.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'running'"))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    scanned: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    removed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    duplicates: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FolderSyncPreference(Base):
    __tablename__ = "folder_sync_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    change_alerts_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class FolderSyncNotification(Base):
    __tablename__ = "folder_sync_notifications"
    __table_args__ = (
        UniqueConstraint("run_id", "recipient_user_id", name="uq_folder_sync_notification_recipient"),
        Index("ix_folder_sync_notifications_recipient_created", "recipient_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("folder_sync_runs.id", ondelete="RESTRICT"), nullable=False)
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = _now()
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResourceAcl(Base):
    """Allow-read entries. One typed foreign key per resource type (only documents in WP1) and exactly
    one principal, a user or a group. There are no deny entries: anything not allowed is denied."""

    __tablename__ = "resource_acl"
    __table_args__ = (
        CheckConstraint("resource_type IN ('document')", name="resource_type_valid"),
        CheckConstraint("(resource_type = 'document') = (document_id IS NOT NULL)", name="resource_matches_type"),
        CheckConstraint("num_nonnulls(principal_user_id, principal_group_id) = 1", name="one_principal"),
        CheckConstraint("origin IN ('manual', 'source_sync')", name="origin_valid"),
        UniqueConstraint(
            "document_id", "principal_user_id", "principal_group_id",
            name="uq_resource_acl_entry", postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    principal_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    principal_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("access_groups.id", ondelete="CASCADE"), index=True
    )
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = _now()
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class CloudSourceAcl(Base):
    """Provider allow-list for a cloud document, intersected with its local department ACL."""

    __tablename__ = "cloud_source_acl"
    __table_args__ = (
        CheckConstraint("num_nonnulls(principal_user_id, principal_group_id) = 1", name="one_principal"),
        UniqueConstraint(
            "document_id", "principal_user_id", "principal_group_id",
            name="uq_cloud_source_acl_principal", postgresql_nulls_not_distinct=True,
        ),
        Index("ix_cloud_source_acl_user", "principal_user_id"),
        Index("ix_cloud_source_acl_group", "principal_group_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    principal_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    principal_group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_groups.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = _now()


class PermissionGrant(Base):
    """A reviewer, publisher or steward right, optionally limited to one category and one group scope
    (NULL = all). Grants are never edited or deleted, only revoked once (enforced by a trigger), so
    every decision can point at the grant it relied on."""

    __tablename__ = "permission_grants"
    __table_args__ = (
        CheckConstraint(
            f"permission IN ({', '.join(repr(permission) for permission in PERMISSIONS)})", name="permission_valid"
        ),
        CheckConstraint(
            f"category IS NULL OR category IN ({', '.join(repr(category) for category in KNOWLEDGE_CATEGORIES)})",
            name="category_valid",
        ),
        CheckConstraint("granted_by <> user_id", name="no_self_grant"),
        CheckConstraint("(revoked_at IS NULL) = (revoked_by IS NULL)", name="revocation_complete"),
        # Target of the composite foreign key that ties a decision to the approver's own grant.
        UniqueConstraint("id", "user_id", "permission", name="uq_permission_grants_owner"),
        Index(
            "uq_permission_grants_active", "user_id", "permission", "category", "scope_group_id",
            unique=True, postgresql_where=text("revoked_at IS NULL"), postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    role_template_assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "role_template_assignments.id",
            ondelete="RESTRICT",
            name="fk_permission_grants_role_template_assignment",
        ),
        index=True,
    )
    permission: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str | None] = mapped_column(String(32))
    scope_group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_groups.id"))
    granted_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    granted_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class RoleTemplate(Base):
    """An immutable Admin-defined bundle of catalogued, scoped resource/action permissions.

    Templates only target the existing Manager or Employee base roles. They cannot define an
    administrator or bypass any route's base-role and resource-scope checks.
    """

    __tablename__ = "role_templates"
    __table_args__ = (
        CheckConstraint("base_role IN ('manager', 'employee')", name="base_role_valid"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(version = 1 AND supersedes_id IS NULL) OR (version > 1 AND supersedes_id IS NOT NULL)",
            name="version_parent_matches",
        ),
        CheckConstraint("jsonb_typeof(permission_specs) = 'array'", name="permission_specs_array"),
        Index(
            "uq_role_templates_active_normalized_name",
            func.lower(text("name")),
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index("uq_role_templates_name_version", func.lower(text("name")), "version", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, server_default=text("''"))
    base_role: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    permission_specs: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("role_templates.id", ondelete="RESTRICT"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = _now()


class RoleTemplateAssignment(Base):
    """Append-only assignment lifecycle whose linked grants can be revoked as one audited unit."""

    __tablename__ = "role_template_assignments"
    __table_args__ = (
        CheckConstraint("(revoked_at IS NULL) = (revoked_by IS NULL)", name="revocation_complete"),
        CheckConstraint("jsonb_typeof(department_ids) = 'array'", name="department_ids_array"),
        Index(
            "ix_role_template_assignments_active_user",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "ix_role_template_assignments_expiring",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL AND expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    role_template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("role_templates.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    assigned_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    department_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class RoleTemplateChangeRequest(Base):
    """Immutable proposed definition and one append-only, separate-admin decision."""

    __tablename__ = "role_template_change_requests"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="status_valid"),
        CheckConstraint(
            "(status = 'pending' AND decided_by IS NULL AND decision_rationale IS NULL AND new_template_id IS NULL AND decided_at IS NULL) OR "
            "(status = 'approved' AND decided_by IS NOT NULL AND decision_rationale IS NOT NULL AND new_template_id IS NOT NULL AND decided_at IS NOT NULL) OR "
            "(status = 'rejected' AND decided_by IS NOT NULL AND decision_rationale IS NOT NULL AND new_template_id IS NULL AND decided_at IS NOT NULL)",
            name="decision_complete",
        ),
        CheckConstraint("length(btrim(decision_rationale)) >= 3 OR decision_rationale IS NULL", name="rationale_minimum"),
        CheckConstraint("jsonb_typeof(proposed_permission_specs) = 'array'", name="proposed_specs_array"),
        Index(
            "uq_role_template_change_pending",
            "role_template_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_role_template_change_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    role_template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("role_templates.id", ondelete="RESTRICT"), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    proposed_description: Mapped[str] = mapped_column(String(500), nullable=False, server_default=text("''"))
    proposed_permission_specs: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    decision_rationale: Mapped[str | None] = mapped_column(String(2000))
    new_template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("role_templates.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = _now()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GovernanceApproval(Base):
    """An append-only review or publish decision on a governed item. A PostgreSQL trigger binds
    the subject ID and author to either a knowledge claim or an organizational knowledge revision.

    Separation of duties, enforced by PostgreSQL:
    * no self-approval: single-row CHECK on the denormalised author;
    * one person per subject and one approval per stage: UNIQUE constraints;
    * the grant belongs to the approver and is the right permission: composite FOREIGN KEY;
    * the author matches the subject, it is still submitted, and the grant is active and covers
      the category and scope at decision time; publishing follows a review: BEFORE INSERT trigger;
    * decisions are never changed: append-only trigger.
    """

    __tablename__ = "governance_approvals"
    __table_args__ = (
        CheckConstraint("subject_type IN ('knowledge_claim', 'knowledge_revision')", name="subject_type_valid"),
        CheckConstraint("stage IN ('review', 'publish')", name="stage_valid"),
        CheckConstraint(
            "(stage = 'review' AND permission = 'knowledge.review') "
            "OR (stage = 'publish' AND permission = 'knowledge.publish')",
            name="stage_matches_permission",
        ),
        CheckConstraint(
            "category IN ('policy', 'financial', 'legal', 'client', 'operational', 'general')", name="category_valid"
        ),
        CheckConstraint("approver_user_id <> subject_author_user_id", name="no_self_approval"),
        CheckConstraint("length(btrim(rationale)) > 0", name="rationale_required"),
        UniqueConstraint("subject_type", "subject_id", "approver_user_id", name="uq_governance_approvals_one_role"),
        UniqueConstraint("subject_type", "subject_id", "stage", name="uq_governance_approvals_one_stage"),
        ForeignKeyConstraint(
            ["permission_grant_id", "approver_user_id", "permission"],
            ["permission_grants.id", "permission_grants.user_id", "permission_grants.permission"],
            name="fk_governance_approvals_own_grant",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subject_author_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    approver_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    permission: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_groups.id"))
    permission_grant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = _now()


# ------------------------------------------------------------------ knowledge claims (ADR-004 WP3)


class KnowledgeClaim(Base):
    """An unverified proposal or correction of organizational knowledge (ADR-004 WP3).

    The claimant is always the signed-in user (never taken from the request body). Claims are
    never embedded in the knowledge index, never cited, never written to personal memory and
    never treated as approved facts. Statement, claimant, kind, category, scope and sources
    are immutable after insert; the only status change WP3 allows is withdrawal.
    """

    __tablename__ = "knowledge_claims"
    __table_args__ = (
        CheckConstraint("kind IN ('proposal', 'correction')", name="kind_valid"),
        CheckConstraint("status IN ('submitted', 'withdrawn')", name="status_valid"),
        CheckConstraint(
            "category IN ('policy', 'financial', 'legal', 'client', 'operational', 'general')", name="category_valid"
        ),
        CheckConstraint("length(btrim(statement)) BETWEEN 1 AND 4000", name="statement_length"),
        CheckConstraint("jsonb_typeof(sources) = 'array'", name="sources_is_array"),
        CheckConstraint("jsonb_array_length(sources) BETWEEN 0 AND 20", name="sources_count"),
        CheckConstraint("(status = 'withdrawn') = (withdrawn_at IS NOT NULL)", name="withdrawn_has_time"),
        # Target of the composite foreign key from governance_approvals (claimant identity).
        UniqueConstraint("id", "claimant_user_id", name="uq_knowledge_claims_identity"),
        Index("ix_knowledge_claims_claimant_recent", "claimant_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claimant_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_groups.id"))
    sources: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'submitted'"))
    created_at: Mapped[datetime] = _now()
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeClaimEvent(Base):
    """Append-only status history for a claim. Written by a trigger so raw SQL updates are recorded too."""

    __tablename__ = "knowledge_claim_events"
    __table_args__ = (
        CheckConstraint("action IN ('submitted', 'withdrawn')", name="action_valid"),
        CheckConstraint("length(btrim(to_status)) > 0", name="to_status_required"),
        Index("ix_knowledge_claim_events_claim", "claim_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_claims.id"), nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = _now()


# ------------------------------------------------------------------ organizational knowledge (ADR-004 WP4)


class KnowledgeEntry(Base):
    """Identity of one organizational knowledge item. Content lives on immutable revisions.
    Publication leaves the authoring history intact; current source ACLs govern every RAG search."""

    __tablename__ = "knowledge_entries"
    __table_args__ = (
        UniqueConstraint("id", "author_user_id", name="uq_knowledge_entries_author"),
        Index("ix_knowledge_entries_owner_created", "owner_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    author_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    # Current custodian is separate from the immutable first author. This lets company knowledge
    # survive offboarding without rewriting who originally contributed it.
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_knowledge_entries_owner_user"), nullable=False
    )
    created_at: Mapped[datetime] = _now()
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_revisions.id", use_alter=True, name="fk_knowledge_entries_current_revision")
    )


class KnowledgeRevision(Base):
    """One immutable version of an entry. Statement, scope and author never change. Revisions
    move through draft, submitted, and published states; only two-person approval can publish."""

    __tablename__ = "knowledge_revisions"
    __table_args__ = (
        CheckConstraint("revision_number > 0", name="revision_number_positive"),
        CheckConstraint("status IN ('draft', 'submitted', 'published', 'superseded', 'withdrawn')", name="status_valid"),
        CheckConstraint("visibility IN ('private', 'restricted', 'shared')", name="visibility_valid"),
        CheckConstraint(
            "category IN ('policy', 'financial', 'legal', 'client', 'operational', 'general')", name="category_valid"
        ),
        CheckConstraint("length(btrim(statement)) BETWEEN 1 AND 4000", name="statement_length"),
        CheckConstraint("(visibility = 'restricted') = (scope_group_id IS NOT NULL)", name="scope_matches_visibility"),
        UniqueConstraint("entry_id", "revision_number", name="uq_knowledge_revisions_number"),
        ForeignKeyConstraint(["entry_id"], ["knowledge_entries.id"], name="fk_knowledge_revisions_entry"),
        ForeignKeyConstraint(["author_user_id"], ["users.id"], name="fk_knowledge_revisions_author_user"),
        Index(
            "uq_knowledge_revisions_one_draft",
            "entry_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    author_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_groups.id"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'draft'"))
    created_at: Mapped[datetime] = _now()
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeFreshnessReview(Base):
    """Append-only evidence that a different, currently authorized reviewer reconfirmed a published revision."""

    __tablename__ = "knowledge_freshness_reviews"
    __table_args__ = (
        CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="rationale_length"),
        CheckConstraint("next_review_due_at > reviewed_at", name="next_due_after_review"),
        Index("ix_knowledge_freshness_reviews_revision", "revision_id", "reviewed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_revisions.id", ondelete="RESTRICT"), nullable=False)
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    permission_grant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("permission_grants.id", ondelete="RESTRICT"), nullable=False)
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    reviewed_at: Mapped[datetime] = _now()
    next_review_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeConflict(Base):
    """A source-backed potential conflict between submitted and already published knowledge.

    Detection is advisory: it never changes publication or retrieval state. A scoped human reviewer
    records whether the statements actually conflict.
    """

    __tablename__ = "knowledge_conflicts"
    __table_args__ = (
        CheckConstraint("revision_id <> compared_revision_id", name="different_revisions"),
        CheckConstraint("status IN ('open', 'confirmed', 'dismissed')", name="status_valid"),
        UniqueConstraint("revision_id", "compared_revision_id", name="uq_knowledge_conflict_pair"),
        Index("ix_knowledge_conflicts_status_detected", "status", "detected_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    compared_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'open'"))
    detected_at: Mapped[datetime] = _now()


class KnowledgeConflictReview(Base):
    """Append-only human decision on one detected knowledge conflict."""

    __tablename__ = "knowledge_conflict_reviews"
    __table_args__ = (
        CheckConstraint("decision IN ('confirmed', 'dismissed')", name="decision_valid"),
        CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="rationale_length"),
        UniqueConstraint("conflict_id", name="uq_knowledge_conflict_review_once"),
        Index("ix_knowledge_conflict_reviews_reviewer_time", "reviewer_user_id", "decided_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    conflict_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_conflicts.id", ondelete="RESTRICT"), nullable=False
    )
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    permission_grant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permission_grants.id", ondelete="RESTRICT"), nullable=False
    )
    compared_permission_grant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permission_grants.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    decided_at: Mapped[datetime] = _now()


class KnowledgeProcess(Base):
    """A customer-defined operational process affected by published policies."""

    __tablename__ = "knowledge_processes"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) BETWEEN 1 AND 180", name="name_length"),
        CheckConstraint("length(btrim(description)) <= 2000", name="description_length"),
        Index("ix_knowledge_processes_scope", "scope_group_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False, server_default=text("''"))
    # The process follows the narrowest scope of the policy used to create it.
    scope_group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_groups.id", ondelete="RESTRICT"))
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class KnowledgeProcessPolicyLink(Base):
    """An auditable relationship from a process to a stable policy knowledge entry."""

    __tablename__ = "knowledge_process_policy_links"
    __table_args__ = (
        UniqueConstraint("process_id", "entry_id", name="uq_knowledge_process_policy_link"),
        Index("ix_knowledge_process_policy_links_entry", "entry_id", "active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    process_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_processes.id", ondelete="RESTRICT"), nullable=False)
    entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_entries.id", ondelete="RESTRICT"), nullable=False)
    linked_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class KnowledgePolicyException(Base):
    """A named owner and status for bringing a mapped process into policy compliance."""

    __tablename__ = "knowledge_policy_exceptions"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'acknowledged', 'resolved')", name="status_valid"),
        CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="rationale_length"),
        CheckConstraint("due_at IS NULL OR due_at > created_at", name="due_after_created"),
        Index("ix_knowledge_policy_exceptions_assignee", "assigned_to", "status", "created_at"),
        Index("ix_knowledge_policy_exceptions_process", "process_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    process_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_processes.id", ondelete="RESTRICT"), nullable=False)
    entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_entries.id", ondelete="RESTRICT"), nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_revisions.id", ondelete="RESTRICT"), nullable=False)
    assigned_to: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'open'"))
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class KnowledgePolicyExceptionEvent(Base):
    """Append-only audit of exception assignment, reassignment and resolution."""

    __tablename__ = "knowledge_policy_exception_events"
    __table_args__ = (
        CheckConstraint("action IN ('assigned', 'reassigned', 'acknowledged', 'resolved')", name="action_valid"),
        CheckConstraint("from_status IS NULL OR from_status IN ('open', 'acknowledged', 'resolved')", name="from_status_valid"),
        CheckConstraint("to_status IN ('open', 'acknowledged', 'resolved')", name="to_status_valid"),
        CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="rationale_length"),
        Index("ix_knowledge_policy_exception_events_exception", "exception_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    exception_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_policy_exceptions.id", ondelete="RESTRICT"), nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    from_assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    to_assignee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_at: Mapped[datetime] = _now()


class KnowledgeEvidence(Base):
    """Exact passage provenance for one revision. The quote, offsets and source identity are immutable.
    `document_id` / `chunk_id` are cleared if the source row goes away; the frozen keys stay so the
    citation can be recognised as orphaned. Orphan columns are the only fields an update may set."""

    __tablename__ = "knowledge_evidence"
    __table_args__ = (
        CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="offsets_ordered"),
        CheckConstraint("char_length(quote) = end_offset - start_offset", name="quote_matches_offsets"),
        CheckConstraint("char_length(quote) BETWEEN 1 AND 2000", name="quote_length"),
        CheckConstraint("char_length(prefix) <= 240 AND char_length(suffix) <= 240", name="context_length"),
        CheckConstraint(
            "orphan_reason IS NULL OR orphan_reason IN "
            "('document_revision_changed', 'source_deleted', 'passage_removed')",
            name="orphan_reason_valid",
        ),
        CheckConstraint("(orphan_reason IS NULL) = (orphaned_at IS NULL)", name="orphan_has_time"),
        CheckConstraint("document_id IS NULL OR document_id = document_key", name="document_key_matches"),
        CheckConstraint("chunk_id IS NULL OR chunk_id = chunk_key", name="chunk_key_matches"),
        Index("ix_knowledge_evidence_revision", "revision_id"),
        Index("ix_knowledge_evidence_document", "document_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_revisions.id"), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    document_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    chunk_id: Mapped[int | None] = mapped_column(ForeignKey("document_chunks.id", ondelete="SET NULL"))
    chunk_key: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    prefix: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    suffix: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    orphan_reason: Mapped[str | None] = mapped_column(String(40))
    orphaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class KnowledgeIndexVector(Base):
    """Reserved membership for an optional derived knowledge vector index. Current approved
    knowledge search uses PostgreSQL full text; any vector rows still require published revisions."""

    __tablename__ = "knowledge_index_vectors"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_revisions.id"), unique=True, nullable=False)
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = _now()


class IndexOutbox(Base):
    """Transactional outbox for knowledge-index mutations. Inserted in the same transaction as the
    change that caused it. Consumers are idempotent; completed rows are never rewritten."""

    __tablename__ = "index_outbox"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('knowledge.index', 'knowledge.unindex', 'knowledge.reconcile')", name="event_type_valid"
        ),
        CheckConstraint("status IN ('pending', 'processing', 'completed', 'failed')", name="status_valid"),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint("(status = 'completed') = (completed_at IS NOT NULL)", name="completed_has_time"),
        CheckConstraint("(status = 'completed') = (result IS NOT NULL)", name="completed_has_result"),
        Index("ix_index_outbox_due", "status", "available_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(255))
    result: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ForecastScenario(Base):
    """Immutable, owner-scoped what-if output with exact source passages and reproducible inputs."""

    __tablename__ = "forecast_scenarios"
    __table_args__ = (
        CheckConstraint("length(btrim(title)) BETWEEN 1 AND 160", name="title_valid"),
        CheckConstraint("length(btrim(metric)) BETWEEN 1 AND 100", name="metric_valid"),
        CheckConstraint("length(btrim(unit)) BETWEEN 1 AND 32", name="unit_valid"),
        CheckConstraint("period_unit IN ('period', 'month', 'quarter', 'year')", name="period_unit_valid"),
        CheckConstraint("horizon_periods BETWEEN 1 AND 36", name="horizon_valid"),
        CheckConstraint("model_version = 'linear-growth-v1'", name="model_version_valid"),
        CheckConstraint("length(input_digest) = 64", name="digest_valid"),
        CheckConstraint("jsonb_typeof(observations) = 'array' AND jsonb_array_length(observations) BETWEEN 2 AND 24", name="observations_valid"),
        CheckConstraint("jsonb_typeof(assumptions) = 'object'", name="assumptions_valid"),
        CheckConstraint("jsonb_typeof(projections) = 'array' AND jsonb_array_length(projections) BETWEEN 1 AND 36", name="projections_valid"),
        Index("ix_forecast_scenarios_owner_created", "owner_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    metric: Mapped[str] = mapped_column(String(100), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    period_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    horizon_periods: Mapped[int] = mapped_column(Integer, nullable=False)
    model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observations: Mapped[list] = mapped_column(JSONB, nullable=False)
    assumptions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    projections: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _now()


class CloudConnectorConfig(Base):
    """Durable connector metadata and authenticated encrypted provider credentials."""

    __tablename__ = "cloud_connector_configs"
    __table_args__ = (
        CheckConstraint(
            "connector_type IN ('google_drive', 'onedrive', 'sharepoint')", name="connector_type_valid"
        ),
        CheckConstraint("visibility IN ('private', 'shared', 'restricted')", name="visibility_valid"),
        CheckConstraint("sync_interval_minutes BETWEEN 5 AND 10080", name="sync_interval_valid"),
        CheckConstraint("last_sync_status IS NULL OR last_sync_status IN ('success', 'partial', 'failed')", name="last_sync_status_valid"),
        CheckConstraint("last_sync_error_count >= 0", name="error_count_nonnegative"),
        Index("ix_cloud_connector_configs_active_created", "is_active", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    connector_type: Mapped[str] = mapped_column(String(24), nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'restricted'"))
    department_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("access_groups.id", ondelete="SET NULL")
    )
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("60"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    sync_in_progress: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_status: Mapped[str | None] = mapped_column(String(16))
    last_sync_error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_cursor: Mapped[str | None] = mapped_column(Text)
    # Independent provider inventory cursor; opaque tokens are never returned from the API.
    permission_reconcile_cursor: Mapped[str | None] = mapped_column(Text)
    credential_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    credential_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    credential_key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = _now()
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


# Register the F02 tables on the shared Alembic/ORM metadata without coupling their
# mapped classes to this established model module.
from app.db.meeting_models import MeetingActionTask, MeetingArtifact, MeetingRevision, MeetingReview  # noqa: E402,F401
from app.db.knowledge_interview_models import (  # noqa: E402,F401
    KnowledgeInterviewReview,
    KnowledgeInterviewSession,
    KnowledgeInterviewSopRevision,
    KnowledgeInterviewTemplate,
)
from app.db.proposal_models import (  # noqa: E402,F401
    ProposalCoverageCase,
    ProposalCoverageRequirement,
    ProposalCoverageReview,
)
from app.db.agent_catalog_models import (  # noqa: E402,F401
    InternalAgentCatalogEvent,
    InternalAgentCatalogGrant,
    InternalAgentCatalogTemplate,
    InternalAgentCatalogVersion,
)
from app.db.agent_action_models import (  # noqa: E402,F401
    AgentActionProposal,
    ACTION_PENDING_APPROVAL,
    ACTION_APPROVED,
    ACTION_REJECTED,
    ACTION_EXPIRED,
    ACTION_EXECUTED,
    ACTION_STATUSES,
    ACTION_TYPES,
)
from app.db.org_models import OrgAssignmentHistory, Position  # noqa: E402,F401
from app.db.approval_models import ApprovalChainStep, ApprovalDelegation, ApprovalEmail, ApprovalLimit, Budget, RequestApprovalStep  # noqa: E402,F401
from app.db.form_models import RequestAttachment, RequestForm  # noqa: E402,F401
from app.db.webhook_models import Webhook, WebhookDelivery  # noqa: E402,F401
from app.db.agent_studio_models import (  # noqa: E402,F401
    AgentStudioAgent,
    AgentStudioApproval,
    AgentStudioMemory,
    AgentStudioRun,
    AgentStudioThread,
)
from app.db.agent_studio_whatsapp_models import (  # noqa: E402,F401
    AgentStudioWhatsAppConnection,
    AgentStudioWhatsAppContact,
    AgentStudioWhatsAppInbound,
)
from app.db.agent_studio_baileys_models import BaileysConnection, BaileysMessage  # noqa: E402,F401
