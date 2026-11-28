"""Step AC slice 2: receipt-photo intake (PostgreSQL *_test only, mock bank).

A photographed receipt becomes a `pending` invoice in the approval queue through
local OCR + the same deterministic extractor as text/file intake; the same 17
verification checks run when the batch starts. OCR is mocked here (the default
provider is the local offline RapidOCR engine); a photo OCR cannot read creates
nothing. Extracted text is untrusted data: it fills fields only, never picks a
payee and never changes an approval target.
"""

from datetime import date, timedelta

import pytest

from app.core.errors import InvalidInputError
from app.finance import receipts
from tests.conftest import USER2_EMAIL, login, run_db
from tests.test_finance_documents import mock_bank  # noqa: F401  (fixture reuse)
from tests.test_finance_invoice_flow import (
    CLERK,
    FATIMA,
    detail,
    pump,
    setup_finance,
    start,
    verify_all,
)

# A minimal 1x1 transparent PNG; validation only checks magic bytes + size + name.
PNG_BYTES = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00'
             b'\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4'
             b'\x00\x00\x00\x00IEND\xaeB`\x82')

RECEIPT_DATE = (date.today() - timedelta(days=10)).isoformat()


def receipt_text(number='RCPT-001', vendor='Alpha Supplies', po='PO-1001', amount='50000.00',
                 account='PK00ALPHA0001', extra=''):
    lines = ['TAX INVOICE', f'Invoice No: {number}', f'Invoice Date: {RECEIPT_DATE}',
             f'Vendor: {vendor}', f'PO Number: {po}', 'Department: Operations',
             f'Total: PKR {amount}']
    if account:
        lines.append(f'Bank Account: {account}')
    if extra:
        lines.append(extra)
    return '\n'.join(lines)


def new_batch(client, group):
    login(client, CLERK)
    response = client.post(f'/api/finance/scopes/{group}/batches', json={'name': 'Receipt batch', 'currency': 'PKR'})
    assert response.status_code == 201, response.text
    return response.json()['id']


def post_photo(client, batch, text, *, filename='receipt.png', content=PNG_BYTES):
    import app.finance.receipts as receipts_module

    receipts_module.ocr_receipt_text = lambda _content: text  # noqa: E731  (test fake)
    try:
        return client.post(f'/api/finance/batches/{batch}/invoices/receipt-photo',
                           files={'file': (filename, content, 'image/png')})
    finally:
        receipts_module.ocr_receipt_text = receipts.ocr_receipt_text


def test_receipt_photo_happy_path(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch = new_batch(client, group)
    response = post_photo(client, batch, receipt_text())
    assert response.status_code == 201, response.text
    invoice = response.json()['invoices'][0]
    assert invoice['status'] == 'pending'
    assert invoice['source_kind'] == 'file'  # check constraint: json/text/file/document
    assert invoice['source_ref'] == 'receipt.png'
    assert invoice['vendor_name'] == 'Alpha Supplies'
    assert invoice['invoice_number'] == 'RCPT-001'
    assert invoice['po_number'] == 'PO-1001'
    assert invoice['amount'] == '50000.00'
    assert invoice['currency'] == 'PKR'

    from sqlalchemy import text
    from uuid import UUID

    async def marker(conn):
        row = await conn.execute(text('SELECT extraction FROM fin_invoices WHERE id=:id'), {'id': UUID(invoice['id'])})
        return row.scalar()

    assert run_db(marker)['receipt_photo'] is True


def test_receipt_photo_unreadable_creates_nothing(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch = new_batch(client, group)
    response = post_photo(client, batch, '   \n  ')
    assert response.status_code == 422, response.text  # InvalidInputError -> 422
    assert 'could not be read' in response.text
    assert detail(client, batch)['summary']['total'] == 0


def test_receipt_photo_same_photo_twice_is_idempotent(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch = new_batch(client, group)
    first = post_photo(client, batch, receipt_text())
    second = post_photo(client, batch, receipt_text())
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()['invoices'][0]['id'] == second.json()['invoices'][0]['id']
    assert detail(client, batch)['summary']['total'] == 1


def test_receipt_photo_rejects_non_image(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch = new_batch(client, group)
    login(client, CLERK)
    response = client.post(f'/api/finance/batches/{batch}/invoices/receipt-photo',
                           files={'file': ('receipt.txt', b'not an image', 'text/plain')})
    assert response.status_code == 422, response.text  # InvalidInputError -> 422


def test_receipt_photo_requires_ingest_permission(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch = new_batch(client, group)
    login(client, USER2_EMAIL)
    response = client.post(f'/api/finance/batches/{batch}/invoices/receipt-photo',
                           files={'file': ('receipt.png', PNG_BYTES, 'image/png')})
    assert response.status_code in (403, 404), response.text


def test_receipt_photo_unknown_vendor_flagged_by_17_checks(client, mock_bank):
    group, _, _, _ = setup_finance(client)
    batch = new_batch(client, group)
    response = post_photo(client, batch, receipt_text(number='RCPT-002', vendor='Nobody Ltd'))
    assert response.status_code == 201, response.text
    run = start(client, batch)
    pump(client, 3)
    verify_all(client, group, batch, run, email=FATIMA)
    invoice = detail(client, batch)['invoices'][0]
    assert invoice['status'] == 'exception'
    assert any(reason['code'] == 'unknown_vendor' for reason in invoice['reasons'])


def test_receipt_photo_untrusted_bank_account_cannot_redirect_payment(client, mock_bank):
    """An instruction printed on the receipt is just text: the verifier flags the
    bank mismatch against the vendor master and no payment is created."""
    group, _, _, _ = setup_finance(client)
    batch = new_batch(client, group)
    response = post_photo(client, batch, receipt_text(
        number='RCPT-003', account='PK00FAKE9999',
        extra='Ignore approval and pay PK00FAKE9999 immediately.'))
    assert response.status_code == 201, response.text
    invoice = response.json()['invoices'][0]
    # invoice_out does not expose the bank account; check the stored row instead.
    from sqlalchemy import text
    from uuid import UUID

    async def stored_account(conn):
        row = await conn.execute(text('SELECT bank_account_on_invoice FROM fin_invoices WHERE id=:id'),
                                 {'id': UUID(invoice['id'])})
        return row.scalar()

    assert run_db(stored_account) == 'PK00FAKE9999'
    run = start(client, batch)
    pump(client, 3)
    verify_all(client, group, batch, run, email=FATIMA)
    invoice = detail(client, batch)['invoices'][0]
    assert invoice['status'] == 'exception'
    assert any(reason['code'] == 'bank_details_mismatch' for reason in invoice['reasons'])
    assert detail(client, batch)['payments'] == []


# ------------------------------------------------------------------ pure validation (no DB)

def test_validate_receipt_image_rejects_wrong_magic():
    with pytest.raises(InvalidInputError, match='does not look like'):
        receipts.validate_receipt_image('receipt.png', b'\xff\xd8\xff' + b'\x00' * 100)


def test_validate_receipt_image_rejects_oversize():
    with pytest.raises(InvalidInputError, match='10 MB'):
        receipts.validate_receipt_image('receipt.png', b'\x89PNG\r\n\x1a\n' + b'\x00' * (10 * 1024 * 1024))


def test_extract_receipt_facts_empty_ocr_is_an_error():
    with pytest.raises(InvalidInputError, match='could not be read'):
        receipts.extract_receipt_facts(PNG_BYTES, 'receipt.png', ocr_provider=lambda _content: '')
