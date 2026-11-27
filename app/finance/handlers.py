"""Workflow action handlers for the invoice flow (registered on the unified runtime).

* finance.verify_invoices (person with invoice.verify): every invoice is checked through `execute_once`
  (duty invoice.verify, subject = the batch) so the duty is recorded for separation of duties, the decision is
  written exactly once per invoice revision and read back. Slices are checkpointed; pause, revoke or a crash
  stop before the next invoice and resume with the invoices still pending (by id).
* finance.request_payment_approvals (auto, as the requester): groups verified invoices per vendor into
  payments and requests a platform approval of the *exact* transfer (amount, payee account from the vendor
  master, invoice set, batch) with expiry and single use. Re-requests on expiry; rejection returns invoices to
  the exceptions queue.
* finance.execute_payments (person with funds.transfer): `execute_once` consumes the approval (hash must still
  match the payment recomputed from the database), submits through the payment connector with an idempotency
  key, and only marks the payment paid after reading the transfer back from the connector.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select, update

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.db.finance_models import FinGoodsReceipt, FinInvoice, FinInvoiceBatch, FinPayment, FinPurchaseOrder, FinVendor
from app.finance.connectors import ConnectorUnavailable, TransferRequest, get_connector
from app.finance.verification import InvoiceFacts, PurchaseOrder, Reference, Vendor, invoice_key, name_key, verify_invoice
from app.platform.approvals import ActionSpec, request_approval
from app.platform.events import audit, publish
from app.platform.execution import VerifiedResult, execute_once
from app.platform.models import PlatformActionExecution, PlatformApprovalRequest
from app.platform.timezone import org_today
from app.workflows.actions import ActionContext, ActionOutcome, register

BILLED = ('verified', 'approved', 'paid')
ABANDONED_CLAIM = timedelta(minutes=5)


def batch_subject(batch_id) -> str:
    return f'invoice_batch:{batch_id}'


def _now():
    return datetime.now(UTC)


async def _batch(ctx: ActionContext) -> tuple[FinInvoiceBatch, int]:
    try:
        batch_id = uuid.UUID(str(ctx.values.get('batch_id')))
        round_ = int(ctx.values.get('round'))
    except (TypeError, ValueError):
        raise NotFoundError('Invoice batch not found.') from None
    # Department scope: the run's department must own the batch.
    batch = await ctx.db.scalar(select(FinInvoiceBatch).where(FinInvoiceBatch.id == batch_id,
                                                              FinInvoiceBatch.group_id == ctx.group_id)
                                .execution_options(populate_existing=True))
    if batch is None:
        raise NotFoundError('Invoice batch not found.')
    return batch, round_


def _denied(exc: PermissionDeniedError) -> ActionOutcome:
    code = 'separation_of_duties' if 'Separation of duties' in str(exc) else 'actor_not_permitted'
    return ActionOutcome('failed', {'message': str(exc)[:200]}, code, retryable=False)


async def _stale_claim(db, row: PlatformActionExecution) -> bool:
    if row.status == 'claimed' and row.created_at <= _now() - ABANDONED_CLAIM:
        row.status, row.failure_code = 'failed', 'abandoned_claim'
        await db.commit()
        return True
    return False


# ------------------------------------------------------------------------------------------ verification

async def reference_for(db, batch: FinInvoiceBatch, settings) -> Reference:
    vendors = {v.name_key: Vendor(v.id, v.name, v.status, v.bank_account, v.currency)
               for v in (await db.scalars(select(FinVendor).where(FinVendor.group_id == batch.group_id))).all()}
    received = dict((await db.execute(select(FinGoodsReceipt.po_id, func.sum(FinGoodsReceipt.amount))
                                      .where(FinGoodsReceipt.group_id == batch.group_id)
                                      .group_by(FinGoodsReceipt.po_id))).all())
    billed = dict((await db.execute(select(FinInvoice.po_number, func.sum(FinInvoice.amount))
                                    .where(FinInvoice.group_id == batch.group_id, FinInvoice.status.in_(BILLED),
                                           FinInvoice.po_number.is_not(None))
                                    .group_by(FinInvoice.po_number))).all())
    pos = {po.po_number: PurchaseOrder(po.id, po.po_number, po.vendor_id, po.amount, po.currency, po.status,
                                       Decimal(received.get(po.id) or 0), Decimal(billed.get(po.po_number) or 0))
           for po in (await db.scalars(select(FinPurchaseOrder).where(FinPurchaseOrder.group_id == batch.group_id))).all()}
    existing = set()
    for vendor_id, number in (await db.execute(select(FinInvoice.vendor_id, FinInvoice.invoice_number).where(
            FinInvoice.group_id == batch.group_id, FinInvoice.vendor_id.is_not(None),
            or_(FinInvoice.status.in_(BILLED),
                (FinInvoice.batch_id == batch.id) & (FinInvoice.status == 'exception'))))).all():
        existing.add((vendor_id, invoice_key(number)))
    return Reference(vendors=vendors, purchase_orders=pos, existing_invoices=existing, today=await org_today(db),
                     tolerance_pct=Decimal(batch.tolerance_pct), tolerance_abs=Decimal(batch.tolerance_abs),
                     max_age_days=settings.finance_max_invoice_age_days)


async def _verify_key(db, invoice: FinInvoice) -> str | None:
    """`invoice.verify:<id>:r<revision>`; a claim abandoned by a crash before its write is retired (it never
    changed the invoice: the decision and the 'dispatched' mark commit together) and a new key is used."""
    base = f'invoice.verify:{invoice.id}:r{invoice.revision}'
    rows = list((await db.scalars(select(PlatformActionExecution).where(or_(
        PlatformActionExecution.idempotency_key == base,
        PlatformActionExecution.idempotency_key.like(base + ':%'))))).all())
    for row in rows:
        if row.status == 'claimed' and not await _stale_claim(db, row):
            return None  # someone else is writing this invoice right now
    return base if not rows else f'{base}:{len(rows)}'


async def _verify_one(ctx: ActionContext, batch: FinInvoiceBatch, inv: FinInvoice, ref: Reference) -> str | None:
    facts = InvoiceFacts(inv.id, inv.invoice_number, inv.vendor_name, inv.po_number, inv.amount, inv.currency,
                         inv.invoice_date, inv.bank_account_on_invoice)
    result = verify_invoice(facts, ref)
    outcome = 'verified' if result.verified else 'exception'
    codes = [r['code'] for r in result.reasons]
    inv_id, revision = inv.id, inv.revision
    spec = ActionSpec('invoice.verify', ctx.platform_scope_id,
                      {'invoice_id': str(inv_id), 'revision': revision, 'source_digest': inv.source_digest,
                       'outcome': outcome, 'reasons': codes}, subject=batch_subject(batch.id))
    key = await _verify_key(ctx.db, inv)
    if key is None:
        return None

    async def dispatch():
        row = await ctx.db.scalar(select(FinInvoice).where(FinInvoice.id == inv_id).with_for_update()
                                  .execution_options(populate_existing=True))
        if row.status != 'pending' or row.revision != revision:
            raise ConflictError('Invoice changed during verification.')
        row.status, row.reasons, row.vendor_id = outcome, result.reasons, result.vendor_id
        row.verified_by, row.verified_at, row.updated_at = ctx.principal.user_id, _now(), _now()
        return f'invoice:{inv_id}:r{revision}'

    async def verify(_ref):
        status, rev = (await ctx.db.execute(select(FinInvoice.status, FinInvoice.revision).where(FinInvoice.id == inv_id)
                                            .execution_options(populate_existing=True))).one()
        accepted = BILLED if outcome == 'verified' else ('exception', 'rejected', 'pending')
        ok = rev == revision and status in accepted
        return VerifiedResult(ok, {'invoice_id': str(inv_id), 'status': status, 'reasons': codes})

    result_ = await execute_once(ctx.db, ctx.principal, key, spec, duty='invoice.verify', dispatch=dispatch, verify=verify)
    if result_.status == 'verified':
        ctx.note('invoice.checked', invoice_id=str(inv_id), outcome=outcome, reasons=codes)
        return outcome
    return None


class VerifyInvoicesHandler:
    name = 'finance.verify_invoices'
    label = 'Verify invoices against PO, receipt, vendor master and duplicates'
    module = 'finance'
    permission = 'invoice.verify'

    async def run(self, ctx: ActionContext) -> ActionOutcome:
        settings = get_settings()
        batch, round_ = await _batch(ctx)
        pending = list((await ctx.db.scalars(select(FinInvoice).where(
            FinInvoice.batch_id == batch.id, FinInvoice.group_id == batch.group_id, FinInvoice.status == 'pending')
            .order_by(FinInvoice.seq).limit(settings.finance_verify_slice))).all())
        if not pending:
            counts = await _counts(ctx.db, batch)
            ctx.checkpoint.update(round=round_, complete=True)
            return ActionOutcome('done', {**counts, 'round': round_})
        if batch.status != 'verifying':
            batch.status = 'verifying'
            await ctx.db.commit()
        ref = await reference_for(ctx.db, batch, settings)
        checked = verified = 0
        for index, inv in enumerate(pending):
            if index:
                stop = await ctx.should_stop()
                if stop:
                    ctx.checkpoint['stopped'] = stop
                    break
            try:
                outcome = await _verify_one(ctx, batch, inv, ref)
            except PermissionDeniedError as exc:
                await ctx.db.rollback()
                return _denied(exc)
            except ConflictError:
                await ctx.db.rollback()
                continue
            if outcome is not None:
                checked += 1
                verified += outcome == 'verified'
        ctx.checkpoint['checked'] = int(ctx.checkpoint.get('checked', 0)) + checked
        ctx.checkpoint['verified'] = int(ctx.checkpoint.get('verified', 0)) + verified
        ctx.checkpoint['round'] = round_
        return ActionOutcome('more', {'checked': checked})

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult:
        batch, _ = await _batch(ctx)
        counts = await _counts(ctx.db, batch)
        ok = counts['pending'] == 0 and counts['total'] == counts['verified'] + counts['exceptions'] + counts['rejected']
        return VerifiedResult(ok, counts)


async def _counts(db, batch) -> dict:
    rows = dict((await db.execute(select(FinInvoice.status, func.count()).where(FinInvoice.batch_id == batch.id)
                                  .group_by(FinInvoice.status))).all())
    return {'total': sum(rows.values()), 'verified': sum(rows.get(s, 0) for s in BILLED),
            'exceptions': rows.get('exception', 0), 'rejected': rows.get('rejected', 0), 'pending': rows.get('pending', 0)}


# ------------------------------------------------------------------------------------------ approval

async def payment_invoices(db, payment: FinPayment) -> list[FinInvoice]:
    return list((await db.scalars(select(FinInvoice).where(FinInvoice.payment_id == payment.id,
                                                           FinInvoice.group_id == payment.group_id)
                                  .order_by(FinInvoice.seq).execution_options(populate_existing=True))).all())


def payment_spec(scope_id, batch, payment, vendor, invoices) -> ActionSpec:
    """The exact transfer. Recomputed from the database every time (never from document text): the payee account
    comes from the vendor master, the amount is the sum of the invoices bound to the payment."""
    amount = sum((Decimal(i.amount) for i in invoices), Decimal('0.00')).quantize(Decimal('0.01'))
    return ActionSpec('funds.transfer', scope_id, {
        'batch_id': str(batch.id), 'payment_id': str(payment.id), 'payee_vendor_id': str(vendor.id),
        'payee_name': vendor.name, 'payee_account': vendor.bank_account,
        'invoice_ids': sorted(str(i.id) for i in invoices)}, amount=amount, currency=payment.currency,
        subject=batch_subject(batch.id))


async def _return_to_exceptions(db, payment: FinPayment, code: str, message: str) -> None:
    await db.execute(update(FinInvoice).where(FinInvoice.payment_id == payment.id).values(
        status='exception', payment_id=None, updated_at=_now(),
        reasons=[{'code': code, 'message': message}]))


class RequestPaymentApprovalsHandler:
    name = 'finance.request_payment_approvals'
    label = 'Request approval of the exact payments'
    module = 'finance'
    permission = None

    async def _group(self, ctx, batch, round_):
        rows = list((await ctx.db.scalars(select(FinInvoice).where(
            FinInvoice.batch_id == batch.id, FinInvoice.status == 'verified', FinInvoice.payment_id.is_(None),
            FinInvoice.vendor_id.is_not(None)).order_by(FinInvoice.seq).with_for_update())).all())
        groups: dict[tuple, list[FinInvoice]] = {}
        for inv in rows:
            groups.setdefault((inv.vendor_id, inv.currency), []).append(inv)
        for (vendor_id, currency), invoices in groups.items():
            vendor = await ctx.db.get(FinVendor, vendor_id)
            amount = sum((Decimal(i.amount) for i in invoices), Decimal('0.00'))
            payment = FinPayment(id=uuid.uuid4(), group_id=batch.group_id, batch_id=batch.id, round=round_,
                                 vendor_id=vendor_id, amount=amount, currency=currency, payee_account=vendor.bank_account,
                                 invoice_ids=[str(i.id) for i in invoices], status='awaiting_approval')
            ctx.db.add(payment)
            await ctx.db.flush()
            for inv in invoices:
                inv.payment_id = payment.id
        batch.status = 'awaiting_approval'
        await ctx.db.commit()
        ctx.checkpoint['grouped'] = True

    async def run(self, ctx: ActionContext) -> ActionOutcome:
        settings = get_settings()
        batch, round_ = await _batch(ctx)
        if not ctx.checkpoint.get('grouped'):
            await self._group(ctx, batch, round_)
        payments = list((await ctx.db.scalars(select(FinPayment).where(
            FinPayment.batch_id == batch.id, FinPayment.round == round_).order_by(FinPayment.created_at))).all())
        awaiting = 0
        for pay in payments:
            if pay.status != 'awaiting_approval':
                continue
            vendor = await ctx.db.get(FinVendor, pay.vendor_id, populate_existing=True)
            invoices = await payment_invoices(ctx.db, pay)
            if not invoices:
                pay.status, pay.failure_code = 'rejected', 'no_invoices'
                continue
            spec = payment_spec(ctx.platform_scope_id, batch, pay, vendor, invoices)
            approval = (await ctx.db.get(PlatformApprovalRequest, pay.approval_id, populate_existing=True)
                        if pay.approval_id else None)
            stale = approval is not None and approval.status in {'approved', 'pending'} and approval.action_hash != spec.hash()
            if approval is not None and approval.status == 'pending' and (approval.expires_at <= _now() or stale):
                approval.status = 'expired' if not stale else 'cancelled'
            if approval is None or stale or approval.status in {'expired', 'cancelled'}:
                pay.amount, pay.payee_account = spec.amount, spec.params['payee_account']
                row = await request_approval(ctx.db, ctx.principal, spec, required_permission='payment.approve',
                                             ttl=timedelta(hours=settings.finance_approval_ttl_hours))
                pay = await ctx.db.get(FinPayment, pay.id)
                pay.approval_id, pay.action_hash, pay.updated_at = row.id, row.action_hash, _now()
                ctx.note('payment.approval_requested', payment_id=str(pay.id), approval_id=str(row.id),
                         action_hash=row.action_hash)
                awaiting += 1
            elif approval.status == 'pending':
                awaiting += 1
            elif approval.status in {'approved', 'consumed'}:
                pay.status, pay.updated_at = 'approved', _now()
                for inv in invoices:
                    inv.status, inv.updated_at = 'approved', _now()
                ctx.note('payment.approved', payment_id=str(pay.id), approval_id=str(approval.id),
                         approved_by=str(approval.decided_by))
            elif approval.status == 'rejected':
                pay.status, pay.updated_at = 'rejected', _now()
                await _return_to_exceptions(ctx.db, pay, 'payment_rejected', 'Payment approval was rejected')
                ctx.note('payment.rejected', payment_id=str(pay.id), approval_id=str(approval.id))
        await ctx.db.commit()
        if awaiting:
            return ActionOutcome('waiting', {'awaiting': awaiting}, poll_seconds=15)
        batch = await ctx.db.get(FinInvoiceBatch, batch.id, populate_existing=True)
        statuses = [p.status for p in payments]
        approved = sum(s == 'approved' for s in statuses)
        batch.status, batch.updated_at = ('paying' if approved else 'completed'), _now()
        await ctx.db.commit()
        return ActionOutcome('done', {'approved': approved, 'rejected': sum(s == 'rejected' for s in statuses),
                                      'payments': len(payments), 'round': round_})

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult:
        batch, round_ = await _batch(ctx)
        checked = []
        for pay in (await ctx.db.scalars(select(FinPayment).where(FinPayment.batch_id == batch.id, FinPayment.round == round_,
                                                                  FinPayment.status == 'approved'))).all():
            vendor = await ctx.db.get(FinVendor, pay.vendor_id, populate_existing=True)
            approval = await ctx.db.get(PlatformApprovalRequest, pay.approval_id, populate_existing=True)
            spec = payment_spec(ctx.platform_scope_id, batch, pay, vendor, await payment_invoices(ctx.db, pay))
            checked.append(approval is not None and approval.status in {'approved', 'consumed'}
                           and approval.action_hash == spec.hash() and approval.scope_id == ctx.platform_scope_id)
        return VerifiedResult(all(checked), {'approved_payments': len(checked)})


# ------------------------------------------------------------------------------------------ payment

class ExecutePaymentsHandler:
    name = 'finance.execute_payments'
    label = 'Transfer approved payments (read-back verified)'
    module = 'finance'
    permission = 'funds.transfer'

    async def _settle(self, ctx, batch, pay, res, connector_name):
        pay = await ctx.db.get(FinPayment, pay.id, populate_existing=True)
        pay.execution_id, pay.connector, pay.updated_at = res.execution_id, connector_name, _now()
        pay.external_ref = res.external_ref
        if res.status == 'verified':
            pay.status, pay.verified_at, pay.failure_code = 'verified', _now(), None
            await ctx.db.execute(update(FinInvoice).where(FinInvoice.payment_id == pay.id).values(status='paid', updated_at=_now()))
            await publish(ctx.db, 'finance.payment_verified', ctx.platform_scope_id,
                          {'payment_id': str(pay.id), 'batch_id': str(batch.id), 'external_ref': res.external_ref},
                          ctx.principal.trace_id)
            ctx.note('payment.verified', payment_id=str(pay.id), external_ref=(res.external_ref or '')[:80])
        elif res.status == 'dispatched':
            pay.status = 'dispatched'
            ctx.note('payment.awaiting_readback', payment_id=str(pay.id))
        elif res.status == 'failed':
            row = await ctx.db.get(PlatformActionExecution, res.execution_id, populate_existing=True)
            pay.status, pay.failure_code = 'failed', (row.failure_code if row else 'failed')
            await _return_to_exceptions(ctx.db, pay, 'payment_failed',
                                        f'Payment failed ({pay.failure_code}); a new approval is needed')
            await publish(ctx.db, 'finance.payment_failed', ctx.platform_scope_id,
                          {'payment_id': str(pay.id), 'batch_id': str(batch.id), 'code': pay.failure_code},
                          ctx.principal.trace_id)
            ctx.note('payment.failed', payment_id=str(pay.id), code=pay.failure_code)
        await ctx.db.commit()

    async def _reconcile(self, ctx, connector, key, execution_id) -> bool:
        """After an ambiguous submit, ask the connector whether it holds a transfer for this key."""
        try:
            found = await connector.find(key)
        except Exception:
            return False
        if found is None:
            return False
        row = await ctx.db.get(PlatformActionExecution, execution_id, populate_existing=True)
        row.status, row.external_ref, row.failure_code = 'dispatched', found.ref[:500], None
        row.dispatched_at = row.dispatched_at or _now()
        audit(ctx.db, ctx.principal, 'platform.action_reconciled', target=str(row.id),
              details={'action_type': row.action_type, 'external_ref': found.ref})
        await ctx.db.commit()
        return True

    async def _pay_one(self, ctx, batch, pay, connector):
        pay_id, batch_id = pay.id, batch.id
        vendor = await ctx.db.get(FinVendor, pay.vendor_id, populate_existing=True)
        invoices = await payment_invoices(ctx.db, pay)
        spec = payment_spec(ctx.platform_scope_id, batch, pay, vendor, invoices)
        key = f'payment:{pay.id}'
        req = TransferRequest(key, spec.amount, pay.currency, spec.params['payee_account'], vendor.name,
                              f'{batch.name[:60]} / {str(pay.id)[:8]}')

        async def dispatch():
            return await connector.submit(req)

        async def verify(ref):
            status = await connector.get(ref)
            ok = (status.status == 'completed' and status.amount == req.amount
                  and status.currency.upper() == req.currency.upper() and status.payee_account == req.payee_account)
            return VerifiedResult(ok, {'ref': ref, 'status': status.status, 'amount': format(status.amount, 'f'),
                                       'currency': status.currency, 'payee_matches': status.payee_account == req.payee_account})

        async def once():
            return await execute_once(ctx.db, ctx.principal, key, spec, duty='funds.transfer', dispatch=dispatch,
                                      verify=verify, approval_id=pay.approval_id)
        try:
            res = await once()
        except ConflictError as exc:  # approval used, expired, or it covers a different payment
            message = str(exc)[:200]
            await ctx.db.rollback()
            pay = await ctx.db.get(FinPayment, pay_id, populate_existing=True)
            pay.status, pay.failure_code, pay.updated_at = 'failed', 'approval_invalid', _now()
            await _return_to_exceptions(ctx.db, pay, 'approval_invalid', message)
            ctx.note('payment.blocked', payment_id=str(pay_id), code='approval_invalid')
            await ctx.db.commit()
            return
        row = await ctx.db.get(PlatformActionExecution, res.execution_id, populate_existing=True)
        if res.status == 'failed' and row is not None and (row.failure_code or '').startswith('dispatch_'):
            if await self._reconcile(ctx, connector, key, res.execution_id):
                res = await once()
        elif res.status == 'claimed' and row is not None:
            if await self._reconcile(ctx, connector, key, res.execution_id):
                res = await once()
            elif await _stale_claim(ctx.db, row):
                res = await once()
            else:
                return  # in progress elsewhere; look again on the next slice
        batch = await ctx.db.get(FinInvoiceBatch, batch_id)
        pay = await ctx.db.get(FinPayment, pay_id)
        await self._settle(ctx, batch, pay, res, connector.name)

    async def run(self, ctx: ActionContext) -> ActionOutcome:
        batch, round_ = await _batch(ctx)
        open_payments = list((await ctx.db.scalars(select(FinPayment).where(
            FinPayment.batch_id == batch.id, FinPayment.round == round_,
            FinPayment.status.in_(('approved', 'dispatched'))).order_by(FinPayment.created_at))).all())
        if not open_payments:
            batch.status, batch.updated_at = 'completed', _now()
            await ctx.db.commit()
            rows = dict((await ctx.db.execute(select(FinPayment.status, func.count()).where(
                FinPayment.batch_id == batch.id, FinPayment.round == round_).group_by(FinPayment.status))).all())
            return ActionOutcome('done', {'paid': rows.get('verified', 0), 'failed': rows.get('failed', 0), 'round': round_})
        if batch.status != 'paying':
            batch.status = 'paying'
            await ctx.db.commit()
        try:
            connector = get_connector()
        except ConnectorUnavailable:
            return ActionOutcome('failed', failure_code='connector_unavailable', retryable=True)
        for index, pay in enumerate(open_payments):
            if index:
                stop = await ctx.should_stop()
                if stop:
                    ctx.checkpoint['stopped'] = stop
                    return ActionOutcome('more', {})
            try:
                await self._pay_one(ctx, batch, pay, connector)
            except PermissionDeniedError as exc:
                await ctx.db.rollback()
                return _denied(exc)
        remaining = list((await ctx.db.scalars(select(FinPayment.status).where(
            FinPayment.batch_id == batch.id, FinPayment.round == round_,
            FinPayment.status.in_(('approved', 'dispatched'))))).all())
        if remaining and all(s == 'dispatched' for s in remaining):
            return ActionOutcome('waiting', {'awaiting_readback': len(remaining)}, poll_seconds=30)
        return ActionOutcome('more', {})

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult:
        batch, round_ = await _batch(ctx)
        connector = get_connector()
        paid = 0
        for pay in (await ctx.db.scalars(select(FinPayment).where(FinPayment.batch_id == batch.id, FinPayment.round == round_)
                                         .execution_options(populate_existing=True))).all():
            if pay.status in {'approved', 'dispatched', 'awaiting_approval'}:
                return VerifiedResult(False, {'open_payment': str(pay.id)})
            if pay.status == 'verified':
                status = await connector.get(pay.external_ref)
                if status.status != 'completed' or status.amount != pay.amount or status.payee_account != pay.payee_account:
                    return VerifiedResult(False, {'payment_id': str(pay.id), 'readback': status.status})
                paid += 1
        return VerifiedResult(True, {'paid': paid})


register(VerifyInvoicesHandler())
register(RequestPaymentApprovalsHandler())
register(ExecutePaymentsHandler())

__all__ = ['batch_subject', 'payment_spec', 'reference_for', 'name_key']
