"""DB-free unit tests for the Step V Gmail connector class (slice 3).

No database, no network, no real credentials: every external call goes
through ``MockTransport`` with canned responses. Gmail send is approval-gated
— a send without a live approval covering the exact action never reaches
the provider.
"""

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.integrations import adapters
from app.integrations.gmail import (
    ApprovalRequiredError,
    GmailAuthError,
    GmailConnector,
    GmailRateLimitedError,
)
from app.integrations.registry import ConnectorRegistry, MockTransport, RegistryError
from app.integrations.security import digest


def _gmail_approval(to, subject, text, *, expired=False):
    """An approval digest-covering exactly this validated send action."""
    connector = SimpleNamespace(
        provider="gmail",
        config={"scopes": ["https://www.googleapis.com/auth/gmail.send"]},
    )
    validated = adapters.validate_operation(
        connector, "email.send", {"to": to, "subject": subject, "text": text}
    )
    delta = timedelta(minutes=-1) if expired else timedelta(hours=1)
    return {
        "digest": digest({"operation": "email.send", "payload": validated}),
        "expires_at": datetime.now(UTC) + delta,
    }


def test_gmail_verify_returns_mailbox_and_maps_auth_error():
    async def run():
        transport = MockTransport()
        transport.when(
            "/users/me/profile",
            {"emailAddress": "owner@example.com", "messagesTotal": 42, "threadsTotal": 7},
        )
        connector = GmailConnector(transport)
        assert await connector.verify() == {
            "ok": True,
            "emailAddress": "owner@example.com",
            "messagesTotal": 42,
        }
        denied = MockTransport()
        denied.when("/users/me/profile", {"error": {"code": 401, "message": "invalid_grant: token revoked"}})
        with pytest.raises(GmailAuthError):
            await GmailConnector(denied).verify()
        broken = MockTransport()
        broken.when("/users/me/profile", {"nope": True})
        with pytest.raises(Exception):
            await GmailConnector(broken).verify()

    asyncio.run(run())


def test_gmail_list_messages_returns_ids_and_token():
    async def run():
        transport = MockTransport()
        transport.when(
            "/users/me/messages?maxResults=2",
            {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "tok-1"},
        )
        connector = GmailConnector(transport)
        result = await connector.list_messages(max_results=2)
        assert result == {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "tok-1"}
        with pytest.raises(Exception):
            await connector.list_messages(max_results=0)
        with pytest.raises(Exception):
            await connector.list_messages(max_results=101)
        assert len(transport.calls) == 1

    asyncio.run(run())


def test_gmail_list_quota_error_is_mapped():
    async def run():
        transport = MockTransport()
        transport.when(
            "/users/me/messages?maxResults=10",
            {"error": {"code": 429, "message": "Quota exceeded for quota metric 'Queries'"}},
        )
        with pytest.raises(GmailRateLimitedError):
            await GmailConnector(transport).list_messages()

    asyncio.run(run())


def test_gmail_send_without_approval_never_calls_provider():
    async def run():
        transport = MockTransport()
        transport.when("/users/me/messages/send", {"id": "sent-1"})
        connector = GmailConnector(transport)
        with pytest.raises(ApprovalRequiredError):
            await connector.send_message(["a@example.com"], "Hi", "body")
        with pytest.raises(ApprovalRequiredError):
            await connector.send_message(["a@example.com"], "Hi", "body", approval={"digest": "wrong"})
        assert transport.calls == []

    asyncio.run(run())


def test_gmail_send_rejects_wrong_digest_and_expired_approval():
    async def run():
        transport = MockTransport()
        transport.when("/users/me/messages/send", {"id": "sent-1"})
        connector = GmailConnector(transport)
        good = _gmail_approval(["a@example.com"], "Hi", "body")
        stale = _gmail_approval(["a@example.com"], "Hi", "body", expired=True)
        other = _gmail_approval(["b@example.com"], "Hi", "body")
        with pytest.raises(ApprovalRequiredError):
            await connector.send_message(["a@example.com"], "Hi", "body", approval=other)
        with pytest.raises(ApprovalRequiredError):
            await connector.send_message(["a@example.com"], "Hi", "body", approval=stale)
        assert transport.calls == []
        result = await connector.send_message(["a@example.com"], "Hi", "body", approval=good)
        assert result == {"ok": True, "id": "sent-1"}

    asyncio.run(run())


