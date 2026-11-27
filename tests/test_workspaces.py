"""Client/project workspaces use the existing live group ACL for restricted documents."""

import uuid

from tests.conftest import ADMIN_EMAIL, CSRF, USER2_EMAIL, USER_EMAIL, login
from tests.rag_helpers import activate_provider, document, make_pdf, process_all


def test_project_workspace_document_acl_and_membership_revocation_are_immediate(client, fake_provider):
    login(client, ADMIN_EMAIL)
    suffix = uuid.uuid4().hex[:10]
    client_group = client.post(
        "/api/admin/access/groups", json={"name": f"Synthetic client {suffix}", "kind": "client"}
    )
    project_group = client.post(
        "/api/admin/access/groups", json={"name": f"Synthetic project {suffix}", "kind": "project"}
    )
    assert client_group.status_code == project_group.status_code == 201
    group_id = project_group.json()["id"]

    department = client.post(
        "/api/admin/access/groups", json={"name": f"Synthetic manager department {suffix}", "kind": "department"}
    )
    assert department.status_code == 201
    manager_email = f"workspace-manager-{suffix}@example.test"
    invitation = client.post(
        "/api/admin/users/invitations",
        json={"email": manager_email, "role": "manager", "department_ids": [department.json()["id"]]},
    )
    assert invitation.status_code == 201, invitation.text
    manager_password = "workspace-manager-acceptance-password"
    accepted = client.post(
        "/api/onboarding/accept",
        json={"token": invitation.json()["token"], "password": manager_password},
        headers=CSRF,
    )
    assert accepted.status_code == 200, accepted.text

    login(client, manager_email, manager_password)
    assert client.get("/api/admin/access/groups").status_code == 403
    manager_upload = client.post(
        "/api/rag/documents",
        data={"visibility": "restricted", "department_id": group_id},
        files={"file": ("forbidden-project-upload.pdf", make_pdf("Manager workspace upload must be denied."), "application/pdf")},
    )
    assert manager_upload.status_code == 403, manager_upload.text

    login(client, ADMIN_EMAIL)

    assert client.post(
        f"/api/admin/access/groups/{group_id}/members", json={"email": USER_EMAIL}
    ).status_code == 204
    uploaded = client.post(
        "/api/rag/documents",
        data={"visibility": "restricted", "department_id": group_id},
        files={"file": ("project-brief.pdf", make_pdf("Redstone contingency reserve: 271 units."), "application/pdf")},
    )
    assert uploaded.status_code == 202, uploaded.text
    document_id = uploaded.json()["id"]
    assert process_all(client) == 1
    assert document(client, document_id)["status"] == "ready"

    fake_provider.rag_reply = lambda _message: "The contingency reserve is 271 units. [S1]"
    activate_provider(client)
    login(client, USER_EMAIL)
    member_read = client.get(f"/api/rag/documents/{document_id}")
    assert member_read.status_code == 200
    answer = client.post(
        "/api/rag/chat",
        json={"question": "What is the Redstone contingency reserve?", "document_ids": [document_id]},
    )
    assert answer.status_code == 200, answer.text
    result = answer.json()
    assert result["retrieved_chunks"] > 0
    assert any(citation["document_id"] == document_id for citation in result["citations"])

    login(client, USER2_EMAIL)
    denied = client.get(f"/api/rag/documents/{document_id}")
    assert denied.status_code == 404
    refusal = client.post(
        "/api/rag/chat",
        json={"question": "What is the Redstone contingency reserve?", "document_ids": [document_id]},
    )
    assert refusal.status_code == 200, refusal.text
    assert refusal.json()["retrieved_chunks"] == 0
    assert refusal.json()["citations"] == []
    assert refusal.json()["insufficient_evidence"] is True

    login(client, ADMIN_EMAIL)
    users = client.get("/api/admin/users").json()["users"]
    member_id = next(user["id"] for user in users if user["email"] == USER_EMAIL)
    removed = client.delete(f"/api/admin/access/groups/{group_id}/members/{member_id}")
    assert removed.status_code == 204, removed.text

    login(client, USER_EMAIL)
    revoked_read = client.get(f"/api/rag/documents/{document_id}")
    assert revoked_read.status_code == 404
    revoked_answer = client.post(
        "/api/rag/chat",
        json={"question": "What is the Redstone contingency reserve?", "document_ids": [document_id]},
    )
    assert revoked_answer.status_code == 200, revoked_answer.text
    assert revoked_answer.json()["retrieved_chunks"] == 0
    assert revoked_answer.json()["citations"] == []
    assert revoked_answer.json()["insufficient_evidence"] is True



