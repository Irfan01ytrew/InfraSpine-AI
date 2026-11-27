"""Tests for accounting system export integrations (QuickBooks, NetSuite, SAP)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors import InvalidInputError, PermissionDeniedError
from app.db.models import Document, FinancialFormRecord, FinancialFormReviewRevision, User
from app.services.financial_form_reviews import FinancialFormReviewService


def _setup_mock_service(role="admin"):
    mock_db = AsyncMock()
    mock_user = MagicMock(spec=User)
    mock_user.id = uuid.uuid4()
    mock_user.role = role
    mock_ctx = MagicMock()

    service = FinancialFormReviewService(mock_db, mock_user, mock_ctx)
    return service, mock_db, mock_user


def test_accounting_export_rejects_unknown_target():
    import asyncio

    async def _run():
        service, _, _ = _setup_mock_service("admin")
        with pytest.raises(InvalidInputError, match="Unsupported accounting target system"):
            await service.export_accounting_batch(target_system="unknown_erp")

    asyncio.run(_run())


def test_accounting_export_denies_employee():
    import asyncio

    async def _run():
        service, _, _ = _setup_mock_service("employee")
        with pytest.raises(PermissionDeniedError):
            await service.export_accounting_batch(target_system="quickbooks")

    asyncio.run(_run())


def test_accounting_export_formats():
    import asyncio

    async def _run():
        service, mock_db, _ = _setup_mock_service("admin")

        doc_id = uuid.uuid4()
        record_id = uuid.uuid4()

        mock_doc = MagicMock(spec=Document)
        mock_doc.id = doc_id
        mock_doc.filename = "Vendor_Invoice_4821.pdf"
        mock_doc.version = 1
        mock_doc.sha256 = "abc123sha"

        mock_record = MagicMock(spec=FinancialFormRecord)
        mock_record.id = record_id
        mock_record.document_id = doc_id
        mock_record.document_version = 1
        mock_record.source_sha256 = "abc123sha"
        mock_record.profile = "invoice"
        mock_record.extraction_data = {"fields": {}}

        mock_reviewed = MagicMock(spec=FinancialFormReviewRevision)
        mock_reviewed.record_id = record_id
        mock_reviewed.revision = 1
        mock_reviewed.reviewed_fields = {
            "vendor": "Acme Industrial Tools",
            "invoice_number": "INV-2026-999",
            "document_date": "2026-10-15",
            "total": "4500.00",
            "currency": "USD",
            "tax": "450.00",
        }
        mock_reviewed.reviewed_line_items = [
            {"description": "Heavy Machinery Parts", "amount": "4050.00"}
        ]
        mock_reviewed.created_at = datetime.now(UTC)

        # Mock DB execute returns
        # 1. First query: records + documents
        # 2. Second query: latest reviews
        res1 = MagicMock()
        res1.all.return_value = [(mock_record, mock_doc)]

        res2 = MagicMock()
        res2.all.return_value = [(mock_reviewed, "auditor@company.com")]

        mock_db.execute.side_effect = [res1, res2, res1, res2, res1, res2]

        # 1. QuickBooks IIF
        qb_bytes, qb_type, qb_fn, qb_count = await service.export_accounting_batch("quickbooks")
        assert qb_count == 1
        assert qb_type == "application/qbooks"
        assert qb_fn.endswith(".iif")
        qb_str = qb_bytes.decode("utf-8")
        assert "!TRNS" in qb_str and "!SPL" in qb_str and "!ENDTRNS" in qb_str
        assert "Acme Industrial Tools" in qb_str
        assert "INV-2026-999" in qb_str
        assert "-4500.00" in qb_str

        # 2. NetSuite CSV
        ns_bytes, ns_type, ns_fn, ns_count = await service.export_accounting_batch("netsuite")
        assert ns_count == 1
        assert "text/csv" in ns_type
        assert ns_fn.endswith(".csv")
        ns_str = ns_bytes.decode("utf-8-sig")
        assert "ExternalID,Vendor,Date" in ns_str
        assert "Acme Industrial Tools" in ns_str
        assert "4500.00" in ns_str

        # 3. SAP IDoc CSV
        sap_bytes, sap_type, sap_fn, sap_count = await service.export_accounting_batch("sap")
        assert sap_count == 1
        assert "text/csv" in sap_type
        assert sap_fn.endswith(".csv")
        sap_str = sap_bytes.decode("utf-8-sig")
        assert "EDI_DC40,DOCNUM,STATUS" in sap_str
        assert "INVOIC02" in sap_str
        assert "Acme Industrial Tools" in sap_str
        assert "4500.00" in sap_str

    asyncio.run(_run())
