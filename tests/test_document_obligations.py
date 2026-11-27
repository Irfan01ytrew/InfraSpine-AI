"""Synthetic PostgreSQL/API acceptance for document version comparison and obligation tracking."""

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login
from tests.rag_helpers import activate_provider, make_pdf, process_all


def test_document_comparison_and_obligation_access_work_with_live_acl(client):
    activate_provider(client)
    source = "Supplier shall provide a replacement within five business days."
    uploaded = client.post(
        "/api/rag/documents",
        files={"file": ("synthetic-terms.pdf", make_pdf(source, "Payment is due within thirty days."), "application/pdf")},
        data={"visibility": "shared"},
    )
    assert uploaded.status_code == 202, uploaded.text
    document_id = uploaded.json()["id"]
    process_all(client)

    created = client.post(
        f"/api/rag/documents/{document_id}/obligations",
        json={
            "title": "Replace defective delivery",
            "rationale": "Track the supplier's stated delivery duty.",
            "source_quote": source,
            "assigned_to_email": USER2_EMAIL,
            "due_at": "2030-04-15T17:00:00Z",
        },
    )
    assert created.status_code == 201, created.text
    obligation = created.json()
    assert obligation["source_current"] is True
    assert obligation["source_page_number"] == 1
    assert obligation["assigned_to_email"] == USER2_EMAIL

    # A shared document does not make another employee's assigned task visible.
    login(client, USER_EMAIL)
    assert client.get(f"/api/rag/document-obligations/{obligation['id']}").status_code == 404

    login(client, USER2_EMAIL)
    assert client.get(f"/api/rag/document-obligations/{obligation['id']}").status_code == 200
    acknowledged = client.patch(
        f"/api/rag/document-obligations/{obligation['id']}",
        json={"expected_version": 1, "status": "acknowledged", "rationale": "I have accepted this follow-up."},
    )
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["status"] == "acknowledged"
    assert acknowledged.json()["version"] == 2
    assert len(acknowledged.json()["history"]) == 2
    assert acknowledged.json()["history"][0]["action"] == "acknowledged"
    assert client.patch(
        f"/api/rag/document-obligations/{obligation['id']}",
        json={
            "expected_version": 2,
            "assigned_to_email": USER_EMAIL,
            "rationale": "An employee cannot reassign this task.",
        },
    ).status_code == 403

    login(client, ADMIN_EMAIL)
    replacement = client.put(
        f"/api/rag/documents/{document_id}/file",
        files={"file": ("synthetic-terms-v2.pdf", make_pdf("Supplier shall provide a replacement within ten business days."), "application/pdf")},
    )
    assert replacement.status_code == 202, replacement.text
    stale = client.get(f"/api/rag/document-obligations/{obligation['id']}")
    assert stale.status_code == 200 and stale.json()["source_current"] is False
    assert client.patch(
        f"/api/rag/document-obligations/{obligation['id']}",
        json={"expected_version": 2, "status": "completed", "rationale": "Do not close against a changed source."},
    ).status_code == 409

    comparison = client.get(
        f"/api/rag/documents/{document_id}/compare?from_version=1&to_version=2"
    )
    assert comparison.status_code == 200, comparison.text
    data = comparison.json()
    assert data["method"] == "deterministic_text_diff"
    assert data["added_lines"] >= 1 and data["removed_lines"] >= 1
    assert any("ten business days" in change["text"] for change in data["changes"] if change["kind"] == "added")
    assert data["obligation_impacts"] == [{
        "id": obligation["id"],
        "title": "Replace defective delivery",
        "status": "acknowledged",
        "source_version": 1,
        "source_quote": source,
        "quote_found_in_to_version": False,
    }]

    # Revoking shared access invalidates both the comparison and the task citation immediately.
    changed = client.put(
        f"/api/rag/documents/{document_id}/access",
        json={"visibility": "private", "user_emails": [], "group_ids": []},
    )
    assert changed.status_code == 200, changed.text
    login(client, USER2_EMAIL)
    assert client.get(f"/api/rag/documents/{document_id}/obligations").status_code == 404
    assert client.get(
        f"/api/rag/documents/{document_id}/compare?from_version=1&to_version=2"
    ).status_code == 404
