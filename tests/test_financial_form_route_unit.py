"""Service-independent checks for the financial extraction route helper."""

import uuid
from types import SimpleNamespace

import pytest

from app.services.financial_form_extraction import extract_financial_payload, overlay_reviewed_financial_values
from app.core.errors import ConflictError


class _Document:
    def __init__(self, *, status="ready"):
        self.id = uuid.uuid4()
        self.filename = "invoice-synthetic.pdf"
        self.version = 2
        self.indexed_version = 2
        self.status = status


class _Chunk:
    page_number = 1
    locator = {"kind": "page", "position": 1, "page": 1}
    content = "\n".join((
        "Invoice Number: INV-2026-01",
        "Invoice Date: 30/09/2026",
        "Vendor: Example Supplies",
        "Subtotal: USD 12.00",
        "Tax: USD 0.00",
        "Grand Total: USD 12.00",
    ))


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _Db:
    def __init__(self):
        self.calls = 0

    async def scalars(self, _query):
        self.calls += 1
        return _Scalars([_Chunk()] if self.calls == 1 else [])


def _run_synchronous_coroutine(coroutine):
    """Drive the no-I/O fake session coroutine without opening a Windows event-loop socketpair."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("The synchronous fake session unexpectedly suspended on external I/O.")


def test_financial_extraction_handler_returns_versioned_typed_source_fields():
    document = _Document()
    result = _run_synchronous_coroutine(extract_financial_payload(
        document, _Db(), profile="auto", date_order="dmy"
    ))

    assert result["document_version"] == 2
    assert result["profile"] == "invoice"
    assert result["status"] == "ready"
    assert result["fields"]["total"]["value"] == "12.00"
    assert result["fields"]["total"]["source"]["page"] == 1
    assert result["exceptions"] == []


def test_financial_extraction_refuses_unindexed_version():
    pending = _Document(status="processing")
    with pytest.raises(ConflictError):
        _run_synchronous_coroutine(extract_financial_payload(
            pending, _Db(), profile="auto", date_order="dmy"
        ))


def test_reviewed_corrections_overlay_display_values_without_changing_source_locations():
    extracted = {
        "fields": {"invoice_number": {
            "value": "INV-OCR", "raw": "INV-OCR", "valid": False, "source": {"page": 1},
        }},
        "line_items": [{
            "description": "Part", "quantity": "2", "unit_price": "5", "amount": "10",
            "currency": "USD", "source": {"table": 1, "row_number": 4},
        }],
    }
    review = SimpleNamespace(
        fields={"invoice_number": "INV-001"},
        line_items=[{
            "description": "Part A", "quantity": "2", "unit_price": "5", "amount": "10", "currency": "USD",
        }],
    )
    visible = overlay_reviewed_financial_values(extracted, review)

    assert visible["fields"]["invoice_number"]["value"] == "INV-001"
    assert visible["fields"]["invoice_number"]["source"] == {"page": 1}
    assert visible["line_items"][0]["description"] == "Part A"
    assert visible["line_items"][0]["source"] == {"table": 1, "row_number": 4}
    assert extracted["fields"]["invoice_number"]["value"] == "INV-OCR"
