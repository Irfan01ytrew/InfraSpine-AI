"""Step AM slice 6 — tests for the Foundry API routes.

Runs against real PostgreSQL (the conftest migrates TEST_DATABASE_URL to
head, including 0104_foundry_registry) and the real FastAPI app via the
conftest `client` fixture. The app is pointed at the test database by the
conftest itself.

Setup per test: an org scope, a `foundry.read` viewer (USER_EMAIL) and a
`foundry.read + foundry.manage` manager (USER2_EMAIL), and one registered
app with a preview_ready build and a blocked build.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import platform
from app.core.config import get_settings
from app.db.models import User
from app.db.session import test_engine_options as _test_engine_options
from app.foundry.deploy import deploy_action_hash
from app.foundry.models import FoundryApp, FoundryBuild
from app.foundry.spec import spec_fingerprint
from app.platform.models import PlatformApprovalRequest
from tests.conftest import (
    ADMIN_EMAIL,
    TEST_DATABASE_URL,
    USER2_EMAIL,
    USER_EMAIL,
    login,
)

pytestmark = pytest.mark.usefixtures("clean_tables")

TARGET = "codespace-demo"


def run(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _spec():
    return {
        "name": "Leave Tracker",
        "purpose": "Let employees request leave and managers approve it.",
        "pages": [
            {"name": "Requests", "path": "/requests", "endpoints": ["list_requests"]},
        ],
        "endpoints": [
            {"name": "list_requests", "method": "GET", "path": "/api/requests",
             "table": "leave_requests"},
        ],
        "tables": [
            {"name": "leave_requests",
             "columns": [{"name": "id", "type": "uuid"},
                         {"name": "status", "type": "string"}]},
        ],
    }


@pytest.fixture
def seed():
    async def go(sm):
        async with sm() as db:
            admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
            admin_p = platform.Principal(admin.id, admin.email, "user", "web", admin.auth_version)
            org = await platform.ensure_org_scope(db)
            await platform.ensure_role(db, "foundry_viewer", {"foundry.read"})
            await platform.ensure_role(db, "foundry_manager",
                                      {"foundry.read", "foundry.manage"})
            for email, role in ((USER_EMAIL, "foundry_viewer"),
                                (USER2_EMAIL, "foundry_manager")):
                user_id = await db.scalar(select(User.id).where(User.email == email))
                await platform.grant_membership(db, admin_p, user_id=user_id,
                                               scope_id=org.id, role_key=role)
            spec = _spec()
            app = FoundryApp(scope_id=org.id, slug="leave-tracker", name="Leave Tracker")
            db.add(app)
            await db.flush()
            ready = FoundryBuild(
                app_id=app.id, spec_version=1, spec_fingerprint=spec_fingerprint(spec),
                build_fingerprint="a" * 64, gate="preview_ready", preview_token="tok",
                preview_expires_in_seconds=3600, health="passed",
                tests_passed=6, tests_failed=0, spec=spec,
                test_summary={"passed": 6, "failed": 0})
            blocked = FoundryBuild(
                app_id=app.id, spec_version=2, spec_fingerprint="b" * 64,
                build_fingerprint="b" * 64, gate="blocked", preview_token="",
                preview_expires_in_seconds=0, health="pending",
                tests_passed=0, tests_failed=1, spec=spec,
                test_summary={"passed": 0, "failed": 1})
            db.add_all([ready, blocked])
            await db.commit()
            return {"org_id": str(org.id), "spec": spec}

    return run(go)


def _approval_row(action_hash):
    async def go(sm):
        async with sm() as db:
            row = await db.scalar(select(PlatformApprovalRequest).where(
                PlatformApprovalRequest.action_hash == action_hash))
            return None if row is None else {
                "status": row.status,
                "required_permission": row.required_permission,
                "action_type": row.action_type,
            }

    return run(go)


# ---------------------------------------------------------------------------
# Reads: foundry.read, 404 for non-members, 401 unauthenticated
# ---------------------------------------------------------------------------

def test_list_apps_as_viewer(client, seed):
    login(client, USER_EMAIL)
    response = client.get(f"/api/foundry/scopes/{seed['org_id']}/apps")
    assert response.status_code == 200, response.text
    (app,) = response.json()["apps"]
    assert app["slug"] == "leave-tracker"
    assert app["builds"] == 2
    assert app["latest_version"] == 2
    assert app["latest_gate"] == "blocked"
    assert app["feedback_count"] == 0


def test_list_apps_unknown_scope_is_404(client, seed):
    login(client, USER_EMAIL)
    response = client.get(f"/api/foundry/scopes/{uuid.uuid4()}/apps")
    assert response.status_code == 404


def test_list_apps_unauthenticated_is_401(client, seed):
    response = client.get(f"/api/foundry/scopes/{seed['org_id']}/apps")
    assert response.status_code == 401


def test_get_app_detail_as_viewer(client, seed):
    login(client, USER_EMAIL)
    response = client.get(f"/api/foundry/scopes/{seed['org_id']}/apps/leave-tracker")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Leave Tracker"
    assert [b["spec_version"] for b in body["builds"]] == [1, 2]
    assert body["builds"][0]["gate"] == "preview_ready"
    assert body["feedback"] == []


def test_get_unknown_app_is_404(client, seed):
    login(client, USER_EMAIL)
    response = client.get(f"/api/foundry/scopes/{seed['org_id']}/apps/nope")
    assert response.status_code == 404


def test_list_scopes_as_viewer(client, seed):
    login(client, USER_EMAIL)
    response = client.get("/api/foundry/scopes")
    assert response.status_code == 200, response.text
    scopes = response.json()["scopes"]
    assert [s["id"] for s in scopes] == [seed["org_id"]]
    assert scopes[0]["kind"] == "org"


def test_list_scopes_unauthenticated_is_401(client, seed):
    response = client.get("/api/foundry/scopes")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Record a build: foundry.manage, fingerprints must agree with the spec
# ---------------------------------------------------------------------------

def test_record_build_as_manager(client, seed, monkeypatch):
    login(client, USER2_EMAIL)
    spec = _spec()
    body = {
        "app_name": "Leave Tracker",
        "spec_version": 3,
        "spec_fingerprint": spec_fingerprint(spec),
        "build_fingerprint": "c" * 64,
        "gate": "blocked",
        "preview_token": "",
        "preview_expires_in_seconds": 0,
        "health": "pending",
        "tests_passed": 0,
        "tests_failed": 0,
        "spec": spec,
    }
    response = client.post(
        f"/api/foundry/scopes/{seed['org_id']}/apps/leave-tracker/builds", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["build_fingerprint"] == "c" * 64


def test_record_build_refuses_a_fingerprint_mismatch(client, seed):
    login(client, USER2_EMAIL)
    body = {
        "app_name": "Leave Tracker",
        "spec_version": 3,
        "spec_fingerprint": "f" * 64,
        "build_fingerprint": "c" * 64,
        "gate": "blocked",
        "spec": _spec(),
    }
    response = client.post(
        f"/api/foundry/scopes/{seed['org_id']}/apps/leave-tracker/builds", json=body)
    assert response.status_code == 422, response.text


def test_record_build_as_viewer_is_forbidden(client, seed):
    login(client, USER_EMAIL)
    body = {
        "app_name": "Leave Tracker",
        "spec_version": 3,
        "spec_fingerprint": spec_fingerprint(_spec()),
        "build_fingerprint": "c" * 64,
        "gate": "blocked",
        "spec": _spec(),
    }
    response = client.post(
        f"/api/foundry/scopes/{seed['org_id']}/apps/leave-tracker/builds", json=body)
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Deploy request: hash-bound approval, blocked build refused, no target refused
# ---------------------------------------------------------------------------

def test_deploy_request_creates_a_hash_bound_approval(client, seed, monkeypatch):
    # Settings are loaded once (lru_cache) when the app starts, so the env
    # var must be set on the cached Settings object, not in the environment.
    monkeypatch.setattr(get_settings(), "foundry_deploy_targets", TARGET)
    login(client, USER2_EMAIL)
    response = client.post(
        f"/api/foundry/scopes/{seed['org_id']}/apps/leave-tracker/deploy-request",
        json={"build_fingerprint": "a" * 64, "target": TARGET})
    assert response.status_code == 200, response.text
    body = response.json()
    expected_hash = deploy_action_hash(seed["org_id"], "leave-tracker", "a" * 64, TARGET)
    assert body["action_hash"] == expected_hash
    assert body["action_type"] == "foundry.deploy"
    row = _approval_row(expected_hash)
    assert row is not None
    assert row["status"] == "pending"
    assert row["required_permission"] == "foundry.deploy"
    detail = client.get(f"/api/foundry/scopes/{seed['org_id']}/apps/leave-tracker").json()
    ready = next(b for b in detail["builds"] if b["build_fingerprint"] == "a" * 64)
    assert ready["deploy_state"] == "requested"
    assert ready["approval_id"] == body["approval_id"]


def test_deploy_request_refuses_a_blocked_build(client, seed, monkeypatch):
    monkeypatch.setattr(get_settings(), "foundry_deploy_targets", TARGET)
    login(client, USER2_EMAIL)
    response = client.post(
        f"/api/foundry/scopes/{seed['org_id']}/apps/leave-tracker/deploy-request",
        json={"build_fingerprint": "b" * 64, "target": TARGET})
    assert response.status_code == 409, response.text


def test_deploy_request_refuses_without_a_configured_target(client, seed, monkeypatch):
    monkeypatch.setattr(get_settings(), "foundry_deploy_targets", "")
    login(client, USER2_EMAIL)
    response = client.post(
        f"/api/foundry/scopes/{seed['org_id']}/apps/leave-tracker/deploy-request",
        json={"build_fingerprint": "a" * 64, "target": TARGET})
    assert response.status_code == 422, response.text
    assert "no_deploy_target_configured" in response.text


def test_deploy_request_as_viewer_is_forbidden(client, seed, monkeypatch):
    monkeypatch.setattr(get_settings(), "foundry_deploy_targets", TARGET)
    login(client, USER_EMAIL)
    response = client.post(
        f"/api/foundry/scopes/{seed['org_id']}/apps/leave-tracker/deploy-request",
        json={"build_fingerprint": "a" * 64, "target": TARGET})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Feedback: stored, revision previewed, secrets refused
# ---------------------------------------------------------------------------

def test_feedback_with_changes_returns_a_revision_preview(client, seed):
    login(client, USER2_EMAIL)
    response = client.post(
        f"/api/foundry/scopes/{seed['org_id']}/apps/leave-tracker/feedback",
        json={"build_fingerprint": "a" * 64, "kind": "change_request",
              "note": "Add a page for new requests.",
              "changes": [
                  {"action": "add_endpoint", "name": "create_request",
                   "method": "POST", "path": "/api/requests", "table": "leave_requests"},
                  {"action": "add_page", "name": "New request",
                   "path": "/requests/new", "endpoints": ["create_request"]},
              ]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["feedback"]["kind"] == "change_request"
    assert body["revision"]["new_version"] == 2
    assert body["revision"]["status"] == "ready_for_test"
    # The live fingerprint is the previous *spec* fingerprint (slice 5),
    # not the build's file-set hash ("a"*64).
    assert body["revision"]["live_build_fingerprint"] == spec_fingerprint(seed["spec"])
    detail = client.get(f"/api/foundry/scopes/{seed['org_id']}/apps/leave-tracker").json()
    assert len(detail["feedback"]) == 1


def test_feedback_note_only_has_no_revision(client, seed):
    login(client, USER2_EMAIL)
    response = client.post(
        f"/api/foundry/scopes/{seed['org_id']}/apps/leave-tracker/feedback",
        json={"build_fingerprint": "a" * 64, "kind": "praise",
              "note": "Looks good.", "changes": []})
    assert response.status_code == 200, response.text
    assert response.json()["revision"] is None


def test_feedback_with_a_secret_is_refused(client, seed):
    login(client, USER2_EMAIL)
    response = client.post(
        f"/api/foundry/scopes/{seed['org_id']}/apps/leave-tracker/feedback",
        json={"build_fingerprint": "a" * 64, "kind": "change_request",
              "note": "Use api_key=sk-abcdefgh12345678 please.", "changes": []})
    assert response.status_code == 422, response.text


def test_feedback_on_an_unknown_build_is_404(client, seed):
    login(client, USER2_EMAIL)
    response = client.post(
        f"/api/foundry/scopes/{seed['org_id']}/apps/leave-tracker/feedback",
        json={"build_fingerprint": "f" * 64, "kind": "bug", "note": "Broken.", "changes": []})
    assert response.status_code == 404
