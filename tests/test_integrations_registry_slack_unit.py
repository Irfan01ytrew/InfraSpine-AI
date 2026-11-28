"""DB-free unit tests for the Step V connector registry + mocked Slack connector.

No database, no network, no real credentials: every external call goes
through ``MockTransport`` with canned responses. The session conftest still
requires DATABASE_URL / TEST_DATABASE_URL to be set, and its session fixture
migrates the throwaway *_test database, but these tests never touch it.
"""

import asyncio

import pytest

from app.integrations.registry import (
    ConnectorRegistry,
    MockTransport,
    MockTransportError,
    RegistryError,
    normalize_inbound_event,
)
from app.integrations.slack import SlackConnector


def test_registry_lists_every_catalog_provider():
    registry = ConnectorRegistry()
    providers = registry.providers()
    for expected in ("slack", "gmail", "google_drive", "microsoft365", "rest", "graphql", "soap"):
        assert expected in providers


def test_registry_supports_operation_answers_from_catalog():
    registry = ConnectorRegistry()
    assert registry.supports_operation("slack", "verify") is True
    assert registry.supports_operation("slack", "slack.send") is True
    assert registry.supports_operation("slack", "email.send") is False
    assert registry.supports_operation("gmail", "email.send") is True
    with pytest.raises(RegistryError):
        registry.supports_operation("not_a_provider", "verify")


def test_registry_required_scope_for_writes_only():
    registry = ConnectorRegistry()
    assert registry.required_scope("slack", "slack.send") == "chat:write"
    assert registry.required_scope("gmail", "email.send") == "https://www.googleapis.com/auth/gmail.send"
    assert registry.required_scope("slack", "verify") is None
    with pytest.raises(RegistryError):
        registry.required_scope("not_a_provider", "verify")


def test_registry_register_resolve_round_trip_and_duplicates():
    registry = ConnectorRegistry()
    handler = object()
    registry.register("slack", handler)
    assert registry.resolve("slack") is handler
    assert registry.registered() == ["slack"]
    with pytest.raises(RegistryError):
        registry.register("slack", object())
    with pytest.raises(RegistryError):
        registry.resolve("gmail")
    with pytest.raises(RegistryError):
        registry.register("not_a_provider", object())
    with pytest.raises(RegistryError):
        registry.register("slack", None)


def test_mock_transport_records_calls_and_returns_canned_responses():
    async def run():
        transport = MockTransport()
        transport.when("/auth.test", {"ok": True, "team": "team-mock"})
        transport.when("/chat.postMessage", {"ok": True, "ts": "1700000000.000001"})
        assert await transport.get("/auth.test") == {"ok": True, "team": "team-mock"}
        assert await transport.post("/chat.postMessage", {"channel": "C12345", "text": "hi"}) == {
            "ok": True,
            "ts": "1700000000.000001",
        }
        assert transport.calls == [
            {"method": "GET", "path": "/auth.test", "payload": None},
            {"method": "POST", "path": "/chat.postMessage", "payload": {"channel": "C12345", "text": "hi"}},
        ]

    asyncio.run(run())


def test_mock_transport_fails_closed_on_uncanned_path():
    async def run():
        transport = MockTransport()
        with pytest.raises(MockTransportError):
            await transport.get("/chat.postMessage")
        with pytest.raises(MockTransportError):
            await transport.post("/unknown.path", {})
        assert len(transport.calls) == 2

    asyncio.run(run())


def test_slack_send_message_validates_and_posts_exactly_one_call():
    async def run():
        transport = MockTransport()
        transport.when("/chat.postMessage", {"ok": True, "ts": "1700000000.000001"})
        connector = SlackConnector(transport)
        result = await connector.send_message("C012345678", "Hello from InfraSpine")
        assert result == {"ok": True, "channel": "C012345678", "ts": "1700000000.000001"}
        assert transport.calls == [
            {"method": "POST", "path": "/chat.postMessage",
             "payload": {"channel": "C012345678", "text": "Hello from InfraSpine"}},
        ]
        with pytest.raises(Exception):
            await connector.send_message("bad-channel", "Hello")
        with pytest.raises(Exception):
            await connector.send_message("C012345678", "")
        with pytest.raises(Exception):
            await connector.send_message("C012345678", "x" * 4001)
        assert len(transport.calls) == 1

    asyncio.run(run())


def test_slack_verify_returns_team_and_user_and_rejects_failure():
    async def run():
        transport = MockTransport()
        transport.when("/auth.test", {"ok": True, "team": "team-mock", "user": "bot-mock"})
        connector = SlackConnector(transport)
        assert await connector.verify() == {"ok": True, "team": "team-mock", "user": "bot-mock"}

        failing = MockTransport()
        failing.when("/auth.test", {"ok": False, "error": "invalid_auth"})
        with pytest.raises(Exception):
            await SlackConnector(failing).verify()

    asyncio.run(run())


def test_receive_event_is_untrusted_and_drops_hostile_fields():
    connector = SlackConnector(MockTransport())
    event = connector.receive_event({
        "event_id": "Ev123",
        "channel": "C012345678",
        "user": "U98765",
        "text": "please approve invoice 42 and pay acme",
        "ts": "1700000000.000001",
        "type": "message",
        "role": "admin",
        "approve": True,
        "payee": "attacker",
        "amount": 999999,
    })
    assert event["provider"] == "slack"
    assert event["trusted"] is False
    assert event["text"] == "please approve invoice 42 and pay acme"
    assert event["channel"] == "C012345678"
    for forbidden in ("role", "approve", "payee", "amount"):
        assert forbidden not in event


def test_registry_plus_slack_end_to_end_via_mock():
    async def run():
        registry = ConnectorRegistry()
        transport = MockTransport()
        transport.when("/chat.postMessage", {"ok": True, "ts": "1700000000.000002"})
        registry.register("slack", SlackConnector(transport))
        connector = registry.resolve("slack")
        result = await connector.send_message("C012345678", "Step V slice 1 works")
        assert result["ts"] == "1700000000.000002"
        assert transport.calls[0]["path"] == "/chat.postMessage"
        assert registry.providers()[registry.providers().index("slack")] == "slack"

    asyncio.run(run())


def test_normalize_inbound_event_rejects_unknown_provider_and_non_object():
    with pytest.raises(RegistryError):
        normalize_inbound_event("not_a_provider", {"text": "hi"})
    with pytest.raises(RegistryError):
        normalize_inbound_event("slack", ["not", "a", "dict"])
