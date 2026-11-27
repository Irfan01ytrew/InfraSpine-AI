"""Persistent employee training progress remains owner-scoped and follows current document access."""

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login
from tests.rag_helpers import activate_provider, document, make_pdf, process_all, replace_file
from tests.test_access_control import set_access
from tests.test_document_formats import ready


def test_training_history_is_private_completable_and_invalidated_when_source_access_changes(client, fake_provider):
    fake_provider.rag_reply = lambda _message: "VPN tokens are rotated every 90 days [S1]."
    activate_provider(client)
    source = ready(
        client,
        "employee-training-source.pdf",
        make_pdf("VPN tokens are rotated every 90 days by the security operations team."),
        visibility="shared",
    )

    login(client, USER_EMAIL)
    created = client.post(
        "/api/rag/training",
        json={"topic": "VPN token rotation", "document_ids": [source["id"]]},
    )
    assert created.status_code == 200, created.text
    plan = created.json()
    assert plan["id"] and plan["status"] == "in_progress" and len(plan["modules"]) == 3
    assert plan["created_at"] and plan["completed_at"] is None
    assert any(module["citations"] for module in plan["modules"])

    history = client.get("/api/rag/training").json()["items"]
    assert len(history) == 1
    assert history[0]["id"] == plan["id"] and history[0]["module_count"] == 3

    login(client, USER2_EMAIL)
    assert client.get(f"/api/rag/training/{plan['id']}").status_code == 404
    assert client.post(f"/api/rag/training/{plan['id']}/complete", headers={"X-Requested-With": "test"}).status_code == 404
    assert client.get("/api/rag/training").json()["items"] == []

    login(client, ADMIN_EMAIL)
    assert set_access(client, source["id"], "private").status_code == 200
    login(client, USER_EMAIL)
    reopened = client.get(f"/api/rag/training/{plan['id']}")
    assert reopened.status_code == 200
    result = reopened.json()
    assert result["status"] == "in_progress"
    assert any(
        module["insufficient_evidence"]
        and not module["citations"]
        and "no longer available or has changed" in module["answer"]
        for module in result["modules"]
    )

    completed = client.post(
        f"/api/rag/training/{plan['id']}/complete",
        headers={"X-Requested-With": "test"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed" and completed.json()["completed_at"]
    assert client.post(
        f"/api/rag/training/{plan['id']}/complete",
        headers={"X-Requested-With": "test"},
    ).json()["status"] == "completed"


def test_training_content_is_hidden_after_its_source_is_replaced(client, fake_provider):
    fake_provider.rag_reply = lambda _message: "Rotate VPN tokens every 90 days [S1]."
    activate_provider(client)
    source = ready(
        client,
        "rotating-policy.pdf",
        make_pdf("VPN tokens are rotated every 90 days."),
        visibility="shared",
    )
    login(client, USER_EMAIL)
    plan = client.post(
        "/api/rag/training",
        json={"topic": "VPN token rotation", "document_ids": [source["id"]]},
    ).json()

    login(client, ADMIN_EMAIL)
    replacement = replace_file(
        client,
        source["id"],
        "rotating-policy.pdf",
        make_pdf("VPN tokens are rotated every 30 days."),
    )
    assert replacement.status_code == 202, replacement.text
    process_all(client)

    login(client, USER_EMAIL)
    reopened = client.get(f"/api/rag/training/{plan['id']}").json()
    assert all(
        module["insufficient_evidence"] and not module["citations"]
        for module in reopened["modules"]
    )
    assert document(client, source["id"])["version"] == 2
