"""Uploaded PNG and JPEG images: validated at upload, described by the authorized vision provider in the worker,
indexed with MiniLM/FAISS like text, and cited as the image itself under the document's ACL. All images are
synthetic."""

import pytest

from app.core.errors import LLMTimeoutError
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login
from tests.rag_helpers import activate_provider, allow_images, ask, document, make_chart_image, process_all, upload
from tests.test_access_control import set_access
from tests.test_document_formats import chunks

DESCRIPTION = "Bar chart of quarterly revenue in PKR million: Q1 2.1, Q2 3.4, Q3 4.2, Q4 3.8. Q3 is the highest."
QUESTION = "Which quarter had the highest revenue?"


def _upload(client, filename: str, data: bytes):
    return upload(client, filename, data, content_type="application/octet-stream")


@pytest.mark.parametrize(("filename", "pil_format"), [("revenue.png", "PNG"), ("revenue.jpg", "JPEG")])
def test_image_upload_is_described_indexed_and_cited_under_its_acl(client, fake_provider, filename, pil_format):
    fake_provider.vision_reply = DESCRIPTION
    fake_provider.rag_reply = lambda message: "Q3 had the highest revenue [S1]."
    activate_provider(client)  # signs in as the admin
    assert allow_images(client).status_code == 200
    response = _upload(client, filename, make_chart_image(pil_format))
    assert response.status_code == 202, response.text
    doc = response.json()
    assert doc["file_format"] == ("png" if pil_format == "PNG" else "jpeg")
    assert set_access(client, doc["id"], "restricted", users=[USER_EMAIL]).status_code == 200
    process_all(client)

    assert document(client, doc["id"])["status"] == "ready"
    rows = chunks(doc["id"])
    assert len(rows) == 1 and rows[0].content == f"Image: {DESCRIPTION}"
    assert rows[0].locator == {"kind": "image", "position": 1, "label": "Image", "image": 1}
    assert len(fake_provider.image_calls) == 1 and fake_provider.image_calls[0].startswith(b"\x89PNG")

    login(client, USER_EMAIL)
    body = ask(client, QUESTION).json()
    assert [(c["filename"], c["location"], c["locator"]["kind"]) for c in body["citations"]] == [(filename, "Image", "image")]
    body = ask(client, "Explain this diagram", [doc["id"]]).json()  # about the image: answered, not drawn
    assert body.get("chart") is None and body["citations"]
    image = client.get(f"/api/rag/documents/{doc['id']}/pages/1/image")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG")
    assert client.get(f"/api/rag/documents/{doc['id']}/pages/2/image").status_code == 404  # no invented pages

    login(client, USER2_EMAIL)  # no grant: neither the description nor the image is reachable
    assert not ask(client, QUESTION).json()["citations"]
    assert client.get(f"/api/rag/documents/{doc['id']}/pages/1/image").status_code == 404
    login(client, ADMIN_EMAIL)
    assert set_access(client, doc["id"], "restricted", users=[USER2_EMAIL]).status_code == 200
    login(client, USER_EMAIL)  # revoked
    assert not ask(client, QUESTION).json()["citations"]
    assert client.get(f"/api/rag/documents/{doc['id']}/pages/1/image").status_code == 404


def test_unselected_image_question_uses_latest_readable_image_without_manual_selection(client, fake_provider):
    fake_provider.vision_reply = DESCRIPTION
    fake_provider.rag_reply = lambda message: "The image shows quarterly revenue; Q3 is the highest at PKR 4.2 million [S1]."
    activate_provider(client)
    assert allow_images(client).status_code == 200
    older = _upload(client, "older-chart.png", make_chart_image("PNG")).json()
    process_all(client)
    doc = _upload(client, "latest-chart.png", make_chart_image("PNG")).json()
    process_all(client)
    assert document(client, older["id"])["status"] == "ready"

    body = ask(client, "iss image mai kiya bat ho rhi hai?").json()

    assert body["strategy"] == "whole_document"
    assert not body["insufficient_evidence"]
    assert body["answer"].startswith("The image shows quarterly revenue")
    assert [(item["filename"], item["location"]) for item in body["citations"]] == [("latest-chart.png", "Image")]


