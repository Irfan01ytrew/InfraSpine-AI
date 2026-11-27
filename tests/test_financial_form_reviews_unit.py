"""Service-independent validation for finance review and CSV handoff rules."""

import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.errors import InvalidInputError, PermissionDeniedError
from app.schemas.financial_forms import FinancialFormReviewRequest
from app.services.financial_form_reviews import (
    FinancialFormReviewService,
    _safe_csv,
    _validate_currency,
    _validate_decimal,
    _validate_exception_acknowledgements,
    _validate_fields,
    _validate_reviewed_totals,
)


def test_bookkeeping_export_denies_employee_before_document_or_record_queries():
    class UnusedDatabase:
        async def scalar(self, *_args, **_kwargs):
            pytest.fail("an unauthorized export must be rejected before database access")

        async def execute(self, *_args, **_kwargs):
            pytest.fail("an unauthorized export must be rejected before database access")

    employee = SimpleNamespace(id=uuid.uuid4(), email="employee@example.test", role="employee")
    service = FinancialFormReviewService(UnusedDatabase(), employee, ctx=None)
    with pytest.raises(PermissionDeniedError):
        service.bookkeeping_csv(uuid.uuid4()).send(None)


@pytest.mark.parametrize("role", ["admin", "manager"])
def test_bookkeeping_export_allows_admin_and_manager_roles(role):
    class EmptyDatabase:
        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(all=lambda: [])

    user = SimpleNamespace(id=uuid.uuid4(), email=f"{role}@example.test", role=role)
    service = FinancialFormReviewService(EmptyDatabase(), user, ctx=None)
    with pytest.raises(StopIteration) as completed:
        service.bookkeeping_csv().send(None)

    body, record_count = completed.value.value
    assert body.startswith(b"\xef\xbb\xbfdocument_id,filename,")
    assert record_count == 0


def test_review_payload_requires_complete_bounded_human_rationale():
    payload = FinancialFormReviewRequest(
        expected_document_version=3,
        profile="invoice",
        fields={"invoice_number": "INV-1"},
        line_items=[],
        reviewed_exception_indexes=[0],
        rationale="Checked against source page 1",
    )
    assert payload.rationale == "Checked against source page 1"
    with pytest.raises(ValidationError):
        FinancialFormReviewRequest(
            expected_document_version=True,
            profile="invoice",
            fields={"invoice_number": "INV-1"},
            line_items=[],
            reviewed_exception_indexes=[0, 0],
            rationale="ok",
        )


@pytest.mark.parametrize("value", ["1,000.00", "NaN", "Infinity", "1.12345", "--1"])
def test_finance_decimal_values_reject_noncanonical_or_nonfinite_input(value):
    with pytest.raises(InvalidInputError):
        _validate_decimal(value, "total")


def test_finance_fields_normalize_currency_and_require_iso_date_shape():
    assert _validate_fields("invoice", {
        "invoice_number": " INV-1 ",
        "document_date": "2026-10-26",
        "subtotal": "10.00",
        "tax": "0",
        "total": "10.00",
        "currency": "usd",
    }) == {
        "invoice_number": "INV-1",
        "document_date": "2026-10-26",
        "subtotal": "10.00",
        "tax": "0",
        "total": "10.00",
        "currency": "USD",
    }
    with pytest.raises(InvalidInputError):
        _validate_fields("invoice", {"document_date": "26/10/2026"})
    with pytest.raises(InvalidInputError):
        _validate_fields("invoice", {"document_date": "20261026"})
    with pytest.raises(InvalidInputError):
        _validate_currency("US", "currency")
    with pytest.raises(InvalidInputError):
        _validate_currency("ZZZ", "currency")


def test_review_requires_acknowledging_every_extraction_exception():
    exceptions = [{"code": "missing_vendor"}, {"code": "invalid_total"}]
    _validate_exception_acknowledgements(exceptions, [0, 1])
    with pytest.raises(InvalidInputError):
        _validate_exception_acknowledgements(exceptions, [0])
    with pytest.raises(InvalidInputError):
        _validate_exception_acknowledgements(exceptions, [0, 2])


def test_reviewed_totals_reject_new_mismatches_but_allow_acknowledged_source_exception():
    record = SimpleNamespace(extraction_data={"exceptions": []})
    fields = {"subtotal": "20.00", "tax": "1.00", "total": "22.00"}
    with pytest.raises(InvalidInputError):
        _validate_reviewed_totals(record, fields, [])

    record.extraction_data["exceptions"] = [{"code": "total_components_mismatch"}]
    _validate_reviewed_totals(record, fields, [])


def test_reviewed_line_total_mismatch_must_match_source_exception_row():
    item = {"quantity": "2", "unit_price": "5", "amount": "12", "source": {"row_number": 4}}
    record = SimpleNamespace(extraction_data={"exceptions": []})
    with pytest.raises(InvalidInputError):
        _validate_reviewed_totals(record, {}, [item])

    record.extraction_data["exceptions"] = [{
        "code": "line_total_mismatch", "source": {"row_number": 4},
    }]
    _validate_reviewed_totals(record, {}, [item])


@pytest.mark.parametrize("value", ["=SUM(A1:A2)", "  -2+3", "\t@cmd", "+12", "\r=1"])
def test_bookkeeping_csv_escapes_spreadsheet_formula_values(value):
    assert _safe_csv(value).startswith("'")


def test_bookkeeping_csv_serializes_source_objects_before_formula_check():
    assert _safe_csv({"page": 1, "quote": "Invoice 123"}) == '{"page":1,"quote":"Invoice 123"}'
