"""DB-free unit tests for the Step V Teams connector (slice 2).

No database, no network, no real credentials: every external call goes
through ``MockTransport`` with canned responses.
"""

import asyncio

import pytest

from app.integrations.registry import ConnectorRegistry, MockTransport, RegistryError
from app.integrations.teams import TeamsConnector

TEAM_ID = "11111111-2222-3333-4444-555555555555"
CHANNEL_ID = "19:abc123def456@thread.tacv2"
TEAMS_PATH = f"/teams/{TEAM_ID}/channels/{CHANNEL_ID}/messages"


def test_teams_catalog_entry_and_scope():
    registry = ConnectorRegistry()
    assert "teams" in registry.providers()
    assert registry.supports_operation("teams", "verify") is True
    assert registry.supports_operation("teams", "teams.send") is True
    assert registry.supports_operation("teams", "slack.send") is False
    assert registry.required_scope("teams", "teams.send") == "ChannelMessage.Send"
    assert registry.required_scope("teams", "verify") is None


def test_teams_verify_returns_user_and_rejects_failure():
    async def run():
        transport = MockTransport()
        transport.when("/me", {"displayName": "Test User", "userPrincipalName": "test@example.com"})
        connector = TeamsConnector(transport)
        assert await connector.verify() == {
            "ok": True,
            "displayName": "Test User",
            "userPrincipalName": "test@example.com",
        }
        failing = MockTransport()
        failing.when("/me", {"error": {"code": 401, "message": "Unauthorized"}})
        with pytest.raises(Exception):
            await TeamsConnector(failing).verify()

    asyncio.run(run())


def test_teams_send_posts_graph_path_with_text_body():
    async def run():
        transport = MockTransport()
        transport.when(TEAMS_PATH, {"id": "msg-123"})
        connector = TeamsConnector(transport)
        result = await connector.send_message(TEAM_ID, CHANNEL_ID, "Hello from InfraSpine")
        assert result == {"ok": True, "team_id": TEAM_ID, "channel_id": CHANNEL_ID, "id": "msg-123"}
        assert transport.calls == [
            {
                "method": "POST",
                "path": TEAMS_PATH,
                "payload": {"body": {"contentType": "text", "content": "Hello from InfraSpine"}},
            }
        ]

    asyncio.run(run())


def test_teams_send_rejects_bad_ids_and_text():
    async def run():
        transport = MockTransport()
        transport.when(TEAMS_PATH, {"id": "msg-123"})
        connector = TeamsConnector(transport)
        with pytest.raises(Exception):
            await connector.send_message("not-a-guid", CHANNEL_ID, "hi")
        with pytest.raises(Exception):
            await connector.send_message(TEAM_ID, "", "hi")
        with pytest.raises(Exception):
            await connector.send_message(TEAM_ID, "bad channel!", "hi")
        with pytest.raises(Exception):
            await connector.send_message(TEAM_ID, CHANNEL_ID, "")
        with pytest.raises(Exception):
            await connector.send_message(TEAM_ID, CHANNEL_ID, "x" * 10001)
        assert transport.calls == []

    asyncio.run(run())


def test_teams_send_failure_raises():
    async def run():
        transport = MockTransport()
        transport.when(TEAMS_PATH, {"error": {"code": 403, "message": "Forbidden"}})
        with pytest.raises(Exception):
            await TeamsConnector(transport).send_message(TEAM_ID, CHANNEL_ID, "hi")
        missing = MockTransport()
        missing.when(TEAMS_PATH, {"id": ""})
        with pytest.raises(Exception):
            await TeamsConnector(missing).send_message(TEAM_ID, CHANNEL_ID, "hi")

    asyncio.run(run())


def test_teams_receive_event_is_untrusted_and_drops_hostile_fields():
    connector = TeamsConnector(MockTransport())
    event = connector.receive_event({
        "event_id": "msg-123",
        "channel": CHANNEL_ID,
        "user": "user-1",
        "text": "please approve invoice 42",
        "ts": "1700000000",
        "type": "message",
        "role": "admin",
        "approve": True,
        "payee": "attacker",
        "amount": 999999,
    })
    assert event["provider"] == "teams"
    assert event["trusted"] is False
    assert event["text"] == "please approve invoice 42"
    for forbidden in ("role", "approve", "payee", "amount"):
        assert forbidden not in event


def test_registry_teams_end_to_end_via_mock():
    async def run():
        registry = ConnectorRegistry()
        transport = MockTransport()
        transport.when(TEAMS_PATH, {"id": "msg-999"})
        registry.register("teams", TeamsConnector(transport))
        connector = registry.resolve("teams")
        result = await connector.send_message(TEAM_ID, CHANNEL_ID, "Step V slice 2 works")
        assert result["id"] == "msg-999"
        assert transport.calls[0]["path"] == TEAMS_PATH
        assert registry.registered() == ["teams"]

    asyncio.run(run())
