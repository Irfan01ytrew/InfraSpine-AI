"""Outgoing webhooks: safe destinations, signed deliveries, retries, pausing, and which events are sent."""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

from app.main import app
from app.services import webhooks
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login
from tests.test_approval_chains import _chain, _decide, _setup, _submit

PUBLIC = "https://93.184.216.34/hooks/infraspine"  # a public IP literal: no DNS lookup in tests


def _create(client, events=("request.submitted",), url=PUBLIC):
    return client.post("/api/admin/webhooks", json={"name": "ERP", "url": url, "events": list(events)})


class Receiver:
    def __init__(self, monkeypatch, status=200):
        self.calls, self.status = [], status
        monkeypatch.setattr(webhooks, "post", self.post)

    async def post(self, url, headers, body, settings=None):
        if isinstance(self.status, Exception):
            raise self.status
        self.calls.append((url, headers, body))
        return self.status


def _deliver(client, at=None):
    async def run():
        return await webhooks.deliver_due(app.state.sessionmaker, now=at or datetime.now(UTC))
    return client.portal.call(run)


def test_destinations_must_be_public_https_and_the_secret_is_shown_once(client):
    login(client, ADMIN_EMAIL)
    for bad in ("http://93.184.216.34/x", "https://10.0.0.5/x", "https://169.254.169.254/latest", "https://localhost/x", "https://user:pw@93.184.216.34/x"):
        assert _create(client, url=bad).status_code == 422, bad
    assert client.post("/api/admin/webhooks", json={"name": "ERP", "url": PUBLIC, "events": ["request.nope"]}).status_code == 422
    made = _create(client)
    assert made.status_code == 201, made.text
    assert made.json()["secret"].startswith("whsec_") and made.json()["webhooks"][0]["url"] == PUBLIC
    assert "secret" not in client.get("/api/admin/webhooks").json()
    login(client, USER_EMAIL)
    assert client.get("/api/admin/webhooks").status_code == 403


def test_request_events_are_signed_and_delivered_and_hr_requests_never_leave(client, monkeypatch):
    receiver = Receiver(monkeypatch)
    login(client, ADMIN_EMAIL)
    secret = _create(client, events=("request.submitted", "request.cancelled")).json()["secret"]
    login(client, USER_EMAIL)
    item = client.post("/api/service-requests", json={"category": "it", "subject": "VPN down", "description": "Private details"}).json()
    client.post("/api/service-requests", json={"category": "hr", "subject": "Private HR", "description": "Private"})
    client.post(f"/api/service-requests/{item['id']}/cancel")
    assert _deliver(client) == 2

    events = [json.loads(body)["event"] for _, _, body in receiver.calls]
    assert events == ["request.submitted", "request.cancelled"]  # nothing for the HR request
    url, headers, body = receiver.calls[0]
    payload = json.loads(body)
    assert url == PUBLIC and payload["data"]["request_id"] == item["id"] and payload["data"]["subject"] == "VPN down"
    assert "Private details" not in body.decode()
    ts, sig = (part.split("=", 1)[1] for part in headers["X-InfraSpine-Signature"].split(","))
    assert hmac.compare_digest(sig, hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest())
    assert headers["X-InfraSpine-Event"] == "request.submitted" and headers["X-InfraSpine-Delivery"] == payload["id"]
    assert _deliver(client) == 0  # nothing left


def test_failures_are_retried_with_backoff_and_a_failing_webhook_is_paused(client, monkeypatch):
    receiver = Receiver(monkeypatch, status=500)
    login(client, ADMIN_EMAIL)
    hook = _create(client).json()["webhooks"][0]
    client.post(f"/api/admin/webhooks/{hook['id']}/test")
    now = datetime.now(UTC)
    assert _deliver(client, now) == 0
    log = client.get(f"/api/admin/webhooks/{hook['id']}/deliveries").json()["deliveries"]
    assert (log[0]["event"], log[0]["status"], log[0]["attempts"], log[0]["response_status"]) == ("webhook.test", "retry", 1, 500)
    assert _deliver(client, now + timedelta(seconds=10)) == 0 and len(receiver.calls) == 1  # backoff: not yet
    receiver.status = 204
    assert _deliver(client, now + timedelta(seconds=31)) == 1
    log = client.get(f"/api/admin/webhooks/{hook['id']}/deliveries").json()["deliveries"]
    assert (log[0]["status"], log[0]["attempts"]) == ("sent", 2)
    assert client.post(f"/api/admin/webhooks/deliveries/{log[0]['id']}/redeliver").status_code == 200

    receiver.status = ConnectionError("down")
    for _ in range(webhooks.PAUSE_AFTER_FAILURES):
        client.post(f"/api/admin/webhooks/{hook['id']}/test")
    _deliver(client, now + timedelta(minutes=1))
    listed = client.get("/api/admin/webhooks").json()["webhooks"][0]
    assert listed["is_active"] is False and "Paused" in listed["disabled_reason"]
    resumed = client.patch(f"/api/admin/webhooks/{hook['id']}", json={"is_active": True}).json()["webhooks"][0]
    assert resumed["is_active"] and resumed["consecutive_failures"] == 0
    assert client.delete(f"/api/admin/webhooks/{hook['id']}").json()["webhooks"] == []


def test_approval_events_follow_the_chain(client, monkeypatch):
    receiver = Receiver(monkeypatch)
    finance, ids, _ = _setup(client)
    _chain(client, {"label": "Manager review", "approver_kind": "manager"})
    _create(client, events=("approval.step_pending", "request.approved", "request.rejected", "request.status_changed"))
    login(client, USER_EMAIL)
    first, second = _submit(client, finance, "20000"), _submit(client, finance, "30000")
    login(client, USER2_EMAIL)
    _decide(client, first["id"])
    _decide(client, second["id"], "reject", "No budget")
    _deliver(client)
    seen = [(json.loads(b)["event"], json.loads(b)["data"]["request_id"]) for _, _, b in receiver.calls]
    assert ("approval.step_pending", first["id"]) in seen and ("request.approved", first["id"]) in seen
    assert ("request.rejected", second["id"]) in seen and ("request.status_changed", first["id"]) in seen
    approved = next(json.loads(b) for _, _, b in receiver.calls if json.loads(b)["event"] == "request.approved")
    assert approved["data"]["decided_by"] == USER2_EMAIL and approved["data"]["amount"] == "20000.00"
