"""Local synthetic tests for deterministic invoice, PO, and form field extraction."""

from app.rag.financial_forms import detect_form_profile, extract_financial_form
from app.rag.parsers.base import ExtractedTable


def _table(columns, rows, *, summary_rows=()):
    return ExtractedTable(
        index=1,
        title="Line items",
        kind="page",
        columns=tuple(columns),
        rows=tuple(tuple(row) for row in rows),
        row_positions=tuple(1 for _ in rows),
        summary_rows=tuple(tuple(row) for row in summary_rows),
        structured=True,
    )


def _codes(result):
    return {issue.code for issue in result.exceptions}


def test_invoice_fields_and_line_items_are_typed_and_keep_source_locations():
    text = "\n".join((
        "TAX INVOICE",
        "Invoice Number: INV-204",
        "Invoice Date: 30/09/2026",
        "Vendor: North Star Supplies",
        "Subtotal: USD 20.00",
        "Tax: USD 0.00",
        "Grand Total: USD 20.00",
    ))
    table = _table(
        ("Description", "Qty", "Unit Price", "Amount"),
        (("Filter assembly", "2", "USD 10.00", "USD 20.00"),),
    )
    result = extract_financial_form(
        "invoice", [(text, {"kind": "page", "position": 1, "label": "Page 1"})], [table]
    )

    assert detect_form_profile("INV-204.pdf", text) == "invoice"
    assert result.status == "ready" and not result.exceptions
    assert result.fields["invoice_number"].value == "INV-204"
    assert result.fields["document_date"].value == "2026-09-30"
    assert result.fields["vendor"].value == "North Star Supplies"
    assert result.fields["total"].value == "20.00"
    assert result.fields["currency"].value == "USD"
    assert result.line_items == ({
        "description": "Filter assembly",
        "quantity": "2",
        "unit_price": "10.00",
        "amount": "20.00",
        "currency": "USD",
        "source": {"kind": "page", "position": 1, "table_index": 1, "row_number": 1},
    },)


def test_purchase_order_uses_configured_date_order_and_requires_review_for_ambiguous_currency():
    text = "\n".join((
        "Purchase Order",
        "PO Number: PO-9007",
        "Order Date: 09/30/2026",
        "Supplier: North Star Supplies",
        "Order Total: Rs. 1,000/-",
    ))
    profile = detect_form_profile("orders/PO-9007.pdf", text)
    result = extract_financial_form(profile, [(text, {"kind": "page", "position": 2})], [], date_order="mdy")

    assert profile == "purchase_order"
    assert result.fields["purchase_order_number"].value == "PO-9007"
    assert result.fields["document_date"].value == "2026-09-30"
    assert result.fields["total"].value == "1000"
    assert result.fields["currency"].value is None
    assert result.status == "needs_review"
    assert "ambiguous_currency" in _codes(result)


def test_local_currency_symbols_are_flagged_for_human_currency_selection():
    for amount in ("₹ 2,500", "₨2,500", "€2,500", "£2,500", "$2,500"):
        result = extract_financial_form(
            "invoice",
            [(f"Invoice Number: INV-8\nInvoice Date: 30/09/2026\nVendor: Example\nTotal: {amount}", None)],
            [],
        )
        assert result.fields["total"].value == "2500"
        assert result.fields["currency"].value is None
        assert "ambiguous_currency" in _codes(result)


def test_invalid_grouping_missing_values_and_line_amount_are_queued_as_exceptions():
    text = "Invoice Number: INV-8\nInvoice Date: 13/32/2026\nTotal: USD 1,2,3"
    table = _table(
        ("Description", "Amount"),
        (("Pump repair", "1,2,3"),),
    )
    result = extract_financial_form("invoice", [(text, {"kind": "page", "position": 1})], [table])

    assert result.status == "needs_review"
    assert {"invalid_date", "required_field_missing", "invalid_amount", "invalid_line_amount"} <= _codes(result)
    assert result.fields["total"].value is None
    assert result.line_items[0]["amount"] is None


def test_generic_form_returns_labeled_values_without_inventing_fields():
    result = extract_financial_form(
        "form",
        [("Request ID: SR-18\nRequested By: Aisha Khan", {"kind": "page", "position": 1})],
        [],
    )

    assert result.status == "ready"
    assert result.fields["request_id"].value == "SR-18"
    assert result.fields["requested_by"].value == "Aisha Khan"


def test_unrelated_invoice_mention_does_not_auto_classify_as_an_invoice():
    assert detect_form_profile("billing-policy.pdf", "Policy for invoice disputes and refunds.") is None


def test_labels_flattened_into_one_paragraph_by_a_layout_parser_are_split_back():
    # Docling returns a block of short "Label: value" lines as one paragraph.
    text = ("TAX INVOICE Invoice Number: INV-204 Invoice Date: 30/09/2026 Vendor: North Star Supplies "
            "Subtotal: USD 20.00 Tax: USD 0.00 Grand Total: USD 20.00")
    result = extract_financial_form("invoice", [(text, {"kind": "page", "position": 1})], [])
    assert result.status == "ready" and result.exceptions == ()
    assert result.fields["invoice_number"].value == "INV-204"
    assert result.fields["document_date"].value == "2026-09-30"
    assert result.fields["vendor"].value == "North Star Supplies"
    assert (result.fields["subtotal"].value, result.fields["tax"].value, result.fields["total"].value) == ("20.00", "0.00", "20.00")
    assert result.fields["total"].source == {"kind": "page", "position": 1}


def test_a_single_label_line_with_a_colon_in_its_value_is_not_split():
    result = extract_financial_form("form", [("Notes: pay by date: 30 days", None)], [])
    assert result.fields["notes"].value == "pay by date: 30 days"
