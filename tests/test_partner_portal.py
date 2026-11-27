"""PostgreSQL/API acceptance for external partner identity and corpus isolation."""

import hashlib

from sqlalchemy import text

from app.core.security import PARTNER_SESSION_COOKIE_NAME, hash_session_token
from tests.fake_llm import MockLLMClient
from tests.conftest import ADMIN_EMAIL, CSRF, USER_EMAIL, login, run_db


class MockPartnerRuntime:
    async def get_client(self, db):
        return MockLLMClient()


def _partner_login(client, workspace_slug: str, email: str, password: str):
    return client.post(
        "/api/partner/auth/login",
        json={"workspace_slug": workspace_slug, "email": email, "password": password},
        headers=CSRF,
    )


def test_partner_text_file_upload_is_bounded_and_retrievable_only_in_its_workspace(client, monkeypatch):
    import app.api.routes.partner_portal as partner_routes

    monkeypatch.setattr(partner_routes, "get_llm_runtime", lambda request: MockPartnerRuntime())
    login(client, ADMIN_EMAIL)
    workspace = client.post(
        "/api/admin/partner-workspaces", json={"slug": "file-import-partner", "name": "File Import Partner"}
    ).json()
    workspace_id = workspace["id"]
    content = "Acme dispatch service is available weekdays from 08:30 to 16:30 UTC."
    uploaded = client.post(
        f"/api/admin/partner-workspaces/{workspace_id}/sources/upload",
        files={"file": (r"C:\fakepath\dispatch-hours.md", content.encode("utf-8"), "text/markdown")},
    )
    assert uploaded.status_code == 201, uploaded.text
    source = uploaded.json()
    assert source["title"] == "dispatch-hours"
    assert source["content_sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    source_list = client.get(f"/api/admin/partner-workspaces/{workspace_id}/sources")
    assert source_list.status_code == 200
    assert source_list.json()[0]["id"] == source["id"]
    assert "content" not in source_list.json()[0]

    rejected_extension = client.post(
        f"/api/admin/partner-workspaces/{workspace_id}/sources/upload",
        files={"file": ("dispatch-hours.pdf", b"not a supported format", "application/pdf")},
    )
    assert rejected_extension.status_code == 415
    rejected_encoding = client.post(
        f"/api/admin/partner-workspaces/{workspace_id}/sources/upload",
        files={"file": ("bad-encoding.txt", b"bad\xfftext", "text/plain")},
    )
    assert rejected_encoding.status_code == 422
    oversized = client.post(
        f"/api/admin/partner-workspaces/{workspace_id}/sources/upload",
        files={"file": ("oversized.txt", b"x" * 200_001, "text/plain")},
    )
    assert oversized.status_code == 422

    invitation = client.post(
        f"/api/admin/partner-workspaces/{workspace_id}/invitations", json={"email": "file-reader@example.test"}
    ).json()
    accepted = client.post(
        "/api/partner/auth/accept",
        json={"token": invitation["token"], "password": "Partner!file-reader-123"},
    )
    assert accepted.status_code == 200, accepted.text
    assert _partner_login(client, workspace["slug"], "file-reader@example.test", "Partner!file-reader-123").status_code == 200
    answer = client.post("/api/partner/assistant/ask", json={"question": "When is Acme dispatch service available?"})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["insufficient_evidence"] is False
    assert "08:30 to 16:30 UTC" in body["answer"]
    assert [citation["source_record_id"] for citation in body["citations"]] == [source["id"]]


def test_partner_identity_cookie_and_corpus_are_tenant_isolated(client, monkeypatch):
    import app.api.routes.partner_portal as partner_routes

    monkeypatch.setattr(partner_routes, "get_llm_runtime", lambda request: MockPartnerRuntime())
    login(client, ADMIN_EMAIL)

    invalid_slug = client.post(
        "/api/admin/partner-workspaces", json={"slug": "../internal", "name": "Unsafe workspace"}
    )
    assert invalid_slug.status_code == 422
    acme = client.post(
        "/api/admin/partner-workspaces", json={"slug": "acme-partner", "name": "Acme Partner"}
    )
    beta = client.post(
        "/api/admin/partner-workspaces", json={"slug": "beta-partner", "name": "Beta Partner"}
    )
    assert acme.status_code == beta.status_code == 201
    acme_id, beta_id = acme.json()["id"], beta.json()["id"]

    acme_invite = client.post(
        f"/api/admin/partner-workspaces/{acme_id}/invitations",
        json={"email": "Partner@Example.test"},
    )
    beta_invite = client.post(
        f"/api/admin/partner-workspaces/{beta_id}/invitations",
        json={"email": "partner@example.test"},
    )
    assert acme_invite.status_code == beta_invite.status_code == 201
    acme_token, beta_token = acme_invite.json()["token"], beta_invite.json()["token"]
    assert acme_token and beta_token and acme_token != beta_token
    invite_id = acme_invite.json()["id"]
    async def _stored_hashes(conn):
        result = await conn.execute(text("SELECT token_hash FROM partner_invitations ORDER BY email, workspace_id"))
        return result.scalars().all()

    stored_hashes = run_db(_stored_hashes)
    assert hash_session_token(acme_token) in stored_hashes
    assert hash_session_token(beta_token) in stored_hashes
    assert acme_token not in stored_hashes and beta_token not in stored_hashes
    listed_invites = client.get(f"/api/admin/partner-workspaces/{acme_id}/invitations")
    assert listed_invites.status_code == 200
    assert listed_invites.json()[0]["token"] is None

    accept_acme = client.post(
        "/api/partner/auth/accept",
        json={"token": acme_token, "password": "Partner!password-123"},
    )
    accept_beta = client.post(
        "/api/partner/auth/accept",
        json={"token": beta_token, "password": "Partner!password-456"},
    )
    assert accept_acme.status_code == accept_beta.status_code == 200
    assert accept_acme.json()["workspace_slug"] == "acme-partner"
    assert accept_beta.json()["workspace_slug"] == "beta-partner"
    assert accept_acme.json()["id"] != accept_beta.json()["id"]
    assert client.post(
        "/api/partner/auth/accept", json={"token": acme_token, "password": "Partner!password-789"}
    ).status_code == 401

    source_acme = client.post(
        f"/api/admin/partner-workspaces/{acme_id}/sources",
        json={
            "title": "Acme support contacts",
            "content": "Acme delivery support uses the email acme-help@example.test. Contact customer success for help.",
        },
    )
    source_beta = client.post(
        f"/api/admin/partner-workspaces/{beta_id}/sources",
        json={
            "title": "Beta onboarding details",
            "content": "Beta customer launch reference is BETA-9137. This value is only for the Beta workspace.",
        },
    )
    assert source_acme.status_code == source_beta.status_code == 201
    assert source_acme.json()["content_sha256"] != source_beta.json()["content_sha256"]
    assert "content" not in source_acme.json()

    acme_login = _partner_login(client, "acme-partner", "PARTNER@example.test", "Partner!password-123")
    assert acme_login.status_code == 200, acme_login.text
    cookie = client.cookies.get(PARTNER_SESSION_COOKIE_NAME)
    assert cookie and cookie not in acme_login.text
    set_cookie = acme_login.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie and "path=/api/partner" in set_cookie
    assert client.get("/api/partner/auth/me").json()["workspace_slug"] == "acme-partner"

    # A partner-only browser session cannot authenticate against the internal app session boundary.
    client.cookies.delete("infraspine_session")
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/knowledge").status_code == 401

    answer = client.post(
        "/api/partner/assistant/ask",
        json={"question": "What email does Acme delivery support use?"},
    )
    assert answer.status_code == 200, answer.text
    answer_json = answer.json()
    assert answer_json["insufficient_evidence"] is False
    assert "acme-help@example.test" in answer_json["answer"]
    assert len(answer_json["citations"]) == 1
    assert answer_json["citations"][0]["source_record_id"] == source_acme.json()["id"]

    # A question about another tenant gets no source even though the answer exists in its corpus.
    cross_workspace = client.post(
        "/api/partner/assistant/ask", json={"question": "What is the BETA-9137 launch reference?"}
    )
    assert cross_workspace.status_code == 200, cross_workspace.text
    assert cross_workspace.json()["insufficient_evidence"] is True
    assert cross_workspace.json()["retrieved_sources"] == 0
    assert cross_workspace.json()["citations"] == []
    assert "BETA-9137" not in cross_workspace.json()["answer"]

    # The audit records counts/IDs but never the partner question or answer content.
    async def _partner_audit(conn):
        result = await conn.execute(
            text("SELECT details::text FROM audit_logs WHERE action='partner.assistant_query'")
        )
        return result.scalars().all()

    audit_rows = run_db(_partner_audit)
    assert len(audit_rows) == 2
    assert all("Acme delivery support" not in row and "BETA-9137" not in row for row in audit_rows)

    # An internal session may manage the workspaces but is not a partner assistant identity.
    login(client, ADMIN_EMAIL)
    client.cookies.delete(PARTNER_SESSION_COOKIE_NAME)
    assert client.get("/api/partner/auth/me").status_code == 401
    login(client, USER_EMAIL)
    assert client.post(
        "/api/admin/partner-workspaces", json={"slug": "employee-created", "name": "Denied"}
    ).status_code == 403

    # Workspace suspension revokes the external session and immediately blocks subsequent requests.
    login(client, ADMIN_EMAIL)
    _partner_login(client, "acme-partner", "partner@example.test", "Partner!password-123")
    suspended = client.patch(f"/api/admin/partner-workspaces/{acme_id}/status", json={"status": "suspended"})
    assert suspended.status_code == 200
    assert client.get("/api/partner/auth/me").status_code == 401
    assert client.patch(f"/api/admin/partner-workspaces/{acme_id}/status", json={"status": "active"}).status_code == 200
    assert client.get("/api/partner/auth/me").status_code == 401


def test_partner_invitation_revocation_and_identity_deactivation_revoke_sessions(client):
    login(client, ADMIN_EMAIL)
    workspace = client.post(
        "/api/admin/partner-workspaces", json={"slug": "lifecycle-partner", "name": "Lifecycle Partner"}
    ).json()
    workspace_id = workspace["id"]
    revoked_invite = client.post(
        f"/api/admin/partner-workspaces/{workspace_id}/invitations", json={"email": "revoked@example.test"}
    ).json()
    assert client.delete(
        f"/api/admin/partner-workspaces/{workspace_id}/invitations/{revoked_invite['id']}"
    ).status_code == 204
    rejected = client.post(
        "/api/partner/auth/accept",
        json={"token": revoked_invite["token"], "password": "Partner!password-234"},
    )
    assert rejected.status_code == 401

    invite = client.post(
        f"/api/admin/partner-workspaces/{workspace_id}/invitations", json={"email": "active@example.test"}
    ).json()
    accepted = client.post(
        "/api/partner/auth/accept", json={"token": invite["token"], "password": "Partner!password-345"}
    )
    assert accepted.status_code == 200
    partner_login = _partner_login(client, "lifecycle-partner", "active@example.test", "Partner!password-345")
    assert partner_login.status_code == 200
    assert client.post("/api/partner/auth/logout", headers=CSRF).status_code == 204
    assert client.get("/api/partner/auth/me").status_code == 401
    partner_login = _partner_login(client, "lifecycle-partner", "active@example.test", "Partner!password-345")
    assert partner_login.status_code == 200
    identity_id = partner_login.json()["id"]
    identities = client.get(f"/api/admin/partner-workspaces/{workspace_id}/identities")
    assert identities.status_code == 200
    assert len(identities.json()) == 1 and identities.json()[0]["id"] == identity_id

    deactivated = client.patch(
        f"/api/admin/partner-workspaces/{workspace_id}/identities/{identity_id}/active",
        json={"active": False},
    )
    assert deactivated.status_code == 200 and deactivated.json()["is_active"] is False
    assert client.get("/api/partner/auth/me").status_code == 401
