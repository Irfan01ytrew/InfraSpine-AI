"""Finance: vendor master, purchase orders, goods receipts, invoice batches, invoices and payments (step E).

Every row is scoped to a department (`group_id`, the same scope as workflows). Permissions are evaluated on that
department's platform scope: finance.read, invoice.ingest, invoice.verify, payment.approve, funds.transfer,
finance.manage. Money is NUMERIC(18,2); amounts never travel as floats.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String,
                        UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _uid():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _group():
    return mapped_column(ForeignKey('access_groups.id', ondelete='RESTRICT'), nullable=False)


def _ts():
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


MONEY = Numeric(18, 2)


class FinVendor(Base):
    __tablename__ = 'fin_vendors'
    __table_args__ = (
        UniqueConstraint('group_id', 'name_key'),
        CheckConstraint("status IN ('active','blocked')", name='status_valid'),
    )
    id: Mapped[uuid.UUID] = _uid()
    group_id: Mapped[uuid.UUID] = _group()
    name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200))
    tax_id: Mapped[str | None] = mapped_column(String(64))
    bank_account: Mapped[str] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16), default='active', server_default=text("'active'"))
    # Step R slice 4: only allow-listed vendors may be auto-picked-up by the
    # autopilot sweep. Default False: a new vendor always needs a human to
    # start its first batch. Set only via the vendor save endpoint.
    autopilot_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                    server_default=text("false"))
    created_at: Mapped[datetime] = _ts()


class FinPurchaseOrder(Base):
    __tablename__ = 'fin_purchase_orders'
    __table_args__ = (
        UniqueConstraint('group_id', 'po_number'),
        CheckConstraint("status IN ('open','closed','cancelled')", name='status_valid'),
        CheckConstraint('amount > 0', name='amount_positive'),
    )
    id: Mapped[uuid.UUID] = _uid()
    group_id: Mapped[uuid.UUID] = _group()
    po_number: Mapped[str] = mapped_column(String(64))
    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('fin_vendors.id', ondelete='RESTRICT'))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16), default='open', server_default=text("'open'"))
    created_at: Mapped[datetime] = _ts()


class FinGoodsReceipt(Base):
    __tablename__ = 'fin_goods_receipts'
    __table_args__ = (UniqueConstraint('group_id', 'receipt_number'), CheckConstraint('amount > 0', name='amount_positive'),
                      Index('ix_fin_goods_receipts_po', 'po_id'))
    id: Mapped[uuid.UUID] = _uid()
    group_id: Mapped[uuid.UUID] = _group()
    po_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('fin_purchase_orders.id', ondelete='RESTRICT'))
    receipt_number: Mapped[str] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    received_on: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = _ts()


class FinInvoiceBatch(Base):
    __tablename__ = 'fin_invoice_batches'
    __table_args__ = (
        CheckConstraint("status IN ('open','verifying','awaiting_approval','paying','completed','failed','cancelled')",
                        name='status_valid'),
        CheckConstraint('tolerance_pct >= 0 AND tolerance_pct <= 20', name='tolerance_valid'),
        Index('ix_fin_invoice_batches_group', 'group_id', 'created_at'),
    )
    id: Mapped[uuid.UUID] = _uid()
    group_id: Mapped[uuid.UUID] = _group()
    name: Mapped[str] = mapped_column(String(160))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    status: Mapped[str] = mapped_column(String(20), default='open', server_default=text("'open'"))
    currency: Mapped[str] = mapped_column(String(3))
    tolerance_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal('2.00'))
    tolerance_abs: Mapped[Decimal] = mapped_column(MONEY, default=Decimal('0'))
    round: Mapped[int] = mapped_column(Integer, default=0, server_default=text('0'))
    wf_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('wf_runs.id', ondelete='RESTRICT'))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class FinPayment(Base):
    __tablename__ = 'fin_payments'
    __table_args__ = (
        CheckConstraint("status IN ('awaiting_approval','approved','rejected','expired','dispatched','verified','failed')",
                        name='status_valid'),
        CheckConstraint('amount > 0', name='amount_positive'),
        Index('ix_fin_payments_batch', 'batch_id', 'status'),
    )
    id: Mapped[uuid.UUID] = _uid()
    group_id: Mapped[uuid.UUID] = _group()
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('fin_invoice_batches.id', ondelete='RESTRICT'))
    round: Mapped[int] = mapped_column(Integer, default=1)
    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('fin_vendors.id', ondelete='RESTRICT'))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    payee_account: Mapped[str] = mapped_column(String(64))
    invoice_ids: Mapped[list] = mapped_column(JSONB)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('platform_approval_requests.id', ondelete='RESTRICT'))
    action_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default='awaiting_approval', server_default=text("'awaiting_approval'"))
    execution_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('platform_action_executions.id', ondelete='RESTRICT'))
    connector: Mapped[str | None] = mapped_column(String(40))
    external_ref: Mapped[str | None] = mapped_column(String(200))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class FinInvoice(Base):
    __tablename__ = 'fin_invoices'
    __table_args__ = (
        UniqueConstraint('batch_id', 'seq'),
        CheckConstraint("status IN ('pending','verified','exception','approved','paid','rejected')", name='status_valid'),
        CheckConstraint("source_kind IN ('json','text','file','document')", name='source_kind_valid'),
        Index('ix_fin_invoices_dupe', 'group_id', 'vendor_id', 'invoice_number'),
        Index('ix_fin_invoices_queue', 'group_id', 'status'),
        Index('ix_fin_invoices_spend', 'group_id', 'invoice_date'),
    )
    id: Mapped[uuid.UUID] = _uid()
    group_id: Mapped[uuid.UUID] = _group()
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('fin_invoice_batches.id', ondelete='RESTRICT'))
    seq: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default=text('1'))
    invoice_number: Mapped[str | None] = mapped_column(String(64))
    vendor_name: Mapped[str | None] = mapped_column(String(200))
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('fin_vendors.id', ondelete='RESTRICT'))
    po_number: Mapped[str | None] = mapped_column(String(64))
    department: Mapped[str | None] = mapped_column(String(120))
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str | None] = mapped_column(String(3))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    bank_account_on_invoice: Mapped[str | None] = mapped_column(String(64))
    source_kind: Mapped[str] = mapped_column(String(16))
    source_ref: Mapped[str | None] = mapped_column(String(255))
    source_digest: Mapped[str] = mapped_column(String(64))
    extraction: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default='pending', server_default=text("'pending'"))
    reasons: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    verified_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('fin_payments.id', ondelete='RESTRICT'))
    resolution_note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
