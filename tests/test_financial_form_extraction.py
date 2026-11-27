"""PostgreSQL-backed API acceptance for bounded finance extraction and document ACL enforcement."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.rag_helpers import activate_provider, make_pdf, process_all


def test_financial_extraction_reads_indexed_source_and_obeys_document_acl(client):
    activate_provider(client)
    source = "\n".join((
        "TAX INVOICE",
        "Invoice Number: INV-204",
        "Invoice Date: 30/09/2026",
        "Vendor: North Star Supplies",
        "Subtotal: USD 20.00",
        "Tax: USD 0.00",
        "Grand Total: USD 20.00",
    ))
    uploaded = client.post(
        "/api/rag/documents",
        files={"file": ("invoice-204.pdf", make_pdf(source), "application/pdf")},
        data={"visibility": "shared"},
    )
    assert uploaded.status_code == 202, uploaded.text
    document_id = uploaded.json()["id"]
    process_all(client)

    extracted = client.get(f"/api/rag/documents/{document_id}/financial-extraction")
    assert extracted.status_code == 200, extracted.text
    result = extracted.json()
    assert result["profile"] == "invoice"
    assert result["status"] == "ready"
    assert result["document_version"] == 1
    assert result["fields"]["invoice_number"]["value"] == "INV-204"
    assert result["fields"]["invoice_number"]["source"]["position"] == 1
    assert result["fields"]["total"]["value"] == "20.00"
    assert result["fields"]["currency"]["value"] == "USD"
    assert result["exceptions"] == []
    assert result["record_id"] is None
    assert result["review_status"] == "not_saved"

    assert client.get(f"/api/rag/documents/{document_id}/financial-extraction?profile=unknown").status_code == 422

    draft = client.post(
        f"/api/rag/documents/{document_id}/financial-extraction",
        json={"profile": "auto", "date_order": "dmy"},
    )
    assert draft.status_code == 200, draft.text
    draft_result = draft.json()
    assert draft_result["review_status"] == "pending"
    assert draft_result["record_id"]
    assert client.get("/api/rag/financial-extractions/review-queue").json()["records"][0]["can_review"] is True
    pending_csv = client.get(f"/api/rag/financial-extractions/bookkeeping.csv?document_id={document_id}")
    assert pending_csv.status_code == 200
    assert b"INV-204" not in pending_csv.content

    review_payload = {
        "expected_document_version": 1,
        "profile": "invoice",
        "date_order": "dmy",
        "fields": {name: field["value"] for name, field in result["fields"].items()},
        "line_items": [
            {name: item.get(name) for name in ("description", "quantity", "unit_price", "amount", "currency")}
            for item in result["line_items"]
        ],
        "reviewed_exception_indexes": [],
        "rationale": "Verified against the current invoice source",
    }
    review_payload["fields"]["invoice_number"] = "INV-204-CHECKED"
    login(client, USER_EMAIL)
    assert client.get(f"/api/rag/documents/{document_id}/financial-extraction").status_code == 200
    assert client.get("/api/rag/financial-extractions/review-queue").json()["records"][0]["can_review"] is False
    user_pending_csv = client.get(f"/api/rag/financial-extractions/bookkeeping.csv?document_id={document_id}")
    assert user_pending_csv.status_code == 403
    assert client.post(
        f"/api/rag/documents/{document_id}/financial-extraction/review", json=review_payload
    ).status_code == 403
    login(client, ADMIN_EMAIL)
    assert client.get(f"/api/rag/documents/{document_id}/financial-extraction?profile=form").status_code == 200
    reviewed = client.post(
        f"/api/rag/documents/{document_id}/financial-extraction/review", json=review_payload
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["review_status"] == "reviewed"
    assert reviewed.json()["review"]["revision"] == 1
    assert len(reviewed.json()["review_history"]) == 1
    assert reviewed.json()["fields"]["invoice_number"]["value"] == "INV-204-CHECKED"
    reloaded = client.get(f"/api/rag/documents/{document_id}/financial-extraction")
    assert reloaded.status_code == 200
    assert reloaded.json()["fields"]["invoice_number"]["value"] == "INV-204-CHECKED"

    record_id = reviewed.json()["record_id"]
    with pytest.raises(SQLAlchemyError):
        run_db(lambda conn: conn.execute(text(
            "UPDATE financial_form_records SET extraction_data = '{}'::jsonb WHERE id = :record_id"
        ), {"record_id": record_id}))
    with pytest.raises(SQLAlchemyError):
        run_db(lambda conn: conn.execute(text(
            "UPDATE financial_form_review_revisions SET rationale = 'tampered' WHERE record_id = :record_id"
        ), {"record_id": record_id}))

    stale_payload = {**review_payload, "expected_document_version": 2}
    stale = client.post(
        f"/api/rag/documents/{document_id}/financial-extraction/review", json=stale_payload
    )
    assert stale.status_code == 409, stale.text
    reviewed_csv = client.get(f"/api/rag/financial-extractions/bookkeeping.csv?document_id={document_id}")
    assert reviewed_csv.status_code == 200
    assert b"INV-204-CHECKED" in reviewed_csv.content

    login(client, USER_EMAIL)
    employee_reviewed_csv = client.get(
        f"/api/rag/financial-extractions/bookkeeping.csv?document_id={document_id}"
    )
    assert employee_reviewed_csv.status_code == 403

    run_db(lambda conn: conn.execute(
        text("UPDATE users SET role = 'manager' WHERE email = :email"), {"email": USER2_EMAIL}
    ))
    login(client, USER2_EMAIL)
    manager_reviewed_csv = client.get(
        f"/api/rag/financial-extractions/bookkeeping.csv?document_id={document_id}"
    )
    assert manager_reviewed_csv.status_code == 200, manager_reviewed_csv.text
    assert b"INV-204-CHECKED" in manager_reviewed_csv.content