def test_gmail_send_with_valid_approval_posts_raw_message():
    async def run():
        transport = MockTransport()
        transport.when("/users/me/messages/send", {"id": "sent-42"})
        connector = GmailConnector(transport)
        approval = _gmail_approval(["a@example.com", "b@example.com"], "Invoice 7", "Please review.")
        result = await connector.send_message(
            ["a@example.com", "b@example.com"], "Invoice 7", "Please review.", approval=approval
        )
        assert result == {"ok": True, "id": "sent-42"}
        assert len(transport.calls) == 1
        call = transport.calls[0]
        assert call["method"] == "POST" and call["path"] == "/users/me/messages/send"
        raw = base64.urlsafe_b64decode(call["payload"]["raw"]).decode()
        assert "a@example.com" in raw and "b@example.com" in raw
        assert "Invoice 7" in raw and "Please review." in raw

    asyncio.run(run())


def test_gmail_send_maps_quota_and_auth_errors():
    async def run():
        approval = _gmail_approval(["a@example.com"], "Hi", "body")
        quota = MockTransport()
        quota.when(
            "/users/me/messages/send",
            {"error": {"code": 429, "message": "User-rate limit exceeded"}},
        )
        with pytest.raises(GmailRateLimitedError):
            await GmailConnector(quota).send_message(["a@example.com"], "Hi", "body", approval=approval)
        denied = MockTransport()
        denied.when(
            "/users/me/messages/send",
            {"error": {"code": 401, "message": "invalid_grant: Bad Request"}},
        )
        with pytest.raises(GmailAuthError):
            await GmailConnector(denied).send_message(["a@example.com"], "Hi", "body", approval=approval)
        assert len(quota.calls) == 1 and len(denied.calls) == 1

    asyncio.run(run())


def test_gmail_send_validates_recipients_and_headers():
    async def run():
        transport = MockTransport()
        transport.when("/users/me/messages/send", {"id": "sent-1"})
        connector = GmailConnector(transport)
        approval = _gmail_approval(["a@example.com"], "Hi", "body")
        with pytest.raises(Exception):
            await connector.send_message(["not-an-email"], "Hi", "body", approval=approval)
        with pytest.raises(Exception):
            await connector.send_message([], "Hi", "body", approval=approval)
        with pytest.raises(Exception):
            await connector.send_message(["a@example.com"], "Bad\r\nSubject", "body", approval=approval)
        with pytest.raises(Exception):
            await connector.send_message(["a@example.com"], "Hi", "x" * 20001, approval=approval)
        assert transport.calls == []

    asyncio.run(run())


def test_gmail_receive_event_is_untrusted_and_drops_hostile_fields():
    connector = GmailConnector(MockTransport())
    event = connector.receive_event({
        "event_id": "m1",
        "user": "owner@example.com",
        "text": "invoice attached",
        "ts": "1700000000",
        "type": "message",
        "role": "admin",
        "approve": True,
        "payee": "attacker",
        "amount": 5000,
    })
    assert event["provider"] == "gmail"
    assert event["trusted"] is False
    assert event["text"] == "invoice attached"
    for forbidden in ("role", "approve", "payee", "amount"):
        assert forbidden not in event


def test_registry_gmail_end_to_end_via_mock():
    async def run():
        registry = ConnectorRegistry()
        transport = MockTransport()
        transport.when("/users/me/profile", {"emailAddress": "owner@example.com", "messagesTotal": 1})
        registry.register("gmail", GmailConnector(transport))
        connector = registry.resolve("gmail")
        result = await connector.verify()
        assert result["emailAddress"] == "owner@example.com"
        assert registry.registered() == ["gmail"]
        with pytest.raises(RegistryError):
            registry.register("gmail", GmailConnector(transport))

    asyncio.run(run())
