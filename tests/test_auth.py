import hashlib

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, PASSWORD, USER_EMAIL, login, run_db


def test_login_sets_hardened_session_cookie(client):
    response = login(client)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    assert response.json()["role"] == "admin"
    token = client.cookies.get("infraspine_session")
    assert token and token not in response.text


def test_session_token_is_stored_hashed(client):
    login(client)
    token = client.cookies.get("infraspine_session")
    stored = run_db(lambda c: c.scalar(text("SELECT token_hash FROM user_sessions")))
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    assert stored != token


def test_single_wrong_password_does_not_lock(client):
    response = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong-password"})
    assert response.json()["error"] == {"code": "invalid_login", "message": "Invalid email or password."}
    login(client)  # correct password still works right after


def test_wrong_password_and_unknown_email_get_identical_errors(client):
    a = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong-password"})
    b = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "wrong-password"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_account_locks_after_repeated_failures(client):
    for _ in range(5):
        client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong-password"})
    # Even the correct password is refused while locked.
    response = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "account_locked"
    locked = run_db(lambda c: c.scalar(text("SELECT locked_until IS NOT NULL FROM users WHERE email = :e"), {"e": ADMIN_EMAIL}))
    assert locked


def test_logout_revokes_the_server_side_session(client):
    login(client)
    token = client.cookies.get("infraspine_session")
    assert client.post("/api/auth/logout").status_code == 204
    client.cookies.set("infraspine_session", token)  # replay the old cookie
    assert client.get("/api/auth/me").status_code == 401


def test_state_changing_requests_require_csrf_header(client):
    no_header = {"X-Requested-With": ""}
    response = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}, headers=no_header)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_rejected"

    login(client)
    response = client.put(
        "/api/admin/providers/deepseek",
        json={"model": "deepseek-chat", "settings": {}, "expected_version": 0},
        headers=no_header,
    )
    assert response.status_code == 403


def test_unauthenticated_requests_are_rejected(client):
    assert client.get("/api/admin/providers").status_code == 401
    assert client.get("/api/admin/providers/catalog").status_code == 401
    assert client.post("/api/ai/chat", json={"message": "hi"}).status_code == 401


def test_non_admin_cannot_use_any_admin_endpoint(client):
    login(client, USER_EMAIL)
    checks = [
        client.get("/api/admin/providers/catalog"),
        client.get("/api/admin/providers"),
        client.get("/api/admin/providers/deepseek"),
        client.put("/api/admin/providers/deepseek", json={"model": "deepseek-chat", "settings": {}, "expected_version": 0}),
        client.post("/api/admin/providers/deepseek/test", json={}),
        client.post("/api/admin/providers/deepseek/credentials", json={"api_key": "x" * 20, "expected_version": 1}),
        client.post("/api/admin/providers/deepseek/activate", json={"expected_version": 1}),
        client.post("/api/admin/providers/deepseek/deactivate", json={"expected_version": 1}),
        client.get("/api/admin/audit-logs"),
    ]
    assert [r.status_code for r in checks] == [403] * len(checks)
    # Regular users can still use chat (no provider is configured yet).
    assert client.post("/api/ai/chat", json={"message": "hi"}).json()["error"]["code"] == "llm_not_configured"


def test_login_attempts_are_audited(client):
    client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong-password"})
    login(client)
    rows = run_db(lambda c: c.execute(text("SELECT action, outcome FROM audit_logs ORDER BY id")))
    assert [tuple(r) for r in rows.all()] == [("auth.login", "failure"), ("auth.login", "success")]


def test_cli_set_password_clears_lockout_and_revokes_sessions(client):
    import asyncio

    from app import cli

    login(client)
    old_token = client.cookies.get("infraspine_session")
    for _ in range(5):
        client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong-password"})

    asyncio.run(cli.set_password(ADMIN_EMAIL, "brand-new-pass"))

    client.cookies.set("infraspine_session", old_token)
    assert client.get("/api/auth/me").status_code == 401  # old session signed out
    login(client, ADMIN_EMAIL, "brand-new-pass")  # lockout cleared, new password works
