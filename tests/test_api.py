
from app.api.deps import get_llm_runtime
from app.core.errors import LLMTimeoutError
from app.main import app
from app.schemas.chat import MAX_MESSAGE_LENGTH


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["llm_mode"] == "database"
    assert "X-Request-ID" in response.headers


def test_readiness_checks_database(client):
    body = client.get("/health/ready").json()
    assert body["status"] == "ready" and body["database"] == "ok"
    assert body["ingestion"]["mode"] == "embedded"
    assert body["ingestion"]["status"] in {"available", "waiting_for_worker"}
    assert isinstance(body["ingestion"]["workers_online"], int)


def test_security_headers(client):
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert client.get("/api/auth/me").headers["Cache-Control"] == "no-store"


def test_malicious_request_id_is_replaced(client):
    response = client.get("/health", headers={"X-Request-ID": "bad id\nInjected: yes"})
    assert response.headers["X-Request-ID"] != "bad id\nInjected: yes"


def test_chat_without_active_provider_returns_503(admin_client):
    response = admin_client.post("/api/ai/chat", json={"message": "Hello"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_not_configured"


def test_chat_validation(admin_client):
    assert admin_client.post("/api/ai/chat", json={"message": "   "}).status_code == 422
    assert admin_client.post("/api/ai/chat", json={}).status_code == 422
    assert admin_client.post("/api/ai/chat", json={"message": "hi", "role": "admin"}).status_code == 422
    assert admin_client.post("/api/ai/chat", json={"message": "x" * (MAX_MESSAGE_LENGTH + 1)}).status_code == 422


def test_validation_error_does_not_echo_input(admin_client):
    secret = "my-password-123" + "x" * MAX_MESSAGE_LENGTH
    response = admin_client.post("/api/ai/chat", json={"message": secret})
    assert "my-password-123" not in response.text


def test_api_key_validation_error_does_not_echo_key(admin_client):
    response = admin_client.post(
        "/api/admin/providers/deepseek/credentials", json={"api_key": "sk-short", "expected_version": 1}
    )
    assert response.status_code == 422
    assert "sk-short" not in response.text


def test_llm_errors_map_to_clean_responses(admin_client):
    class TimingOutClient:
        async def chat(self, message):
            raise LLMTimeoutError()

    class FakeRuntime:
        async def get_client(self, db):
            return TimingOutClient()

    app.dependency_overrides[get_llm_runtime] = lambda: FakeRuntime()
    try:
        response = admin_client.post("/api/ai/chat", json={"message": "hi"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 504
    assert response.json() == {"error": {"code": "llm_timeout", "message": LLMTimeoutError.message}}