def test_client_project_workspace_hierarchy(client, fake_provider):
    login(client, ADMIN_EMAIL)
    suffix = uuid.uuid4().hex[:8]
    
    # 1. Create client workspace
    r_client = client.post(
        "/api/admin/access/groups",
        json={"name": f"Enterprise Client {suffix}", "kind": "client"},
    )
    assert r_client.status_code == 201, r_client.text
    client_id = r_client.json()["id"]

    # 2. Create project workspace under client workspace
    r_proj = client.post(
        "/api/admin/access/groups",
        json={"name": f"Client Project Alpha {suffix}", "kind": "project", "parent_id": client_id},
    )
    assert r_proj.status_code == 201, r_proj.text
    proj_id = r_proj.json()["id"]
    assert r_proj.json()["parent_id"] == client_id

    # 3. Setting a client with parent must be rejected
    r_bad_client = client.post(
        "/api/admin/access/groups",
        json={"name": f"Sub Client {suffix}", "kind": "client", "parent_id": client_id},
    )
    assert r_bad_client.status_code == 422, r_bad_client.text

    # 4. Setting non-existent parent must be rejected with 404
    r_bad_parent = client.post(
        "/api/admin/access/groups",
        json={"name": f"Orphan Project {suffix}", "kind": "project", "parent_id": str(uuid.uuid4())},
    )
    assert r_bad_parent.status_code == 404, r_bad_parent.text

    # 5. Setting self as parent via PATCH must be rejected
    r_self_parent = client.patch(
        f"/api/admin/access/groups/{proj_id}",
        json={"parent_id": proj_id},
    )
    assert r_self_parent.status_code == 422, r_self_parent.text

    # A client is not itself a member of each child project; document access stays explicit.
    assert client.post(
        f"/api/admin/access/groups/{client_id}/members", json={"email": USER_EMAIL}
    ).status_code == 204
    assert client.get(f"/api/admin/access/groups/{proj_id}/members").json()["members"] == []

    # Parent deletion must preserve child projects. Reparenting and explicit unlinking are audited updates.
    assert client.delete(f"/api/admin/access/groups/{client_id}").status_code == 409
    r_second_client = client.post(
        "/api/admin/access/groups",
        json={"name": f"Enterprise Client Two {suffix}", "kind": "client"},
    )
    assert r_second_client.status_code == 201, r_second_client.text
    second_client_id = r_second_client.json()["id"]
    r_reparent = client.patch(
        f"/api/admin/access/groups/{proj_id}",
        json={"parent_id": second_client_id},
    )
    assert r_reparent.status_code == 200, r_reparent.text
    assert r_reparent.json()["parent_id"] == second_client_id
    r_unlink = client.patch(
        f"/api/admin/access/groups/{proj_id}",
        json={"parent_id": None},
    )
    assert r_unlink.status_code == 200, r_unlink.text
    assert r_unlink.json()["parent_id"] is None
    assert client.delete(f"/api/admin/access/groups/{client_id}").status_code == 204
    assert any(group["id"] == proj_id for group in client.get("/api/admin/access/groups").json()["groups"])

    # Directory clients cannot become parents of local workspaces.
    r_directory_client = client.post(
        "/api/admin/access/groups",
        json={"name": f"Directory Client {suffix}", "kind": "client", "external_id": f"directory-{suffix}"},
    )
    assert r_directory_client.status_code == 201, r_directory_client.text
    r_directory_parent = client.post(
        "/api/admin/access/groups",
        json={
            "name": f"Directory Parent Project {suffix}",
            "kind": "project",
            "parent_id": r_directory_client.json()["id"],
        },
    )
    assert r_directory_parent.status_code == 422, r_directory_parent.text


