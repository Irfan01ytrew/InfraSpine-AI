"""Isolated PostgreSQL/API/worker coverage for partner document retrieval and revocation."""
import io
import uuid
from datetime import UTC, datetime, timedelta

import docx
import openpyxl
from pptx import Presentation
from sqlalchemy import text

from app.core.security import PARTNER_SESSION_COOKIE_NAME
from app.services.partner_retrieval import retrieve_partner_chunks
from tests.conftest import ADMIN_EMAIL, FakeEmbedder, login, run_db
from tests.rag_helpers import make_pdf
from tests.test_partner_portal import MockPartnerRuntime, _partner_login


def _bytes(document):
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def _office_files():
    word = docx.Document()
    word.add_heading("Marigold support policy", level=1)
    word.add_paragraph("Marigold support responds within 11 business hours.")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Dispatch"
    sheet.append(["Service", "Window"])
    sheet.append(["Copperpine dispatch", "21:35 UTC"])
    slides = Presentation()
    slide = slides.slides.add_slide(slides.slide_layouts[1])
    slide.shapes.title.text = "Lagoon renewal"
    slide.placeholders[1].text = "Lagoon renewal quota is 42 units per quarter."
    return {"policy.docx": _bytes(word), "dispatch.xlsx": _bytes(book), "renewal.pptx": _bytes(slides)}


def _workspace(client, slug):
    response = client.post("/api/admin/partner-workspaces", json={"slug": slug, "name": slug.title()})
    assert response.status_code == 201, response.text
    return response.json()


def _upload(client, workspace_id, name, data):
    response = client.post(f"/api/admin/partner-workspaces/{workspace_id}/sources/upload",
                           files={"file": (name, data, "application/octet-stream")})
    assert response.status_code == 201, response.text
    assert response.json()["index_status"] == "pending"
    return response.json()


def _process_all(client):
    count = 0
    while client.portal.call(client.app.state.rag.partner_worker.process_next):
        count += 1
    return count


def _invite(client, workspace, email):
    invitation = client.post(f"/api/admin/partner-workspaces/{workspace['id']}/invitations", json={"email": email}).json()
    password = "Synthetic!partner-9876"
    accepted = client.post("/api/partner/auth/accept", json={"token": invitation["token"], "password": password})
    assert accepted.status_code == 200, accepted.text
    assert _partner_login(client, workspace["slug"], email, password).status_code == 200


