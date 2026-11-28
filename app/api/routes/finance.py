"""Finance API (step E): vendor master, purchase orders, receipts, invoice batches (ingest, verify, results),
exceptions queue, payment approvals (exact action hash), payments and the spend chart.

Every route authorises on the department's platform scope: finance.read, finance.manage, invoice.ingest,
invoice.verify, payment.approve, funds.transfer. Invoice content is data only.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AppSettings, CurrentUser, DbSession
from app.core.errors import InvalidInputError
from app.finance import extraction
from app.finance.connectors import resolve_connector
from app.finance.service import (FinanceService, batch_out, cash_flow_csv, cash_flow_out, invoice_out, money,
                                 payment_out, tax_report_csv, tax_report_out)
from app.finance.verification import REASONS

router = APIRouter(prefix='/api/finance', tags=['finance'])

CURRENCY = r'^[A-Za-z]{3}$'


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class VendorIn(Strict):
    name: str = Field(min_length=1, max_length=200)
    bank_account: str = Field(min_length=4, max_length=64)
    currency: str = Field(pattern=CURRENCY)
    tax_id: str | None = Field(default=None, max_length=64)
    status: Literal['active', 'blocked'] = 'active'


class PurchaseOrderIn(Strict):
    po_number: str = Field(min_length=1, max_length=64)
    vendor_id: UUID
    amount: Decimal = Field(gt=0, max_digits=16, decimal_places=2)
    currency: str = Field(pattern=CURRENCY)
    status: Literal['open', 'closed', 'cancelled'] = 'open'


class ReceiptIn(Strict):
    po_id: UUID
    receipt_number: str = Field(min_length=1, max_length=64)
    amount: Decimal = Field(gt=0, max_digits=16, decimal_places=2)
    received_on: date


class BatchIn(Strict):
    name: str = Field(min_length=1, max_length=160)
    currency: str = Field(pattern=CURRENCY)
    tolerance_pct: Decimal = Field(default=Decimal('2'), ge=0, le=20, max_digits=5, decimal_places=2)
    tolerance_abs: Decimal = Field(default=Decimal('0'), ge=0, max_digits=16, decimal_places=2)


class InvoiceRow(Strict):
    invoice_number: str | None = Field(default=None, max_length=64)
    vendor_name: str | None = Field(default=None, max_length=200)
    po_number: str | None = Field(default=None, max_length=64)
    amount: Decimal | None = Field(default=None, max_digits=16, decimal_places=2)
    currency: str | None = Field(default=None, max_length=3)
    invoice_date: date | None = None
    department: str | None = Field(default=None, max_length=120)
    bank_account_on_invoice: str | None = Field(default=None, max_length=64)


class InvoicesIn(Strict):
    invoices: list[InvoiceRow] = Field(default_factory=list, max_length=1000)
    texts: list[str] = Field(default_factory=list, max_length=200)


class ResolveIn(Strict):
    action: Literal['correct', 'reject']
    fields: InvoiceRow | None = None
    note: str = Field(default='', max_length=500)


class DecisionIn(Strict):
    decision: Literal['approve', 'reject']
    presented_hash: str = Field(min_length=64, max_length=64, pattern=r'^[0-9a-f]{64}$')
    note: str = Field(default='', max_length=500)


class RoleIn(Strict):
    user_id: UUID
    role: Literal['finance.viewer', 'finance.clerk', 'finance.verifier', 'finance.approver', 'finance.treasury',
                  'finance.admin']
    valid_to: datetime | None = None


class FromDocumentsIn(Strict):
    document_ids: list[UUID] = Field(default_factory=list, max_length=200)
    folder: str | None = Field(default=None, max_length=1024)


class ConnectorIn(Strict):
    mode: Literal['mock', 'http', 'http+mock'] | None = None


def svc(request: Request, db, user, settings) -> FinanceService:
    return FinanceService(db, user, settings, request.app.state.cipher)


def vendor_out(v) -> dict:
    return {'id': str(v.id), 'name': v.name, 'bank_account': v.bank_account, 'currency': v.currency,
            'tax_id': v.tax_id, 'status': v.status}


@router.get('/scopes')
async def scopes(request: Request, db: DbSession, user: CurrentUser, settings: AppSettings):
    service = svc(request, db, user, settings)
    try:
        connector = (await resolve_connector(db, settings)).name
    except Exception:
        connector = 'unavailable'
    return {'scopes': await service.scopes(), 'reasons': REASONS, 'connector': connector}


@router.get('/scopes/{group_id}/vendors')
async def vendors(group_id: UUID, request: Request, db: DbSession, user: CurrentUser, settings: AppSettings):
    return {'vendors': [vendor_out(v) for v in await svc(request, db, user, settings).vendors(group_id)]}


@router.post('/scopes/{group_id}/vendors', status_code=201)
async def save_vendor(group_id: UUID, body: VendorIn, request: Request, db: DbSession, user: CurrentUser, settings: AppSettings):
    row = await svc(request, db, user, settings).save_vendor(group_id, **body.model_dump())
    await db.commit()
    return vendor_out(row)


@router.get('/scopes/{group_id}/purchase-orders')
async def purchase_orders(group_id: UUID, request: Request, db: DbSession, user: CurrentUser, settings: AppSettings):
    return {'purchase_orders': await svc(request, db, user, settings).purchase_orders(group_id)}


@router.post('/scopes/{group_id}/purchase-orders', status_code=201)
async def save_purchase_order(group_id: UUID, body: PurchaseOrderIn, request: Request, db: DbSession, user: CurrentUser,
                              settings: AppSettings):
    row = await svc(request, db, user, settings).save_purchase_order(group_id, **body.model_dump())
    await db.commit()
    return {'id': str(row.id), 'po_number': row.po_number, 'amount': money(row.amount), 'status': row.status}


@router.post('/scopes/{group_id}/receipts', status_code=201)
async def add_receipt(group_id: UUID, body: ReceiptIn, request: Request, db: DbSession, user: CurrentUser,
                      settings: AppSettings):
    row = await svc(request, db, user, settings).add_receipt(group_id, **body.model_dump())
    await db.commit()
    return {'id': str(row.id), 'po_id': str(row.po_id), 'amount': money(row.amount)}


@router.get('/scopes/{group_id}/roles')
async def roles(group_id: UUID, request: Request, db: DbSession, user: CurrentUser, settings: AppSettings):
    return {'roles': await svc(request, db, user, settings).roles(group_id)}


@router.post('/scopes/{group_id}/roles', status_code=201)
async def grant_role(group_id: UUID, body: RoleIn, request: Request, db: DbSession, user: CurrentUser, settings: AppSettings):
    row = await svc(request, db, user, settings).grant_role(group_id, user_id=body.user_id, role=body.role,
                                                            valid_to=body.valid_to)
    await db.commit()
    return {'id': str(row.id), 'role': body.role, 'user_id': str(body.user_id)}


@router.delete('/scopes/{group_id}/roles/{membership_id}', status_code=204)
async def revoke_role(group_id: UUID, membership_id: UUID, request: Request, db: DbSession, user: CurrentUser,
                      settings: AppSettings):
    await svc(request, db, user, settings).revoke_role(group_id, membership_id)
    await db.commit()


@router.get('/batches')
async def batches(request: Request, db: DbSession, user: CurrentUser, settings: AppSettings, group_id: UUID | None = None):
    service = svc(request, db, user, settings)
    out = []
    for batch in await service.batches(group_id):
        out.append({**batch_out(batch), 'summary': await service.summary(batch)})
    return {'batches': out}


@router.post('/scopes/{group_id}/batches', status_code=201)
async def create_batch(group_id: UUID, body: BatchIn, request: Request, db: DbSession, user: CurrentUser, settings: AppSettings):
    row = await svc(request, db, user, settings).create_batch(group_id, **body.model_dump())
    await db.commit()
    return batch_out(row)


@router.get('/batches/{batch_id}')
async def batch_detail(batch_id: UUID, request: Request, db: DbSession, user: CurrentUser, settings: AppSettings):
    service = svc(request, db, user, settings)
    batch = await service.batch(batch_id)
    invoices = await service.invoices(batch_id)
    return {'batch': batch_out(batch), 'summary': await service.summary(batch),
            'invoices': [invoice_out(i) for i in invoices], 'payments': await service.payments(batch_id)}


@router.post('/batches/{batch_id}/invoices', status_code=201)
async def add_invoices(batch_id: UUID, body: InvoicesIn, request: Request, db: DbSession, user: CurrentUser,
                       settings: AppSettings):
    if not body.invoices and not body.texts:
        raise InvalidInputError('Send invoice rows or invoice texts.')
    items = []
    for row in body.invoices:
        facts = extraction.facts_from_json(row.model_dump(mode='json'))
        items.append(('json', facts, None, extraction.digest_of(row.model_dump(mode='json'))))
    for index, text in enumerate(body.texts):
        facts = extraction.facts_from_text(text, date_order=settings.rag_date_order)
        items.append(('text', facts, f'text #{index + 1}', extraction.digest_of(text.encode('utf-8'))))
    rows = await svc(request, db, user, settings).add_invoices(batch_id, items)
    await db.commit()
    return {'invoices': [invoice_out(r) for r in rows]}


@router.get('/knowledge-documents')
async def knowledge_documents(request: Request, db: DbSession, user: CurrentUser, settings: AppSettings,
                              folder: str | None = Query(default=None, max_length=1024),
                              q: str | None = Query(default=None, max_length=120)):
    return await svc(request, db, user, settings).knowledge_documents(folder=folder, query=q)


@router.post('/batches/{batch_id}/invoices/from-documents', status_code=201)
async def add_invoices_from_documents(batch_id: UUID, body: FromDocumentsIn, request: Request, db: DbSession,
                                      user: CurrentUser, settings: AppSettings):
    if not body.document_ids and not body.folder:
        raise InvalidInputError('Choose documents or a folder.')
    rows, skipped = await svc(request, db, user, settings).add_invoices_from_documents(
        batch_id, document_ids=body.document_ids, folder=body.folder)
    await db.commit()
    return {'invoices': [invoice_out(r) for r in rows], 'skipped': skipped}


@router.get('/connector')
async def connector(request: Request, db: DbSession, user: CurrentUser, settings: AppSettings):
    return await svc(request, db, user, settings).connector_status()


@router.put('/connector')
async def set_connector(body: ConnectorIn, request: Request, db: DbSession, user: CurrentUser, settings: AppSettings):
    out = await svc(request, db, user, settings).set_connector(body.mode)
    await db.commit()
    return out


@router.post('/batches/{batch_id}/invoices/upload', status_code=201)
async def upload_invoice(batch_id: UUID, request: Request, db: DbSession, user: CurrentUser, settings: AppSettings,
                         file: UploadFile = File(...), note: str = Form(default='')):
    content = await file.read(10 * 1024 * 1024 + 1)
    name = (file.filename or 'invoice.txt')[:200]
    service = svc(request, db, user, settings)
    await service.batch(batch_id, 'invoice.ingest')  # check before parsing anything
    facts = extraction.facts_from_file(name, content, settings=settings, date_order=settings.rag_date_order)
    rows = await service.add_invoices(batch_id, [('file', facts, name, extraction.digest_of(content))])
    await db.commit()
    return {'invoices': [invoice_out(r) for r in rows]}


@router.post('/batches/{batch_id}/invoices/receipt-photo', status_code=201)
async def upload_receipt_photo(batch_id: UUID, request: Request, db: DbSession, user: CurrentUser,
                               settings: AppSettings, file: UploadFile = File(...)):
    """Receipt photo -> `pending` invoice in the approval queue (Step AC slice 2).

    The photo is read with local OCR and run through the same extractor as text/file
    intake; the same 17 verification checks run when the batch starts. A photo OCR
    cannot read is an error, never a guessed invoice. Never pays.
    """
    content = await file.read(10 * 1024 * 1024 + 1)
    name = (file.filename or 'receipt.jpg')[:200]
    service = svc(request, db, user, settings)
    rows = await service.add_receipt_photo(batch_id, name, content)
    await db.commit()
    return {'invoices': [invoice_out(r) for r in rows]}


@router.post('/batches/{batch_id}/start', status_code=202)
async def start_batch(batch_id: UUID, request: Request, db: DbSession, user: CurrentUser, settings: AppSettings):
    batch, run = await svc(request, db, user, settings).start(batch_id)
    await db.commit()
    return {'batch': batch_out(batch), 'run_id': str(run.id)}


@router.get('/batches/{batch_id}/payments')
async def payments(batch_id: UUID, request: Request, db: DbSession, user: CurrentUser, settings: AppSettings):
    return {'payments': await svc(request, db, user, settings).payments(batch_id)}


@router.get('/exceptions')
async def exceptions(request: Request, db: DbSession, user: CurrentUser, settings: AppSettings, group_id: UUID | None = None):
    rows = await svc(request, db, user, settings).exceptions(group_id)
    return {'invoices': [invoice_out(r) for r in rows]}


@router.post('/invoices/{invoice_id}/resolve')
async def resolve(invoice_id: UUID, body: ResolveIn, request: Request, db: DbSession, user: CurrentUser, settings: AppSettings):
    fields = body.fields.model_dump(mode='json', exclude_none=True) if body.fields else None
    row = await svc(request, db, user, settings).resolve(invoice_id, body.action, fields=fields, note=body.note)
    await db.commit()
    return invoice_out(row)


@router.get('/approvals')
async def approvals(request: Request, db: DbSession, user: CurrentUser, settings: AppSettings):
    return {'approvals': await svc(request, db, user, settings).approvals()}


@router.post('/approvals/{approval_id}/decide')
async def decide(approval_id: UUID, body: DecisionIn, request: Request, db: DbSession, user: CurrentUser, settings: AppSettings):
    row = await svc(request, db, user, settings).decide(approval_id, body.decision, presented_hash=body.presented_hash,
                                                        note=body.note)
    await db.commit()
    return {'approval_id': str(row.id), 'status': row.status}


@router.get('/spend')
async def spend(request: Request, db: DbSession, user: CurrentUser, settings: AppSettings,
                months: int = Query(12, ge=1, le=36), group_id: UUID | None = None):
    return await svc(request, db, user, settings).spend(months=months, group_id=group_id)


@router.get('/cash-flow')
async def cash_flow(request: Request, db: DbSession, user: CurrentUser, settings: AppSettings,
                    currency: str = Query(pattern=CURRENCY), months: int = Query(3, ge=1, le=12),
                    group_id: UUID | None = None, opening_balance: Decimal | None = Query(default=None, ge=0)):
    """Cash-flow forecast (Step AC slice 3): forward projection, every month labelled
    `forecast`. Read-only — it never pays, approves or changes anything."""
    result = await svc(request, db, user, settings).cash_flow(
        months=months, currency=currency, group_id=group_id, opening_balance=opening_balance)
    return cash_flow_out(result)


@router.get('/cash-flow.csv')
async def cash_flow_csv_export(request: Request, db: DbSession, user: CurrentUser, settings: AppSettings,
                               currency: str = Query(pattern=CURRENCY), months: int = Query(3, ge=1, le=12),
                               group_id: UUID | None = None, opening_balance: Decimal | None = Query(default=None, ge=0)):
    """One-click CSV export of the same cash-flow projection."""
    result = await svc(request, db, user, settings).cash_flow(
        months=months, currency=currency, group_id=group_id, opening_balance=opening_balance)
    return Response(content=cash_flow_csv(result), media_type='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="cash-flow-{result.as_of.isoformat()}.csv"'})


@router.get('/tax-report')
async def tax_report(request: Request, db: DbSession, user: CurrentUser, settings: AppSettings,
                     months: int = Query(12, ge=1, le=36), group_id: UUID | None = None,
                     currency: str | None = Query(default=None, pattern=CURRENCY)):
    """Tax report (Step AC slice 4): totals by period + department + currency over
    verified/paid invoices. Read-only — it totals DB rows, never pays or approves."""
    result = await svc(request, db, user, settings).tax_report(
        months=months, group_id=group_id, currency=currency)
    return tax_report_out(result)


@router.get('/tax-report.csv')
async def tax_report_csv_export(request: Request, db: DbSession, user: CurrentUser, settings: AppSettings,
                                months: int = Query(12, ge=1, le=36), group_id: UUID | None = None,
                                currency: str | None = Query(default=None, pattern=CURRENCY)):
    """One-click CSV download of the same tax report."""
    result = await svc(request, db, user, settings).tax_report(
        months=months, group_id=group_id, currency=currency)
    return Response(content=tax_report_csv(result), media_type='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="tax-report-{result["to"]}.csv"'})


__all__ = ['router', 'payment_out']