def test_workspace_ownership_and_lifecycle_administration(client, fake_provider):
    login(client, ADMIN_EMAIL)
    suffix = uuid.uuid4().hex[:8]

    # Get admin user ID
    users = client.get("/api/admin/users").json()["users"]
    admin_id = next(u["id"] for u in users if u["email"] == ADMIN_EMAIL)

    # Create workspace with owner
    r_ws = client.post(
        "/api/admin/access/groups",
        json={"name": f"Lifecycle Project {suffix}", "kind": "project", "owner_id": admin_id},
    )
    assert r_ws.status_code == 201, r_ws.text
    ws_id = r_ws.json()["id"]
    assert r_ws.json()["owner_id"] == admin_id
    assert r_ws.json()["lifecycle_status"] == "active"

    # Archive the workspace via PATCH
    r_archive = client.patch(
        f"/api/admin/access/groups/{ws_id}",
        json={"lifecycle_status": "archived"},
    )
    assert r_archive.status_code == 200, r_archive.text
    assert r_archive.json()["lifecycle_status"] == "archived"

    # Uploading to archived workspace must fail with 409 Conflict
    r_up_archived = client.post(
        "/api/rag/documents",
        data={"visibility": "restricted", "department_id": ws_id},
        files={"file": ("test.pdf", make_pdf("Upload to archived"), "application/pdf")},
    )
    assert r_up_archived.status_code == 409, r_up_archived.text
    assert "Cannot upload to an archived workspace" in r_up_archived.text

    # Reactivate the workspace
    r_reactivate = client.patch(
        f"/api/admin/access/groups/{ws_id}",
        json={"lifecycle_status": "active"},
    )
    assert r_reactivate.status_code == 200, r_reactivate.text
    assert r_reactivate.json()["lifecycle_status"] == "active"

    # Uploading now succeeds
    r_up_active = client.post(
        "/api/rag/documents",
        data={"visibility": "restricted", "department_id": ws_id},
        files={"file": ("active.pdf", make_pdf("Active upload"), "application/pdf")},
    )
    assert r_up_active.status_code == 202, r_up_active.text


def test_workspace_summary_customer_reporting(client, fake_provider):
    login(client, ADMIN_EMAIL)
    suffix = uuid.uuid4().hex[:8]

    # Create client workspace
    r_client = client.post(
        "/api/admin/access/groups",
        json={"name": f"Reporting Client {suffix}", "kind": "client"},
    )
    client_id = r_client.json()["id"]

    # Create child project
    r_proj = client.post(
        "/api/admin/access/groups",
        json={"name": f"Reporting Project {suffix}", "kind": "project", "parent_id": client_id},
    )
    proj_id = r_proj.json()["id"]

    # Add member to child project
    client.post(f"/api/admin/access/groups/{proj_id}/members", json={"email": USER_EMAIL})

    # Upload document to project
    r_up = client.post(
        "/api/rag/documents",
        data={"visibility": "restricted", "department_id": proj_id},
        files={"file": ("report_doc.pdf", make_pdf("Content for summary"), "application/pdf")},
    )
    assert r_up.status_code == 202
    assert process_all(client) == 1

    # Request client summary report: aggregates across child project
    r_summary = client.get(f"/api/admin/access/groups/{client_id}/summary")
    assert r_summary.status_code == 200, r_summary.text
    summary = r_summary.json()
    assert summary["group_id"] == client_id
    assert summary["kind"] == "client"
    assert summary["child_project_count"] == 1
    assert summary["total_documents"] >= 1
    assert summary["total_bytes"] > 0