def test_partner_pdf_office_hybrid_citations_isolation_and_revocation(client, monkeypatch):
    import app.api.routes.partner_portal as partner_routes

    monkeypatch.setattr(partner_routes, "get_llm_runtime", lambda request: MockPartnerRuntime())
    embedder = client.app.state.rag.embedder
    original_embed = embedder.embed
    def semantic_aliases(texts):
        return original_embed([value.replace("departure", "frostline dispatch").replace("commence", "opens") for value in texts])
    monkeypatch.setattr(embedder, "embed", semantic_aliases)
    login(client, ADMIN_EMAIL)
    alpha, beta = _workspace(client, "alpha-docs"), _workspace(client, "beta-docs")
    pdf = _upload(client, alpha["id"], "frostline.pdf", make_pdf(
        "This page describes a general introduction.", "Frostline dispatch opens at 09:15 UTC."))
    offices = {name: _upload(client, alpha["id"], name, payload) for name, payload in _office_files().items()}
    other = _upload(client, beta["id"], "beta.pdf", make_pdf("Beta reference is BETA-9137."))
    assert _process_all(client) == 5
    sources = client.get(f"/api/admin/partner-workspaces/{alpha['id']}/sources").json()
    assert all(row["index_status"] == "ready" for row in sources)
    assert all("content" not in row for row in sources)

    async def semantic_search():
        async with client.app.state.sessionmaker() as db:
            return await retrieve_partner_chunks(db, uuid.UUID(alpha["id"]), "When does departure commence?",
                client.app.state.rag.partner_indexer, embedder)
    semantic = client.portal.call(semantic_search)
    assert any(str(chunk.document_id) == pdf["id"] for chunk in semantic), "semantic-only candidate missing"
    assert client.app.state.rag.partner_store.count() > 0

    _invite(client, alpha, "reader-alpha@example.test")
    client.cookies.delete("infraspine_session")
    for question, source_id, location in (
        ("When does Frostline dispatch open?", pdf["id"], "Page 2"),
        ("How quickly does Marigold support respond?", offices["policy.docx"]["id"], "Marigold support policy"),
        ("What is the Copperpine dispatch window?", offices["dispatch.xlsx"]["id"], "Dispatch"),
        ("What is the Lagoon renewal quota?", offices["renewal.pptx"]["id"], "Slide 1"),
    ):
        answer = client.post("/api/partner/assistant/ask", json={"question": question})
        assert answer.status_code == 200, answer.text
        assert not answer.json()["insufficient_evidence"], answer.json()
        assert any(c["source_record_id"] == source_id and location in c["location_label"] for c in answer.json()["citations"])

    # Excel's source location is retained even when an answer needs further semantic verification.
    async def sheet_search():
        async with client.app.state.sessionmaker() as db:
            return await retrieve_partner_chunks(db, uuid.UUID(alpha["id"]), "Copperpine dispatch window",
                client.app.state.rag.partner_indexer, embedder)
    sheet_chunks = client.portal.call(sheet_search)
    assert any(str(chunk.document_id) == offices["dispatch.xlsx"]["id"] and "Dispatch" in chunk.location_label for chunk in sheet_chunks)

    denied = client.post("/api/partner/assistant/ask", json={"question": "What is BETA-9137?"}).json()
    assert "BETA-9137" not in denied["answer"]
    assert all(c["source_record_id"] != other["id"] for c in denied["citations"])
    login(client, ADMIN_EMAIL)
    assert client.delete(f"/api/admin/partner-workspaces/{alpha['id']}/sources/{pdf['id']}").status_code == 204
    after_archive = client.post("/api/partner/assistant/ask", json={"question": "When does Frostline dispatch open?"}).json()
    assert all(c["source_record_id"] != pdf["id"] for c in after_archive["citations"])
    identities = client.get(f"/api/admin/partner-workspaces/{alpha['id']}/identities").json()
    assert client.patch(f"/api/admin/partner-workspaces/{alpha['id']}/identities/{identities[0]['id']}/active",
                        json={"active": False}).status_code == 200
    assert client.post("/api/partner/assistant/ask", json={"question": "Marigold support?"}).status_code == 401


def test_partner_worker_retries_embedding_failure_and_fences_expired_claim(client):
    login(client, ADMIN_EMAIL)
    workspace = _workspace(client, "retry-docs")
    source = _upload(client, workspace["id"], "retry.pdf", make_pdf("Orchid support opens at noon UTC."))
    worker = client.app.state.rag.partner_worker
    FakeEmbedder.fail = True
    assert client.portal.call(worker.process_next)
    row = client.get(f"/api/admin/partner-workspaces/{workspace['id']}/sources").json()[0]
    assert row["index_status"] == "pending" and row["index_attempts"] == 1
    FakeEmbedder.fail = False
    assert client.portal.call(worker.process_next)
    row = client.get(f"/api/admin/partner-workspaces/{workspace['id']}/sources").json()[0]
    assert row["index_status"] == "ready" and row["index_attempts"] == 2

    another = _upload(client, workspace["id"], "lease.pdf", make_pdf("Violet dispatch opens at noon UTC."))
    old_job = client.portal.call(worker._claim)
    assert str(old_job.source_id) == another["id"]
    async def expire(conn):
        await conn.execute(text("UPDATE partner_knowledge_sources SET locked_until=:expired WHERE id=:id"),
                           {"expired": datetime.now(UTC) - timedelta(seconds=1), "id": uuid.UUID(another["id"])})
    run_db(expire)
    assert client.portal.call(worker.process_next)
    client.portal.call(worker._fail, old_job, "late_failure", True)
    rows = client.get(f"/api/admin/partner-workspaces/{workspace['id']}/sources").json()
    recovered = next(item for item in rows if item["id"] == another["id"])
    assert recovered["index_status"] == "ready" and recovered["index_attempts"] == 2
    assert recovered["index_error_code"] is None

    revoked = _upload(client, workspace["id"], "cancel.pdf", make_pdf("Amber dispatch opens at noon UTC."))
    stale_job = client.portal.call(worker._claim)
    chunks, vectors = worker._build(stale_job)
    assert client.delete(f"/api/admin/partner-workspaces/{workspace['id']}/sources/{revoked['id']}").status_code == 204
    client.portal.call(worker._finish, stale_job, chunks, vectors)
    async def count_chunks(conn):
        return await conn.scalar(text("SELECT count(*) FROM partner_knowledge_chunks WHERE source_id=:id"),
                                 {"id": uuid.UUID(revoked["id"])})
    assert run_db(count_chunks) == 0