def test_visual_question_without_an_accessible_image_does_not_fabricate_content(client, fake_provider):
    activate_provider(client)

    body = ask(client, "iss image mai kiya bat ho rhi hai?").json()

    assert body["insufficient_evidence"]
    assert not body["citations"]
    assert "ready, searchable image" in body["answer"]
    assert fake_provider.calls == []


def test_an_image_is_never_sent_without_authorization_and_waits_for_a_vision_provider(client, fake_provider):
    fake_provider.vision_reply = DESCRIPTION
    activate_provider(client)
    doc = _upload(client, "diagram.png", make_chart_image()).json()
    process_all(client)
    failed = document(client, doc["id"])
    assert failed["status"] == "failed" and failed["error_code"] == "vision_unavailable"
    assert fake_provider.image_calls == [] and chunks(doc["id"]) == []

    # Authorized, but the provider times out: a transient failure, retried (here until the attempts run out).
    assert allow_images(client).status_code == 200
    fake_provider.vision_reply = LLMTimeoutError()
    assert client.post(f"/api/rag/documents/{doc['id']}/reprocess").status_code == 202
    process_all(client)
    retry = document(client, doc["id"])
    assert retry["error_code"] == "image_not_described" and retry["attempts"] > 1
    assert len(fake_provider.image_calls) == retry["attempts"]

    # The provider refuses images (no vision support): a clear permanent state, nothing indexed.
    fake_provider.vision_reply = None
    assert client.post(f"/api/rag/documents/{doc['id']}/reprocess").status_code == 202
    process_all(client)
    refused = document(client, doc["id"])
    assert refused["status"] == "failed" and refused["error_code"] == "vision_unavailable"

    fake_provider.vision_reply = DESCRIPTION  # an explicit reprocess indexes it once vision works
    assert client.post(f"/api/rag/documents/{doc['id']}/reprocess").status_code == 202
    process_all(client)
    assert document(client, doc["id"])["status"] == "ready"


def test_images_are_validated_before_they_are_decoded(client, fake_provider, monkeypatch):
    from app.core.config import get_settings

    activate_provider(client)
    png = make_chart_image("PNG")
    cases = [
        ("broken.png", png[:200], "invalid_image"),  # truncated
        ("photo.png", make_chart_image("JPEG"), "file_type_mismatch"),  # renamed JPEG
        ("photo.jpg", png, "file_type_mismatch"),
        ("tiny.png", make_chart_image("PNG", (40, 40)), "image_too_small"),
    ]
    for filename, data, code in cases:
        response = _upload(client, filename, data)
        assert response.status_code == 415, (filename, response.text)
        assert response.json()["error"]["details"]["reason"] == code, (filename, response.text)

    monkeypatch.setattr(get_settings(), "rag_image_max_megapixels", 1)  # a decompression bomb is refused by header
    response = _upload(client, "huge.png", make_chart_image("PNG", (1400, 900)))
    assert response.json()["error"]["details"]["reason"] == "image_too_large", response.text
    assert fake_provider.image_calls == []


def test_unsupported_vision_model_reports_image_documents_unavailable(client, fake_provider):
    from tests.rag_helpers import PROVIDER

    fake_provider.vision_reply = DESCRIPTION
    activate_provider(client)
    assert allow_images(client).status_code == 200
    version = client.get(PROVIDER).json()["version"]
    assert client.put(PROVIDER, json={"model": "deepseek-v4-pro", "settings": {}, "expected_version": version}).status_code == 200
    assert client.get("/api/rag/image-understanding").json()["state"] == "unsupported"
    doc = _upload(client, "scan.jpeg", make_chart_image("JPEG")).json()
    process_all(client)
    assert document(client, doc["id"])["error_code"] == "vision_unavailable"
    assert fake_provider.image_calls == []
