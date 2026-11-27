"""Form file fields: upload checks, linking on submit, who may download, and cleanup of unused uploads."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.main import app
from app.services.request_attachments import cleanup_orphans, storage
from tests.conftest import ADMIN_EMAIL, CSRF, USER2_EMAIL, USER_EMAIL, login, run_db

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _upload(client, name, content, mime="application/octet-stream"):
    return client.post("/api/service-requests/attachments", files={"file": (name, content, mime)}, headers=CSRF)


def _form(client, department_id=None):
    login(client, ADMIN_EMAIL)
    return client.post("/api/admin/request-forms", json={
        "name": "Expense receipt", "category": "facilities", "department_group_id": department_id,
        "fields": [{"label": "Receipts", "type": "file", "required": True}, {"label": "Note", "type": "text"}],
    }).json()["forms"][0]


def test_uploads_are_checked_and_linked_to_the_request_on_submit(client):
    form = _form(client)
    login(client, USER_EMAIL)
    assert _upload(client, "run.exe", b"MZ...").status_code in (400, 415, 422)
    assert _upload(client, "fake.pdf", b"not a pdf at all").status_code in (400, 415, 422)
    assert _upload(client, "photo.png", b"GIF89a....").status_code in (400, 415, 422)
    assert _upload(client, "big.pdf", PDF + b"0" * (10 * 1024 * 1024)).status_code == 413
    receipt = _upload(client, "../../Receipt March.pdf", PDF, "application/pdf")
    photo = _upload(client, "photo.png", PNG, "image/png")
    notes = _upload(client, "notes.csv", "item,amount\nTaxi,1200\n".encode(), "text/csv")
    assert receipt.status_code == photo.status_code == notes.status_code == 201, receipt.text
    receipt, photo, notes = receipt.json(), photo.json(), notes.json()
    assert receipt["filename"] == "Receipt March.pdf"
    assert client.delete(f"/api/service-requests/attachments/{notes['id']}").status_code == 204

    base = {"category": "facilities", "subject": "Taxi receipts", "form_id": form["id"]}
    assert client.post("/api/service-requests", json={**base, "answers": {"note": "x"}}).status_code == 422  # file required
    assert client.post("/api/service-requests", json={**base, "answers": {"receipts": [notes["id"]]}}).status_code == 422  # removed
    made = client.post("/api/service-requests", json={**base, "answers": {"receipts": [receipt["id"], photo["id"]], "note": "March"}})
    assert made.status_code == 201, made.text
    answer = made.json()["form_answers"][0]
    assert answer["value"] == "Receipt March.pdf, photo.png" and [a["filename"] for a in answer["attachments"]] == ["Receipt March.pdf", "photo.png"]
    again = client.post("/api/service-requests", json={**base, "subject": "Again", "answers": {"receipts": [receipt["id"]]}})
    assert again.status_code == 422  # already attached to a request

    request_id = made.json()["id"]
    download = client.get(f"/api/service-requests/{request_id}/attachments/{receipt['id']}")
    assert download.status_code == 200 and download.content == PDF
    assert download.headers["content-type"] == "application/pdf" and download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert client.get(f"/api/service-requests/{request_id}/attachments/{uuid.uuid4()}").status_code == 404

    login(client, USER2_EMAIL)  # cannot read the request, so cannot download its files
    assert client.get(f"/api/service-requests/{request_id}/attachments/{receipt['id']}").status_code == 404
    assert _upload(client, "mine.pdf", PDF).status_code == 201
    stolen = client.post("/api/service-requests", json={**base, "subject": "Not mine", "answers": {"receipts": [photo["id"]]}})
    assert stolen.status_code == 422
    login(client, ADMIN_EMAIL)
    assert client.get(f"/api/service-requests/{request_id}/attachments/{photo['id']}").content == PNG


def test_uploads_never_submitted_are_removed_after_a_day(client):
    login(client, USER_EMAIL)
    kept = _upload(client, "keep.pdf", PDF).json()
    old = _upload(client, "old.pdf", PDF).json()
    key = run_db(lambda conn: conn.scalar(text("SELECT storage_key FROM request_attachments WHERE id = :id"), {"id": uuid.UUID(old["id"])}))
    run_db(lambda conn: conn.execute(text("UPDATE request_attachments SET created_at = now() - interval '25 hours' WHERE id = :id"), {"id": uuid.UUID(old["id"])}))
    assert storage().path_for(key).exists()

    async def cleanup():
        return await cleanup_orphans(app.state.sessionmaker, now=datetime.now(UTC) + timedelta(seconds=1))

    assert client.portal.call(cleanup) == 1
    assert not storage().path_for(key).exists()
    remaining = run_db(lambda conn: conn.execute(text("SELECT id FROM request_attachments")))
    assert [str(r[0]) for r in remaining.all()] == [kept["id"]]
