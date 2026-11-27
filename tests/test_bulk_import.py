from pathlib import Path

from app.core.config import get_settings
from app.main import app
from app.rag.bulk_import import import_folder
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login
from tests.rag_helpers import process_all


def test_folder_import_resumes_versions_changes_and_keeps_department_acl(client, tmp_path: Path):
    login(client, ADMIN_EMAIL)
    group = client.post("/api/admin/access/groups", json={"name": "Bulk Import Dept", "kind": "department"})
    assert group.status_code == 201, group.text
    department_id = group.json()["id"]
    assert client.post(
        f"/api/admin/access/groups/{department_id}/members", json={"email": USER2_EMAIL}
    ).status_code == 204

    (tmp_path / "policy.txt").write_text("Department only: the archive key is bluebird.", encoding="utf-8")
    (tmp_path / "copy.txt").write_text("Department only: the archive key is bluebird.", encoding="utf-8")
    sessionmaker = app.state.sessionmaker
    settings = get_settings()

    async def run_import():
        return await import_folder(
            sessionmaker,
            settings,
            tmp_path,
            admin_email=ADMIN_EMAIL,
            visibility="restricted",
            department_id=department_id,
            source_id="bulk-resume-test",
        )

    first = client.portal.call(run_import)
    assert first["scanned"] == 2 and first["queued"] == 2 and first["duplicates"] == 1

    # The repeated scan is resumable and treats the same bytes at the same source path as unchanged.
    second = client.portal.call(run_import)
    assert second["queued"] == 0 and second["unchanged"] == 2

    process_all(client)
    client.cookies.clear()
    login(client, USER2_EMAIL)
    docs = client.get("/api/rag/documents").json()["documents"]
    imported = [doc for doc in docs if doc["filename"] in {"policy.txt", "copy.txt"}]
    assert len(imported) == 2
    changed_doc = next(doc for doc in imported if doc["filename"] == "policy.txt")

    (tmp_path / "policy.txt").write_text("Department only: the archive key is robin.", encoding="utf-8")
    client.cookies.clear()
    login(client, ADMIN_EMAIL)
    third = client.portal.call(run_import)
    assert third["queued"] == 1 and third["unchanged"] == 1
    current = client.get(f"/api/rag/documents/{changed_doc['id']}").json()
    assert current["version"] == 2 and current["status"] == "pending"
    history = client.get(f"/api/rag/documents/{changed_doc['id']}/versions").json()
    assert len(history) == 1 and history[0]["reason"] == "folder_sync"

    progress = client.get("/api/rag/imports/folders/bulk-resume-test")
    assert progress.status_code == 200
    assert progress.json()["total_documents"] == 2
    assert progress.json()["queued"] == 1

    client.cookies.clear()
    login(client, USER_EMAIL)
    assert client.get(f"/api/rag/documents/{changed_doc['id']}").status_code == 404
