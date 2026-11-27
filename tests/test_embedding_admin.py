from tests.conftest import USER_EMAIL, login


def test_embedding_model_status_is_available_to_admin_only(admin_client):
    response = admin_client.get("/api/admin/embedding-models")

    assert response.status_code == 200
    assert response.json() == {
        "model_name": "test/fake-bow-embedder",
        "dimensions": 384,
        "device": "cpu",
        "available_locally": True,
        "loaded": True,
        "status": "ready",
    }


def test_embedding_model_status_requires_authentication(client):
    assert client.get("/api/admin/embedding-models").status_code == 401
    assert client.post("/api/admin/embedding-models/test").status_code == 401


def test_embedding_model_status_rejects_regular_user(client):
    login(client, USER_EMAIL)
    assert client.get("/api/admin/embedding-models").status_code == 403
    assert client.post("/api/admin/embedding-models/test").status_code == 403


def test_embedding_model_health_action_returns_real_check_result(admin_client, fake_embedder):
    response = admin_client.post("/api/admin/embedding-models/test")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["model_name"] == fake_embedder.model_name
    assert response.json()["dimensions"] == fake_embedder.dimension
    assert response.json()["latency_ms"] >= 0
    assert "passed" in response.json()["message"]


def test_embedding_model_health_failure_does_not_expose_backend_error(admin_client, fake_embedder):
    fake_embedder.fail = True

    response = admin_client.post("/api/admin/embedding-models/test")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["message"] == "The embedding health check failed."
    assert "backend unavailable" not in response.text
