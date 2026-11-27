"""Custom request forms: administrator-defined fields for a kind of request (e.g. "New laptop", "Travel advance").

A form fixes the request category, may route to a department, and lists typed fields. Editing the fields bumps
`version`; each request keeps the labels and answers it was submitted with, so later edits never change history.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

FIELD_TYPES = ("text", "long_text", "number", "date", "choice", "yes_no", "file")


class RequestForm(Base):
    __tablename__ = "request_forms"
    __table_args__ = (
        CheckConstraint("category IN ('it', 'hr', 'access', 'facilities', 'other', 'purchase', 'expense')", name="category_valid"),
        CheckConstraint("length(btrim(name)) BETWEEN 2 AND 120", name="name_valid"),
        CheckConstraint("description IS NULL OR length(description) <= 500", name="description_length"),
        CheckConstraint("jsonb_typeof(fields) = 'array'", name="fields_array"),
        CheckConstraint("jsonb_typeof(audience) = 'object'", name="audience_object"),
        CheckConstraint("version >= 1", name="version_valid"),
        Index("uq_request_forms_active_name", func.lower(text("name")), unique=True, postgresql_where=text("is_active")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    # Requests made with this form go to this department (when set), whether or not the requester belongs to it.
    department_group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_groups.id", ondelete="SET NULL"))
    fields: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # Who may use the form: {"departments": [ids], "positions": [ids]}; both empty means everyone.
    # Members and managers of a listed department, and holders of a listed position, qualify; admins always do.
    audience: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    version: Mapped[int] = mapped_column(nullable=False, server_default=text("1"))
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RequestAttachment(Base):
    """A file uploaded for a form's file field; `request_id` is set when the request is submitted."""

    __tablename__ = "request_attachments"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("(request_id IS NULL) = (linked_at IS NULL)", name="linked_consistent"),
        Index("ix_request_attachments_request", "request_id"),
        Index("ix_request_attachments_pending", "uploader_user_id", "created_at", postgresql_where=text("request_id IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    uploader_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    request_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("service_requests.id", ondelete="CASCADE"))
    field_key: Mapped[str | None] = mapped_column(String(40))
    filename: Mapped[str] = mapped_column(String(220), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