def test_partner_upload_rejects_format_spoofing_and_non_admin(client):
    login(client, ADMIN_EMAIL)
    workspace = _workspace(client, "upload-validation")
    bad = client.post(f"/api/admin/partner-workspaces/{workspace['id']}/sources/upload",
                      files={"file": ("wrong.docx", make_pdf("wrong format"), "application/pdf")})
    assert bad.status_code == 415
    oversized = client.post(f"/api/admin/partner-workspaces/{workspace['id']}/sources/upload",
                            files={"file": ("huge.pdf", b"%PDF-" + b"x" * (2 * 1024 * 1024), "application/pdf")})
    assert oversized.status_code == 413
    assert client.get(f"/api/admin/partner-workspaces/{workspace['id']}/sources").json() == []
    client.cookies.delete("infraspine_session")
    denied = client.post(f"/api/admin/partner-workspaces/{workspace['id']}/sources/upload",
                         files={"file": ("private.pdf", make_pdf("private"), "application/pdf")})
    assert denied.status_code == 401


def test_indexed_partner_text_remains_searchable(client, monkeypatch):
    import app.api.routes.partner_portal as partner_routes
    monkeypatch.setattr(partner_routes, "get_llm_runtime", lambda request: MockPartnerRuntime())
    login(client, ADMIN_EMAIL)
    workspace = _workspace(client, "indexed-text")
    response = client.post(f"/api/admin/partner-workspaces/{workspace['id']}/sources/upload",
                           files={"file": ("service.md", b"Willow support opens at 07:45 UTC.", "text/markdown")})
    assert response.status_code == 201
    assert _process_all(client) == 1
    source = client.get(f"/api/admin/partner-workspaces/{workspace['id']}/sources").json()[0]
    assert source["index_status"] == "ready"
    _invite(client, workspace, "indexed-text@example.test")
    answer = client.post("/api/partner/assistant/ask", json={"question": "When does Willow support open?"})
    assert answer.status_code == 200 and not answer.json()["insufficient_evidence"]
    assert "07:45 UTC" in answer.json()["answer"]
    assert answer.json()["citations"][0]["source_record_id"] == source["id"]


