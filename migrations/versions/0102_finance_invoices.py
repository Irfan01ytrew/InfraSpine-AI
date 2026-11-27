"""Finance invoice flow (step E): vendor master, purchase orders, goods receipts, invoice batches, invoices,
payments, and the finance platform roles (separate verify / approve-payment / transfer duties).

Revision ID: 0102_finance_invoices
Revises: 0101_unified_workflow_runtime
"""
from collections.abc import Sequence
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0102_finance_invoices"
down_revision: str | None = "0101_unified_workflow_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = {
    "finance.viewer": ("Finance viewer", ("finance.read",)),
    "finance.clerk": ("Invoice clerk", ("finance.read", "invoice.ingest", "workflow.access")),
    "finance.verifier": ("Invoice verifier", ("finance.read", "invoice.verify", "workflow.access")),
    "finance.approver": ("Payment approver", ("finance.read", "payment.approve", "workflow.access")),
    "finance.treasury": ("Treasury (funds transfer)", ("finance.read", "funds.transfer", "workflow.access")),
    "finance.admin": ("Finance administrator", ("finance.read", "finance.manage", "invoice.ingest", "workflow.access")),
}
MONEY = sa.Numeric(18, 2)


def _ts(name):
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "fin_vendors",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("name_key", sa.String(200), nullable=False),
        sa.Column("tax_id", sa.String(64)),
        sa.Column("bank_account", sa.String(64), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["group_id"], ["access_groups.id"], name=op.f("fk_fin_vendors_group_id_access_groups"), ondelete="RESTRICT"),
        sa.UniqueConstraint("group_id", "name_key", name=op.f("uq_fin_vendors_group_id")),
        sa.CheckConstraint("status IN ('active','blocked')", name=op.f("ck_fin_vendors_status_valid")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fin_vendors")),
    )
    op.create_table(
        "fin_purchase_orders",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("po_number", sa.String(64), nullable=False),
        sa.Column("vendor_id", sa.UUID(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'open'"), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fin_purchase_orders")),
        sa.ForeignKeyConstraint(["group_id"], ["access_groups.id"], name=op.f("fk_fin_purchase_orders_group_id_access_groups"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["vendor_id"], ["fin_vendors.id"], name=op.f("fk_fin_purchase_orders_vendor_id_fin_vendors"), ondelete="RESTRICT"),
        sa.UniqueConstraint("group_id", "po_number", name=op.f("uq_fin_purchase_orders_group_id")),
        sa.CheckConstraint("status IN ('open','closed','cancelled')", name=op.f("ck_fin_purchase_orders_status_valid")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_fin_purchase_orders_amount_positive")),
    )
    op.create_table(
        "fin_goods_receipts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("po_id", sa.UUID(), nullable=False),
        sa.Column("receipt_number", sa.String(64), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("received_on", sa.Date(), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fin_goods_receipts")),
        sa.ForeignKeyConstraint(["group_id"], ["access_groups.id"], name=op.f("fk_fin_goods_receipts_group_id_access_groups"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["po_id"], ["fin_purchase_orders.id"], name=op.f("fk_fin_goods_receipts_po_id_fin_purchase_orders"), ondelete="RESTRICT"),
        sa.UniqueConstraint("group_id", "receipt_number", name=op.f("uq_fin_goods_receipts_group_id")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_fin_goods_receipts_amount_positive")),
    )
    op.create_index(op.f("ix_fin_goods_receipts_po"), "fin_goods_receipts", ["po_id"])
    op.create_table(
        "fin_invoice_batches",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'open'"), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("tolerance_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("tolerance_abs", MONEY, nullable=False),
        sa.Column("round", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("wf_run_id", sa.UUID(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fin_invoice_batches")),
        sa.ForeignKeyConstraint(["group_id"], ["access_groups.id"], name=op.f("fk_fin_invoice_batches_group_id_access_groups"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_fin_invoice_batches_created_by_users"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["wf_run_id"], ["wf_runs.id"], name=op.f("fk_fin_invoice_batches_wf_run_id_wf_runs"), ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('open','verifying','awaiting_approval','paying','completed','failed','cancelled')",
                           name=op.f("ck_fin_invoice_batches_status_valid")),
        sa.CheckConstraint("tolerance_pct >= 0 AND tolerance_pct <= 20", name=op.f("ck_fin_invoice_batches_tolerance_valid")),
    )
    op.create_index(op.f("ix_fin_invoice_batches_group"), "fin_invoice_batches", ["group_id", "created_at"])
    op.create_table(
        "fin_payments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("vendor_id", sa.UUID(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("payee_account", sa.String(64), nullable=False),
        sa.Column("invoice_ids", postgresql.JSONB(), nullable=False),
        sa.Column("approval_id", sa.UUID(), nullable=True),
        sa.Column("action_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'awaiting_approval'"), nullable=False),
        sa.Column("execution_id", sa.UUID(), nullable=True),
        sa.Column("connector", sa.String(40), nullable=True),
        sa.Column("external_ref", sa.String(200), nullable=True),
        sa.Column("failure_code", sa.String(80), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fin_payments")),
        sa.ForeignKeyConstraint(["group_id"], ["access_groups.id"], name=op.f("fk_fin_payments_group_id_access_groups"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["batch_id"], ["fin_invoice_batches.id"], name=op.f("fk_fin_payments_batch_id_fin_invoice_batches"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["vendor_id"], ["fin_vendors.id"], name=op.f("fk_fin_payments_vendor_id_fin_vendors"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["approval_id"], ["platform_approval_requests.id"], name=op.f("fk_fin_payments_approval_id_platform_approval_requests"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["execution_id"], ["platform_action_executions.id"], name=op.f("fk_fin_payments_execution_id_platform_action_executions"), ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('awaiting_approval','approved','rejected','expired','dispatched','verified','failed')",
                           name=op.f("ck_fin_payments_status_valid")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_fin_payments_amount_positive")),
    )
    op.create_index(op.f("ix_fin_payments_batch"), "fin_payments", ["batch_id", "status"])
    op.create_table(
        "fin_invoices",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("invoice_number", sa.String(64)),
        sa.Column("vendor_name", sa.String(200)),
        sa.Column("vendor_id", sa.UUID()),
        sa.Column("po_number", sa.String(64)),
        sa.Column("department", sa.String(120)),
        sa.Column("amount", MONEY),
        sa.Column("currency", sa.String(3)),
        sa.Column("invoice_date", sa.Date()),
        sa.Column("bank_account_on_invoice", sa.String(64)),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_ref", sa.String(255)),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("extraction", postgresql.JSONB()),
        sa.Column("status", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("reasons", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("verified_by", sa.UUID()),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("payment_id", sa.UUID()),
        sa.Column("resolution_note", sa.String(500)),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fin_invoices")),
        sa.ForeignKeyConstraint(["group_id"], ["access_groups.id"], name=op.f("fk_fin_invoices_group_id_access_groups"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["batch_id"], ["fin_invoice_batches.id"], name=op.f("fk_fin_invoices_batch_id_fin_invoice_batches"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["vendor_id"], ["fin_vendors.id"], name=op.f("fk_fin_invoices_vendor_id_fin_vendors"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["verified_by"], ["users.id"], name=op.f("fk_fin_invoices_verified_by_users"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payment_id"], ["fin_payments.id"], name=op.f("fk_fin_invoices_payment_id_fin_payments"), ondelete="RESTRICT"),
        sa.UniqueConstraint("batch_id", "seq", name=op.f("uq_fin_invoices_batch_id")),
        sa.CheckConstraint("status IN ('pending','verified','exception','approved','paid','rejected')", name=op.f("ck_fin_invoices_status_valid")),
        sa.CheckConstraint("source_kind IN ('json','text','file','document')", name=op.f("ck_fin_invoices_source_kind_valid")),
    )
    op.create_index(op.f("ix_fin_invoices_dupe"), "fin_invoices", ["group_id", "vendor_id", "invoice_number"])
    op.create_index(op.f("ix_fin_invoices_queue"), "fin_invoices", ["group_id", "status"])
    op.create_index(op.f("ix_fin_invoices_spend"), "fin_invoices", ["group_id", "invoice_date"])

    bind = op.get_bind()
    for key, (name, permissions) in ROLES.items():
        bind.execute(sa.text("INSERT INTO platform_roles (id, key, name, system) VALUES (:id, :key, :name, true) "
                             "ON CONFLICT (key) DO NOTHING"), {"id": uuid.uuid4(), "key": key, "name": name})
        for permission in permissions:
            bind.execute(sa.text("INSERT INTO platform_role_permissions (role_id, permission) SELECT id, :p FROM "
                                 "platform_roles WHERE key = :key ON CONFLICT DO NOTHING"), {"p": permission, "key": key})


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM fin_payments WHERE status IN ('dispatched','verified'))")):
        raise RuntimeError("Executed payments exist. Restore a verified backup instead of downgrading.")
    for table in ("fin_invoices", "fin_payments", "fin_invoice_batches", "fin_goods_receipts", "fin_purchase_orders", "fin_vendors"):
        op.drop_table(table)
    bind.execute(sa.text("DELETE FROM platform_memberships WHERE role_id IN (SELECT id FROM platform_roles WHERE key = ANY(:keys))"),
                 {"keys": list(ROLES)})
    bind.execute(sa.text("DELETE FROM platform_roles WHERE key = ANY(:keys)"), {"keys": list(ROLES)})
