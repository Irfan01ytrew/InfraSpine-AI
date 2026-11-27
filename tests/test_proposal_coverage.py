"""Synthetic PostgreSQL/API acceptance for ACL-grounded tender coverage and human review."""

import json
from io import BytesIO

from openpyxl import load_workbook

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.rag_helpers import activate_provider, make_pdf, process_all
from tests.test_automation import _manager_department


def test_proposal_coverage_is_grounded_scoped_reviewed_and_revalidated_after_acl_change(client, fake_provider):
    department, _ = _manager_department(client, "Proposal Coverage Operations")
    fake_provider.rag_reply = lambda _message: (
        "The company provides continuous support coverage. The support plan sets a two-hour response objective [S1]."
    )

    activate_provider(client)
    uploaded = client.post(
        "/api/rag/documents",
        files={"file": ("synthetic-support-plan.pdf", make_pdf(
            "The company provides continuous support coverage. The support plan sets a two-hour response objective."
        ), "application/pdf")},
        data={"visibility": "restricted", "department_id": department["id"]},
    )
    assert uploaded.status_code == 202, uploaded.text
    document_id = uploaded.json()["id"]
    process_all(client)

    login(client, USER2_EMAIL)
    created = client.post(
        "/api/proposal-coverage",
        json={
            "title": "Synthetic support tender",
            "department_group_id": department["id"],
            "requirements": [{"requirement": "Provide continuous support coverage with a two-hour response objective."}],
        },
    )
    assert created.status_code == 201, created.text
    case = created.json()
    case_id = case["id"]
    requirement_id = case["requirements"][0]["id"]
    assert case["can_edit"] and case["can_submit"] is False

    login(client, USER_EMAIL)
    assert client.get(f"/api/proposal-coverage/{case_id}").status_code == 404
    assert client.get(f"/api/proposal-coverage/{case_id}/export?format=pdf").status_code == 404
    assert client.get("/api/proposal-coverage").json()["cases"] == []

    login(client, USER2_EMAIL)
    assert client.post(f"/api/proposal-coverage/{case_id}/submit").status_code == 422
    analyzed = client.post(
        f"/api/proposal-coverage/{case_id}/requirements/{requirement_id}/analyze", json={}
    )
    assert analyzed.status_code == 200, analyzed.text
    finding = analyzed.json()["requirements"][0]
    assert finding["evidence_current"] is True
    assert finding["insufficient_evidence"] is False
    assert len(finding["citations"]) == 1
    assert finding["citations"][0]["document_id"] == document_id
    assert finding["answer"] and "[S1]" in finding["answer"]
    proposal_call = next(
        (system_prompt, message)
        for system_prompt, message in reversed(fake_provider.calls)
        if system_prompt and "SOURCES:" in message
    )
    assert "For this proposal-coverage check" in proposal_call[0]
    assert "Do not say the company meets a requirement" in proposal_call[0]
    assert "Find internal company evidence" not in proposal_call[1]
    assert "Requirement:\n" not in proposal_call[1]

    submitted = client.post(f"/api/proposal-coverage/{case_id}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "submitted" and submitted.json()["revision_number"] == 1
    assert client.post(
        f"/api/proposal-coverage/{case_id}/review",
        json={"decision": "approved", "rationale": "Self approval must be refused."},
    ).status_code == 403

    login(client, "manager@example.com", "manager-password-123")
    queue = client.get("/api/proposal-coverage")
    assert any(row["id"] == case_id for row in queue.json()["cases"])
    manager_view = client.get(f"/api/proposal-coverage/{case_id}")
    assert manager_view.status_code == 200 and manager_view.json()["requirements"][0]["evidence_current"] is True
    saved_assessment = client.post(
        f"/api/proposal-coverage/{case_id}/requirements/{requirement_id}/assessment",
        json={"assessment": "meets", "rationale": "The plan names both coverage and response time."},
    )
    assert saved_assessment.status_code == 200, saved_assessment.text
    approved = client.post(
        f"/api/proposal-coverage/{case_id}/review",
        json={"decision": "approved", "rationale": "All cited requirements were reviewed."},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["reviews"][0]["decision"] == "approved"
    for file_format, media_type, signature in (
        ("pdf", "application/pdf", b"%PDF-"),
        ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK\x03\x04"),
        ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK\x03\x04"),
    ):
        exported = client.get(f"/api/proposal-coverage/{case_id}/export?format={file_format}")
        assert exported.status_code == 200, exported.text
        assert exported.headers["content-type"].startswith(media_type)
        assert exported.headers["cache-control"] == "no-store"
        assert exported.headers["x-content-type-options"] == "nosniff"
        assert exported.content.startswith(signature)
        assert exported.headers["content-disposition"].endswith(f".{file_format}\"")

    # Remove the department grant after approval. Saved answer text and snippets must be withheld immediately.
    login(client, ADMIN_EMAIL)
    changed = client.put(
        f"/api/rag/documents/{document_id}/access",
        json={"visibility": "private", "user_emails": [], "group_ids": []},
    )
    assert changed.status_code == 200, changed.text
    login(client, USER2_EMAIL)
    after_revocation = client.get(f"/api/proposal-coverage/{case_id}")
    assert after_revocation.status_code == 200
    revoked_finding = after_revocation.json()["requirements"][0]
    assert revoked_finding["evidence_current"] is False
    assert revoked_finding["citations"] == []
    assert "withheld" in revoked_finding["answer"].lower()
    assert "continuous support coverage" not in revoked_finding["answer"]
    stale_export = client.get(f"/api/proposal-coverage/{case_id}/export?format=xlsx")
    assert stale_export.status_code == 200, stale_export.text
    stale_sheet = load_workbook(BytesIO(stale_export.content), data_only=False)["Requirements"]
    assert "withheld" in stale_sheet["E2"].value.lower()
    assert stale_sheet["F2"].value == "None"

    async def read_audit_text(conn):
        result = await conn.execute(text(
            "SELECT details::text FROM audit_logs WHERE action LIKE 'proposal_coverage.%'"
        ))
        return result.scalars().all()

    audit_text = run_db(read_audit_text)
    assert audit_text
    assert all("two-hour response objective" not in row for row in audit_text)
    assert run_db(lambda conn: conn.scalar(text("SELECT count(*) FROM proposal_coverage_reviews"))) == 1


def test_rfp_requirement_extraction_returns_editable_source_cited_candidates_and_checks_acl(client, fake_provider):
    activate_provider(client)
    uploaded = client.post(
        "/api/rag/documents",
        files={"file": ("synthetic-rfp.pdf", make_pdf("The bidder shall provide a 24-hour support desk."), "application/pdf")},
        data={"visibility": "private"},
    )
    assert uploaded.status_code == 202, uploaded.text
    document_id = uploaded.json()["id"]
    process_all(client)
    chunk_id = run_db(
        lambda conn: conn.scalar(
            text("SELECT id FROM document_chunks WHERE document_id = :id ORDER BY chunk_index LIMIT 1"),
            {"id": document_id},
        )
    )
    assert chunk_id is not None
    async def read_source_version(conn):
        result = await conn.execute(
            text("SELECT version, sha256 FROM documents WHERE id = :id"), {"id": document_id}
        )
        return result.one()

    source_version, source_sha = run_db(read_source_version)
    fake_provider.rag_reply = lambda _message: json.dumps({
        "requirements": [{
            "requirement": "Provide a 24-hour support desk.",
            "evidence_chunk_ids": [chunk_id],
        }]
    })

    extracted = client.post("/api/proposal-coverage/extract-requirements", json={"document_ids": [document_id]})
    assert extracted.status_code == 200, extracted.text
    body = extracted.json()
    assert body["source_document_count"] == 1 and len(body["candidates"]) == 1
    candidate = body["candidates"][0]
    assert candidate["requirement"] == "Provide a 24-hour support desk."
    assert candidate["evidence"] == [{
        "chunk_id": chunk_id,
        "document_id": document_id,
        "document_version": source_version,
        "document_sha256": source_sha,
        "filename": "synthetic-rfp.pdf",
        "location": "Page 1",
        "excerpt": "The bidder shall provide a 24-hour support desk.",
    }]
    assert client.get("/api/proposal-coverage").json()["cases"] == []
    audit_detail = run_db(lambda conn: conn.scalar(text(
        "SELECT details::text FROM audit_logs WHERE action = 'proposal_coverage.requirements_extracted' ORDER BY created_at DESC LIMIT 1"
    )))
    assert audit_detail and "24-hour support desk" not in audit_detail
    created_case = client.post("/api/proposal-coverage", json={
        "title": "Synthetic RFP traceability",
        "department_group_id": None,
        "requirements": [{
            "requirement": candidate["requirement"],
            "source_chunk_ids": [chunk_id],
        }],
    })
    assert created_case.status_code == 201, created_case.text
    case_id = created_case.json()["id"]
    origin = created_case.json()["requirements"][0]
    assert origin["origin_evidence_current"] is True
    assert origin["origin_evidence"][0]["document_sha256"] == source_sha

    call_count = len(fake_provider.calls)
    login(client, USER_EMAIL)
    denied = client.post("/api/proposal-coverage/extract-requirements", json={"document_ids": [document_id]})
    assert denied.status_code == 404
    assert len(fake_provider.calls) == call_count, "unauthorized source text must not be sent to the model"

    login(client, ADMIN_EMAIL)
    run_db(lambda conn: conn.execute(
        text("UPDATE documents SET sha256 = :sha WHERE id = :id"),
        {"sha": "f" * 64, "id": document_id},
    ))
    stale_origin = client.get(f"/api/proposal-coverage/{case_id}").json()["requirements"][0]
    assert stale_origin["origin_evidence"] == []
    assert stale_origin["origin_evidence_current"] is False

    fake_provider.rag_reply = lambda _message: json.dumps({
        "requirements": [{
            "requirement": "A fabricated citation must be rejected.",
            "evidence_chunk_ids": [chunk_id + 1_000_000],
        }]
    })
    fabricated = client.post("/api/proposal-coverage/extract-requirements", json={"document_ids": [document_id]})
    assert fabricated.status_code == 502, fabricated.text