def test_partner_original_download_scoped_and_integrity_checked(client, monkeypatch):
    import app.api.routes.partner_portal as partner_routes
    monkeypatch.setattr(partner_routes, "get_llm_runtime", lambda request: MockPartnerRuntime())
    login(client, ADMIN_EMAIL)
    alpha, beta = _workspace(client, "download-alpha"), _workspace(client, "download-beta")
    files = {"quartz.pdf": make_pdf("Quartz dispatch opens at 09:40 UTC."), **_office_files()}
    sources = {name: _upload(client, alpha["id"], name, payload) for name, payload in files.items()}
    other = _upload(client, beta["id"], "beta.pdf", make_pdf("Private beta facts."))
    assert _process_all(client) == 5
    _invite(client, alpha, "download-reader@example.test")
    for name, source in sources.items():
        result = client.get(f"/api/partner/sources/{source['id']}/file")
        assert result.status_code == 200 and result.content == files[name]
        assert result.headers["cache-control"] == "no-store"
        assert result.headers["content-disposition"].startswith("attachment;")
        assert result.headers["x-content-type-options"] == "nosniff"
    answer = client.post("/api/partner/assistant/ask", json={"question": "When does Quartz dispatch open?"})
    assert answer.status_code == 200
    assert any(c["source_record_id"] == sources["quartz.pdf"]["id"] and c["download_available"] for c in answer.json()["citations"])
    assert client.get(f"/api/partner/sources/{other['id']}/file").status_code == 404
    assert client.delete(f"/api/admin/partner-workspaces/{alpha['id']}/sources/{sources['quartz.pdf']['id']}").status_code == 204
    assert client.get(f"/api/partner/sources/{sources['quartz.pdf']['id']}/file").status_code == 404
    async def key_for(conn):
        return await conn.scalar(text("SELECT storage_key FROM partner_knowledge_sources WHERE id=:id"),
                                 {"id": uuid.UUID(sources["policy.docx"]["id"])})
    storage_key = run_db(key_for)
    client.app.state.rag.partner_worker.storage.path_for(storage_key).write_bytes(b"changed test file")
    assert client.get(f"/api/partner/sources/{sources['policy.docx']['id']}/file").status_code == 409
    identity = client.get(f"/api/admin/partner-workspaces/{alpha['id']}/identities").json()[0]
    assert client.patch(f"/api/admin/partner-workspaces/{alpha['id']}/identities/{identity['id']}/active", json={"active": False}).status_code == 200
    assert client.get(f"/api/partner/sources/{sources['dispatch.xlsx']['id']}/file").status_code == 401


def test_partner_revocation_during_answer_withholds_response(client, monkeypatch):
    import app.api.routes.partner_portal as partner_routes
    from sqlalchemy import update
    from app.db.models import PartnerIdentity, PartnerKnowledgeSource, PartnerSession
    from tests.fake_llm import MockLLMClient

    login(client, ADMIN_EMAIL)
    workspace = _workspace(client, "revocation-race")
    source = client.post(f"/api/admin/partner-workspaces/{workspace['id']}/sources",
        json={"title": "Harbor timetable", "content": "Harbor dispatch opens at 08:45 UTC."}).json()
    _invite(client, workspace, "race-reader@example.test")
    identity_id = client.get("/api/partner/auth/me").json()["id"]
    class RevokingClient:
        async def chat(self, message, *, system_prompt=None):
            async with client.app.state.sessionmaker() as db:
                await db.execute(update(PartnerKnowledgeSource).where(PartnerKnowledgeSource.id == uuid.UUID(source["id"]))
                                 .values(status="archived", archived_at=datetime.now(UTC)))
                await db.commit()
            return await MockLLMClient().chat(message, system_prompt=system_prompt)
    class Runtime:
        async def get_client(self, db):
            return RevokingClient()
    monkeypatch.setattr(partner_routes, "get_llm_runtime", lambda request: Runtime())
    withheld = client.post("/api/partner/assistant/ask", json={"question": "When does Harbor dispatch open?"})
    assert withheld.status_code == 200
    assert withheld.json()["insufficient_evidence"] and withheld.json()["citations"] == []
    assert "08:45" not in withheld.json()["answer"]

    client.post(f"/api/admin/partner-workspaces/{workspace['id']}/sources",
        json={"title": "Pier timetable", "content": "Pier dispatch opens at 10:25 UTC."})
    class DeactivatingClient:
        async def chat(self, message, *, system_prompt=None):
            async with client.app.state.sessionmaker() as db:
                await db.execute(update(PartnerIdentity).where(PartnerIdentity.id == uuid.UUID(identity_id)).values(is_active=False))
                await db.execute(update(PartnerSession).where(PartnerSession.identity_id == uuid.UUID(identity_id))
                                 .values(revoked_at=datetime.now(UTC)))
                await db.commit()
            return await MockLLMClient().chat(message, system_prompt=system_prompt)
    class DeactivatingRuntime:
        async def get_client(self, db):
            return DeactivatingClient()
    monkeypatch.setattr(partner_routes, "get_llm_runtime", lambda request: DeactivatingRuntime())
    denied = client.post("/api/partner/assistant/ask", json={"question": "When does Pier dispatch open?"})
    assert denied.status_code == 401
    assert "10:25" not in denied.text
