"""Finance service: scoped vendor master, purchase orders, receipts, invoice batches, exceptions, approvals,
payments and the spend chart. Every lookup is filtered by the department (access group) and authorised on that
department's platform scope; a record from another department is "not found".
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select, update

from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.db.finance_models import (FinGoodsReceipt, FinInvoice, FinInvoiceBatch, FinPayment, FinPurchaseOrder,
                                   FinVendor)
from app.db.models import AccessGroup, User
from app.db.workflow_engine_models import WorkflowRun, WorkflowStep
from app.finance import extraction
from app.finance.verification import name_key
from app.finance.workflow import TEMPLATE_DESCRIPTION, TEMPLATE_NAME, TEMPLATE_REF, invoice_spec
from app.platform.access import (effective_permissions, ensure_role, grant_membership, principal_for, require,
                                 visible_scopes)
from app.platform.approvals import decide_approval
from app.platform.events import audit, publish
from app.platform.models import PlatformApprovalRequest, PlatformMembership, PlatformRole, PlatformScope
from app.platform.sync import group_scope_sync
from app.platform.timezone import org_today

FINANCE_ROLES = ('finance.viewer', 'finance.clerk', 'finance.verifier', 'finance.approver', 'finance.treasury',
                 'finance.admin')
ROLE_PERMISSIONS = {  # same as migration 0102 (recreated on demand if missing)
    'finance.viewer': {'finance.read'},
    'finance.clerk': {'finance.read', 'invoice.ingest', 'workflow.access'},
    'finance.verifier': {'finance.read', 'invoice.verify', 'workflow.access'},
    'finance.approver': {'finance.read', 'payment.approve', 'workflow.access'},
    'finance.treasury': {'finance.read', 'funds.transfer', 'workflow.access'},
    'finance.admin': {'finance.read', 'finance.manage', 'invoice.ingest', 'workflow.access'},
}
FINANCE_PERMISSIONS = ('finance.read', 'finance.manage', 'invoice.ingest', 'invoice.verify', 'payment.approve',
                       'funds.transfer')
ACTIVE_RUN = ('queued', 'running', 'paused', 'compensating')
SPEND_STATUSES = ('verified', 'approved', 'paid')


def batch_subject(batch_id) -> str:
    return f'invoice_batch:{batch_id}'


def money(value) -> str | None:
    return None if value is None else format(Decimal(value).quantize(Decimal('0.01')), 'f')


class FinanceService:
    def __init__(self, db, user: User, settings, cipher=None, ctx=None):
        self.db, self.user, self.settings, self.cipher = db, user, settings, cipher
        self.p = principal_for(user)

    # ------------------------------------------------------------------ scope and permission

    async def platform_scope(self, group_id) -> uuid.UUID:
        group = await self.db.get(AccessGroup, group_id)
        if group is None or group.kind != 'department':
            raise NotFoundError('Department not found.')
        scope = await self.db.run_sync(lambda s: group_scope_sync(s.connection(), group_id))
        if scope is None:
            raise NotFoundError('Department not found.')
        return scope

    async def require(self, group_id, permission: str) -> uuid.UUID:
        scope = await self.platform_scope(group_id)
        await require(self.db, self.p, permission, scope)
        return scope

    async def readable_groups(self, permission: str = 'finance.read') -> list[uuid.UUID]:
        flt = await visible_scopes(self.db, self.p, permission)
        rows = await self.db.scalars(select(PlatformScope.legacy_id).where(
            PlatformScope.legacy_table == 'access_groups', flt.sql(PlatformScope.id)))
        return [row for row in rows if row is not None]

    async def scopes(self) -> list[dict]:
        groups = await self.readable_groups()
        out = []
        for group in (await self.db.scalars(select(AccessGroup).where(
                AccessGroup.id.in_(groups or [uuid.uuid4()]), AccessGroup.kind == 'department')
                .order_by(AccessGroup.name))).all():
            scope = await self.platform_scope(group.id)
            perms = await effective_permissions(self.db, self.user.id, scope)
            out.append({'id': str(group.id), 'name': group.name,
                        'permissions': sorted(p for p in FINANCE_PERMISSIONS if p in perms or '*' in perms)})
        return out

    # ------------------------------------------------------------------ master data

    async def vendors(self, group_id) -> list[FinVendor]:
        await self.require(group_id, 'finance.read')
        return list((await self.db.scalars(select(FinVendor).where(FinVendor.group_id == group_id)
                                           .order_by(FinVendor.name))).all())

    async def save_vendor(self, group_id, *, name, bank_account, currency, tax_id=None, status='active') -> FinVendor:
        await self.require(group_id, 'finance.manage')
        key = name_key(name)
        if not key:
            raise InvalidInputError('Vendor name is required.')
        account = extraction.normalise_account(bank_account)
        if not account:
            raise InvalidInputError('A bank account is required for payments.')
        row = await self.db.scalar(select(FinVendor).where(FinVendor.group_id == group_id, FinVendor.name_key == key)
                                   .with_for_update())
        before = None
        if row is None:
            row = FinVendor(id=uuid.uuid4(), group_id=group_id, name=name.strip()[:200], name_key=key)
            self.db.add(row)
        else:
            before = row.bank_account
        row.bank_account, row.currency, row.tax_id, row.status = account, currency.upper(), tax_id, status
        await self.db.flush()
        audit(self.db, self.p, 'finance.vendor_saved', target=str(row.id),
              details={'group_id': str(group_id), 'bank_account_changed': before is not None and before != account,
                       'status': status})
        return row

    async def purchase_orders(self, group_id) -> list[dict]:
        await self.require(group_id, 'finance.read')
        received = (select(FinGoodsReceipt.po_id, func.sum(FinGoodsReceipt.amount).label('received'))
                    .group_by(FinGoodsReceipt.po_id).subquery())
        rows = (await self.db.execute(select(FinPurchaseOrder, FinVendor.name, received.c.received)
                                      .join(FinVendor, FinVendor.id == FinPurchaseOrder.vendor_id)
                                      .outerjoin(received, received.c.po_id == FinPurchaseOrder.id)
                                      .where(FinPurchaseOrder.group_id == group_id)
                                      .order_by(FinPurchaseOrder.po_number))).all()
        return [{'id': str(po.id), 'po_number': po.po_number, 'vendor_id': str(po.vendor_id), 'vendor_name': vendor,
                 'amount': money(po.amount), 'currency': po.currency, 'status': po.status,
                 'received': money(rec or 0)} for po, vendor, rec in rows]

    async def save_purchase_order(self, group_id, *, po_number, vendor_id, amount, currency, status='open'):
        await self.require(group_id, 'finance.manage')
        vendor = await self.db.scalar(select(FinVendor).where(FinVendor.id == vendor_id, FinVendor.group_id == group_id))
        if vendor is None:
            raise NotFoundError('Vendor not found in this department.')
        number = po_number.strip().upper()[:64]
        row = await self.db.scalar(select(FinPurchaseOrder).where(FinPurchaseOrder.group_id == group_id,
                                                                  FinPurchaseOrder.po_number == number).with_for_update())
        if row is None:
            row = FinPurchaseOrder(id=uuid.uuid4(), group_id=group_id, po_number=number)
            self.db.add(row)
        row.vendor_id, row.amount, row.currency, row.status = vendor.id, Decimal(amount), currency.upper(), status
        await self.db.flush()
        audit(self.db, self.p, 'finance.purchase_order_saved', target=str(row.id), details={'group_id': str(group_id)})
        return row

    async def add_receipt(self, group_id, *, po_id, receipt_number, amount, received_on: date):
        await self.require(group_id, 'finance.manage')
        po = await self.db.scalar(select(FinPurchaseOrder).where(FinPurchaseOrder.id == po_id,
                                                                 FinPurchaseOrder.group_id == group_id))
        if po is None:
            raise NotFoundError('Purchase order not found in this department.')
        row = FinGoodsReceipt(id=uuid.uuid4(), group_id=group_id, po_id=po.id, receipt_number=receipt_number.strip()[:64],
                              amount=Decimal(amount), received_on=received_on)
        self.db.add(row)
        await self.db.flush()
        audit(self.db, self.p, 'finance.goods_received', target=str(row.id), details={'po_id': str(po.id)})
        return row

    # ------------------------------------------------------------------ batches and ingestion

    async def batch(self, batch_id, permission='finance.read', *, lock=False) -> FinInvoiceBatch:
        query = select(FinInvoiceBatch).where(FinInvoiceBatch.id == batch_id)
        if lock:
            query = query.with_for_update()
        row = await self.db.scalar(query)
        if row is None:
            raise NotFoundError('Invoice batch not found.')
        await self.require(row.group_id, permission)  # 404 without department membership
        return row

    async def batches(self, group_id=None) -> list[FinInvoiceBatch]:
        groups = await self.readable_groups()
        if group_id is not None:
            if group_id not in groups:
                await self.require(group_id, 'finance.read')  # raises 404/403 with the right reason
            groups = [group_id]
        if not groups:
            return []
        return list((await self.db.scalars(select(FinInvoiceBatch).where(FinInvoiceBatch.group_id.in_(groups))
                                           .order_by(FinInvoiceBatch.created_at.desc()).limit(200))).all())

    async def create_batch(self, group_id, *, name, currency, tolerance_pct=Decimal('2'), tolerance_abs=Decimal('0')):
        await self.require(group_id, 'invoice.ingest')
        row = FinInvoiceBatch(id=uuid.uuid4(), group_id=group_id, name=name.strip()[:160], created_by=self.user.id,
                              status='open', currency=currency.upper(), tolerance_pct=Decimal(tolerance_pct),
                              tolerance_abs=Decimal(tolerance_abs), round=0)
        self.db.add(row)
        await self.db.flush()
        audit(self.db, self.p, 'finance.batch_created', target=str(row.id), details={'group_id': str(group_id)})
        return row

    async def _active_run(self, batch) -> WorkflowRun | None:
        if batch.wf_run_id is None:
            return None
        run = await self.db.get(WorkflowRun, batch.wf_run_id, populate_existing=True)
        return run if run is not None and run.status in ACTIVE_RUN else None

    async def add_invoices(self, batch_id, items: list[tuple[str, dict, str | None, str]]) -> list[FinInvoice]:
        """items: (source_kind, facts, source_ref, source_digest). Re-sending the same source is idempotent."""
        batch = await self.batch(batch_id, 'invoice.ingest', lock=True)
        if batch.status == 'cancelled':
            raise ConflictError('This batch is cancelled.')
        count = await self.db.scalar(select(func.count()).select_from(FinInvoice).where(FinInvoice.batch_id == batch.id))
        if count + len(items) > self.settings.finance_max_batch_invoices:
            raise InvalidInputError(f'A batch holds at most {self.settings.finance_max_batch_invoices} invoices.')
        seq = await self.db.scalar(select(func.coalesce(func.max(FinInvoice.seq), 0)).where(FinInvoice.batch_id == batch.id))
        known = {d: i for d, i in (await self.db.execute(select(FinInvoice.source_digest, FinInvoice.id)
                                                         .where(FinInvoice.batch_id == batch.id))).all()}
        out = []
        for kind, facts, ref, source_digest in items:
            if source_digest in known:
                out.append(await self.db.get(FinInvoice, known[source_digest]))
                continue
            typed = extraction.coerce(facts)
            seq += 1
            row = FinInvoice(id=uuid.uuid4(), group_id=batch.group_id, batch_id=batch.id, seq=seq, revision=1,
                             invoice_number=typed.get('invoice_number'), vendor_name=typed.get('vendor_name'),
                             po_number=(typed.get('po_number') or None) and typed['po_number'].strip().upper()[:64],
                             department=typed.get('department'), amount=typed.get('amount'),
                             currency=typed.get('currency'), invoice_date=typed.get('invoice_date'),
                             bank_account_on_invoice=typed.get('bank_account_on_invoice'), source_kind=kind,
                             source_ref=(ref or None) and ref[:255], source_digest=source_digest,
                             extraction=typed.get('extraction'), status='pending', reasons=[])
            self.db.add(row)
            known[source_digest] = row.id
            out.append(row)
        await self.db.flush()
        audit(self.db, self.p, 'finance.invoices_ingested', target=str(batch.id), details={'count': len(items)})
        return out

    async def add_invoices_from_documents(self, batch_id, *, document_ids=None, folder=None):
        """Invoices from documents already indexed in Knowledge that this person can read (documents.read + ACL).
        `invoice.ingest` is checked before any document is read."""
        from app.finance.documents import facts_from_documents
        await self.batch(batch_id, 'invoice.ingest')
        items, skipped = await facts_from_documents(self.db, self.user, document_ids=document_ids, folder=folder,
                                                    date_order=self.settings.rag_date_order)
        rows = await self.add_invoices(batch_id, items) if items else []
        audit(self.db, self.p, 'finance.invoices_from_documents', target=str(batch_id),
              details={'documents': len(items), 'skipped': len(skipped), 'folder': bool(folder)})
        return rows, skipped

    async def knowledge_documents(self, *, folder=None, query=None) -> dict:
        """The document/folder picker: only documents and folders this person can read now."""
        from app.finance.documents import folder_of, readable_documents
        if not await self.readable_groups('invoice.ingest'):
            from app.core.errors import PermissionDeniedError
            raise PermissionDeniedError('You cannot add invoices in any department.')
        docs = await readable_documents(self.db, self.user, folder=folder, query=query)
        folders = sorted({f for f in (folder_of(d) for d in docs) if f})
        return {'documents': [{'id': str(d.id), 'filename': d.filename, 'folder': folder_of(d),
                               'indexed': d.indexed_version == d.version,
                               'updated_at': d.updated_at.isoformat() if getattr(d, 'updated_at', None) else None}
                              for d in docs], 'folders': folders}

    async def connector_status(self) -> dict:
        from app.finance.connectors import connector_status
        from app.platform.side_effects import org_scope_id
        await require(self.db, self.p, 'platform.manage', await org_scope_id(self.db))
        return await connector_status(self.db, self.settings)

    async def set_connector(self, mode: str | None) -> dict:
        """Administrator choice of payment connector (mock / http / http+mock); None = use the environment.
        The bank URL and token stay in the server environment and are never stored or returned here."""
        from app.finance.connectors import MODES, connector_status, get_connector
        from app.platform.models import OrgSettings
        from app.platform.side_effects import org_scope_id
        await require(self.db, self.p, 'platform.manage', await org_scope_id(self.db))
        if mode is not None and mode not in MODES:
            raise InvalidInputError('Choose mock, http or http+mock.')
        if mode is not None:
            from app.finance.connectors import ConnectorUnavailable
            try:
                get_connector(self.settings, mode=mode)
            except ConnectorUnavailable as exc:
                raise ConflictError(f'That connector cannot be used here: {exc}') from None
        row = await self.db.get(OrgSettings, 1, with_for_update=True)
        if row is None:
            raise ConflictError('Organisation settings are not initialised.')
        previous, row.finance_payment_connector = row.finance_payment_connector, mode
        await self.db.flush()
        audit(self.db, self.p, 'finance.connector_changed', target='org_settings',
              details={'from': previous, 'to': mode})
        return await connector_status(self.db, self.settings)

    async def start(self, batch_id) -> tuple[FinInvoiceBatch, WorkflowRun]:
        """Start (or re-start after corrections) the unified workflow for this batch."""
        from app.workflows.service import WorkflowService
        from app.workflows.unified import ensure_system_definition
        batch = await self.batch(batch_id, 'invoice.ingest', lock=True)
        if await self._active_run(batch) is not None:
            raise ConflictError('This batch is already being processed.')
        pending = await self.db.scalar(select(func.count()).select_from(FinInvoice).where(
            FinInvoice.batch_id == batch.id, FinInvoice.status == 'pending'))
        if not pending:
            raise ConflictError('There are no invoices waiting for verification in this batch.')
        definition = await ensure_system_definition(
            self.db, scope_id=batch.group_id, owner_id=self.user.id, origin_ref=TEMPLATE_REF, name=TEMPLATE_NAME,
            description=TEMPLATE_DESCRIPTION, spec=invoice_spec())
        if not definition.enabled:
            raise ConflictError('The invoice workflow is disabled in this department.')
        batch.round += 1
        service = WorkflowService(self.db, self.user, self.cipher)
        run = await service.trigger(batch.group_id, definition.id, {'batch_id': str(batch.id), 'round': batch.round},
                                    f'invoice-batch:{batch.id}:round:{batch.round}', origin='system',
                                    origin_ref=str(batch.id))
        batch.wf_run_id, batch.status, batch.updated_at = run.id, 'verifying', datetime.now(UTC)
        scope = await self.platform_scope(batch.group_id)
        await publish(self.db, 'finance.batch_started', scope,
                      {'batch_id': str(batch.id), 'run_id': str(run.id), 'round': batch.round}, self.p.trace_id)
        audit(self.db, self.p, 'finance.batch_started', target=str(batch.id),
              details={'run_id': str(run.id), 'round': batch.round, 'pending': pending})
        return batch, run

    async def invoices(self, batch_id, status=None) -> list[FinInvoice]:
        batch = await self.batch(batch_id)
        query = select(FinInvoice).where(FinInvoice.batch_id == batch.id, FinInvoice.group_id == batch.group_id)
        if status:
            query = query.where(FinInvoice.status == status)
        return list((await self.db.scalars(query.order_by(FinInvoice.seq))).all())

    async def summary(self, batch: FinInvoiceBatch) -> dict:
        rows = dict((await self.db.execute(select(FinInvoice.status, func.count()).where(
            FinInvoice.batch_id == batch.id).group_by(FinInvoice.status))).all())
        total = sum(rows.values())
        verified = rows.get('verified', 0) + rows.get('approved', 0) + rows.get('paid', 0)
        remaining = list((await self.db.scalars(select(FinInvoice.id).where(
            FinInvoice.batch_id == batch.id, FinInvoice.status.in_(('pending', 'exception'))).order_by(FinInvoice.seq))).all())
        run = await self.db.get(WorkflowRun, batch.wf_run_id) if batch.wf_run_id else None
        steps = []
        if run is not None:
            steps = [{'id': str(s.id), 'node_id': s.node_id, 'status': s.status, 'phase': s.phase,
                      'acting_user_id': str(s.acting_user_id) if s.acting_user_id else None,
                      'verified_at': s.verified_at.isoformat() if s.verified_at else None}
                     for s in (await self.db.scalars(select(WorkflowStep).where(WorkflowStep.run_id == run.id))).all()]
        return {'total': total, 'verified': verified, 'by_status': rows,
                'headline': f'{verified} of {total} verified',
                'remaining_ids': [str(i) for i in remaining],
                'run': None if run is None else {'id': str(run.id), 'status': run.status, 'phase': run.phase,
                                                 'blocked_reason': run.blocked_reason,
                                                 'last_verified_node': run.last_verified_node,
                                                 'failure_code': run.failure_code, 'steps': steps}}

    # ------------------------------------------------------------------ exceptions

    async def exceptions(self, group_id=None) -> list[FinInvoice]:
        groups = await self.readable_groups()
        if group_id is not None:
            if group_id not in groups:
                await self.require(group_id, 'finance.read')
            groups = [group_id]
        if not groups:
            return []
        return list((await self.db.scalars(select(FinInvoice).where(
            FinInvoice.group_id.in_(groups), FinInvoice.status == 'exception')
            .order_by(FinInvoice.updated_at.desc()).limit(1000))).all())

    async def resolve(self, invoice_id, action: str, *, fields: dict | None = None, note: str = '') -> FinInvoice:
        row = await self.db.scalar(select(FinInvoice).where(FinInvoice.id == invoice_id).with_for_update())
        if row is None:
            raise NotFoundError('Invoice not found.')
        # Correcting data is an ingestion duty; rejecting an invoice is a verification decision.
        await self.require(row.group_id, 'invoice.ingest' if action == 'correct' else 'invoice.verify')
        if row.status != 'exception':
            raise ConflictError('Only invoices in the exceptions queue can be resolved.')
        if action == 'reject':
            row.status = 'rejected'
        elif action == 'correct':
            facts = extraction.coerce(extraction.facts_from_json(fields or {}))
            for key, value in facts.items():
                if key == 'extraction' or (fields or {}).get(key) is None:
                    continue
                if key == 'po_number':
                    value = value.strip().upper()[:64]
                setattr(row, key, value)
            row.status, row.reasons, row.revision, row.payment_id = 'pending', [], row.revision + 1, None
            row.verified_at, row.verified_by = None, None
        else:
            raise InvalidInputError('Resolve with correct or reject.')
        row.resolution_note = note.strip()[:500] or None
        row.updated_at = datetime.now(UTC)
        audit(self.db, self.p, f'finance.invoice_{"corrected" if action == "correct" else "rejected"}', target=str(row.id),
              details={'batch_id': str(row.batch_id), 'revision': row.revision})
        return row

    # ------------------------------------------------------------------ approvals and payments

    async def approvals(self) -> list[dict]:
        flt = await visible_scopes(self.db, self.p, 'payment.approve')
        rows = (await self.db.execute(
            select(PlatformApprovalRequest, FinPayment, FinVendor.name, FinInvoiceBatch.name)
            .join(FinPayment, FinPayment.approval_id == PlatformApprovalRequest.id)
            .join(FinVendor, FinVendor.id == FinPayment.vendor_id)
            .join(FinInvoiceBatch, FinInvoiceBatch.id == FinPayment.batch_id)
            .where(PlatformApprovalRequest.status == 'pending', PlatformApprovalRequest.action_type == 'funds.transfer',
                   flt.sql(PlatformApprovalRequest.scope_id))
            .order_by(PlatformApprovalRequest.created_at))).all()
        out = []
        for approval, payment, vendor, batch in rows:
            numbers = list((await self.db.scalars(select(FinInvoice.invoice_number).where(
                FinInvoice.payment_id == payment.id).order_by(FinInvoice.seq))).all())
            out.append({'approval_id': str(approval.id), 'action_hash': approval.action_hash, 'action': approval.action,
                        'amount': money(approval.amount), 'currency': approval.currency,
                        'expires_at': approval.expires_at.isoformat(), 'payment_id': str(payment.id),
                        'batch_id': str(payment.batch_id), 'batch_name': batch, 'vendor_name': vendor,
                        'payee_account': payment.payee_account, 'invoice_numbers': numbers,
                        'requested_by': str(approval.requested_by),
                        'own_request': approval.requested_by == self.user.id})
        return out

    async def decide(self, approval_id, decision: str, *, presented_hash: str, note: str = ''):
        payment = await self.db.scalar(select(FinPayment).where(FinPayment.approval_id == approval_id))
        if payment is None:
            raise NotFoundError('Approval not found.')
        await self.require(payment.group_id, 'payment.approve')
        row = await decide_approval(self.db, self.p, approval_id, decision, presented_hash=presented_hash, note=note)
        batch = await self.db.get(FinInvoiceBatch, payment.batch_id)
        if batch is not None and batch.wf_run_id is not None:  # wake the waiting step now instead of at the next poll
            now = datetime.now(UTC)
            await self.db.execute(update(WorkflowStep).where(WorkflowStep.run_id == batch.wf_run_id,
                                                             WorkflowStep.status == 'waiting').values(available_at=now))
            await self.db.execute(update(WorkflowRun).where(WorkflowRun.id == batch.wf_run_id).values(wake_at=now))
        return row

    async def payments(self, batch_id) -> list[dict]:
        batch = await self.batch(batch_id)
        rows = (await self.db.execute(select(FinPayment, FinVendor.name).join(FinVendor, FinVendor.id == FinPayment.vendor_id)
                                      .where(FinPayment.batch_id == batch.id).order_by(FinPayment.created_at))).all()
        return [payment_out(p, vendor) for p, vendor in rows]

    # ------------------------------------------------------------------ spend chart

    async def spend(self, *, months: int = 12, group_id=None) -> dict:
        groups = await self.readable_groups()
        if group_id is not None:
            if group_id not in groups:
                await self.require(group_id, 'finance.read')
            groups = [group_id]
        today = await org_today(self.db)
        months = max(1, min(months, 36))
        first = date(today.year, today.month, 1)
        for _ in range(months - 1):
            first = date(first.year - 1, 12, 1) if first.month == 1 else date(first.year, first.month - 1, 1)
        if not groups:
            return {'months': [], 'departments': [], 'rows': [], 'currencies': [], 'from': first.isoformat()}
        department = func.coalesce(FinInvoice.department, AccessGroup.name)
        month = func.to_char(FinInvoice.invoice_date, 'YYYY-MM')
        rows = (await self.db.execute(
            select(month.label('month'), department.label('department'), FinInvoice.currency,
                   func.sum(FinInvoice.amount), func.count())
            .join(AccessGroup, AccessGroup.id == FinInvoice.group_id)
            .where(FinInvoice.group_id.in_(groups), FinInvoice.status.in_(SPEND_STATUSES),
                   FinInvoice.invoice_date >= first, FinInvoice.invoice_date <= today)
            .group_by(month, department, FinInvoice.currency).order_by(month, department))).all()
        labels, cursor = [], first
        while cursor <= today:
            labels.append(cursor.strftime('%Y-%m'))
            cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)
        return {'months': labels, 'from': first.isoformat(), 'statuses': list(SPEND_STATUSES),
                'departments': sorted({r[1] for r in rows}), 'currencies': sorted({r[2] for r in rows if r[2]}),
                'rows': [{'month': r[0], 'department': r[1], 'currency': r[2], 'amount': money(r[3]), 'count': r[4]}
                         for r in rows]}

    async def status_counts(self) -> dict:
        """Invoice counts by status across the groups the caller may read.

        Step AF (control room): the live "invoices by status" chart. Uses the
        same `finance.read` filter as `spend()` — a caller with no readable
        group sees an empty result, never another group's invoices.
        """
        groups = await self.readable_groups()
        if not groups:
            return {'total': 0, 'by_status': {}}
        rows = dict((await self.db.execute(select(FinInvoice.status, func.count()).where(
            FinInvoice.group_id.in_(groups)).group_by(FinInvoice.status))).all())
        return {'total': sum(rows.values()), 'by_status': rows}

    # ------------------------------------------------------------------ finance roles (platform memberships)

    async def roles(self, group_id) -> list[dict]:
        scope = await self.require(group_id, 'finance.read')
        rows = (await self.db.execute(select(PlatformMembership, PlatformRole.key, User.email)
                                      .join(PlatformRole, PlatformRole.id == PlatformMembership.role_id)
                                      .join(User, User.id == PlatformMembership.user_id)
                                      .where(PlatformMembership.scope_id == scope, PlatformMembership.revoked_at.is_(None),
                                             PlatformRole.key.in_(FINANCE_ROLES))
                                      .order_by(User.email))).all()
        return [{'id': str(m.id), 'user_id': str(m.user_id), 'email': email, 'role': key,
                 'valid_to': m.valid_to.isoformat() if m.valid_to else None} for m, key, email in rows]

    async def grant_role(self, group_id, *, user_id, role: str, valid_to=None):
        if role not in FINANCE_ROLES:
            raise InvalidInputError('Unknown finance role.')
        scope = await self.platform_scope(group_id)
        person = await self.db.get(User, user_id)
        if person is None or not person.is_active:
            raise InvalidInputError('Choose an active person.')
        await require(self.db, self.p, 'platform.manage', scope)
        await ensure_role(self.db, role, ROLE_PERMISSIONS[role], name=role.replace('.', ' ').title(), system=True)
        row = await grant_membership(self.db, self.p, user_id=user_id, scope_id=scope, role_key=role, valid_to=valid_to)
        audit(self.db, self.p, 'finance.role_granted', target=str(row.id),
              details={'group_id': str(group_id), 'user_id': str(user_id), 'role': role,
                       'valid_to': valid_to.isoformat() if valid_to else None})
        return row

    async def revoke_role(self, group_id, membership_id):
        scope = await self.require(group_id, 'platform.manage')
        row = await self.db.scalar(select(PlatformMembership).join(PlatformRole, PlatformRole.id == PlatformMembership.role_id)
                                   .where(PlatformMembership.id == membership_id, PlatformMembership.scope_id == scope,
                                          PlatformRole.key.in_(FINANCE_ROLES)).with_for_update())
        if row is None:
            raise NotFoundError('Role grant not found.')
        if row.revoked_at is None:
            row.revoked_at, row.revoke_reason = datetime.now(UTC), 'platform:finance_role_revoked'
            audit(self.db, self.p, 'finance.role_revoked', target=str(row.id), details={'group_id': str(group_id)})
        return row


def invoice_out(row: FinInvoice) -> dict:
    return {'id': str(row.id), 'batch_id': str(row.batch_id), 'group_id': str(row.group_id), 'seq': row.seq,
            'revision': row.revision, 'invoice_number': row.invoice_number, 'vendor_name': row.vendor_name,
            'vendor_id': str(row.vendor_id) if row.vendor_id else None, 'po_number': row.po_number,
            'department': row.department, 'amount': money(row.amount), 'currency': row.currency,
            'invoice_date': row.invoice_date.isoformat() if row.invoice_date else None,
            'status': row.status, 'reasons': row.reasons or [], 'source_kind': row.source_kind,
            'source_ref': row.source_ref, 'payment_id': str(row.payment_id) if row.payment_id else None,
            'verified_at': row.verified_at.isoformat() if row.verified_at else None,
            'resolution_note': row.resolution_note,
            'extraction_status': (row.extraction or {}).get('status')}


def payment_out(row: FinPayment, vendor_name: str | None = None) -> dict:
    return {'id': str(row.id), 'batch_id': str(row.batch_id), 'round': row.round, 'vendor_id': str(row.vendor_id),
            'vendor_name': vendor_name, 'amount': money(row.amount), 'currency': row.currency,
            'payee_account': row.payee_account, 'invoice_ids': row.invoice_ids, 'status': row.status,
            'approval_id': str(row.approval_id) if row.approval_id else None, 'action_hash': row.action_hash,
            'execution_id': str(row.execution_id) if row.execution_id else None, 'connector': row.connector,
            'external_ref': row.external_ref, 'failure_code': row.failure_code,
            'verified_at': row.verified_at.isoformat() if row.verified_at else None}


def batch_out(row: FinInvoiceBatch) -> dict:
    return {'id': str(row.id), 'group_id': str(row.group_id), 'name': row.name, 'status': row.status,
            'currency': row.currency, 'tolerance_pct': money(row.tolerance_pct), 'tolerance_abs': money(row.tolerance_abs),
            'round': row.round, 'wf_run_id': str(row.wf_run_id) if row.wf_run_id else None,
            'created_at': row.created_at.isoformat() if row.created_at else None}

