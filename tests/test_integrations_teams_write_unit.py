"""DB-free unit tests for the Step V ``teams.send`` branch in
``adapters.execute_write`` (slice 4).

No database, no network, no real credentials: ``adapters.call`` is replaced
with a fake that records the path/body and returns a canned Graph reply.
Before this slice a Teams write fell through to the connector's default
``write_path``; now it posts the Graph channel-message path/body, mirroring
the existing ``email.send`` provider split.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.integrations import adapters
from app.integrations.adapters import TransportError

TEAM_ID = "11111111-2222-3333-4444-555555555555"
CHANNEL_ID = "19:abc123def456@thread.tacv2"
GRAPH_PATH = f"/teams/{TEAM_ID}/channels/{CHANNEL_ID}/messages"


def _teams_connector():
    return SimpleNamespace(
        provider="teams",
        config={"scopes": ["ChannelMessage.Send"]},
        base_url="https://graph.microsoft.com/v1.0",
        cursor=None,
    )


def _job(operation="teams.send"):
    return SimpleNamespace(operation=operation, id=uuid.uuid4())


def test_execute_write_teams_send_posts_graph_path_and_body(monkeypatch):
    calls = []

    async def fake_call(connector, secret, path, **kwargs):
        calls.append({"path": path, "payload": kwargs.get("payload"), "method": kwargs.get("method")})
        return SimpleNamespace(body={"id": "msg-123"}, status=201)

    monkeypatch.setattr(adapters, "call", fake_call)

    async def run():
        result = await adapters.execute_write(
            _teams_connector(),
            {"access_token": "mock-token"},
            _job(),
            {"team_id": TEAM_ID, "channel_id": CHANNEL_ID, "text": "Hello from InfraSpine"},
        )
        assert result["provider_id"] == "msg-123"
        assert result["provider_status"] == 201
        assert result["acceptance"] == "provider_accepted"
        assert calls == [{
            "path": GRAPH_PATH,
            "payload": {"body": {"contentType": "text", "content": "Hello from InfraSpine"}},
            "method": "POST",
        }]

    asyncio.run(run())


def test_execute_write_teams_send_missing_id_is_ambiguous(monkeypatch):
    async def fake_call(connector, secret, path, **kwargs):
        return SimpleNamespace(body={}, status=201)

    monkeypatch.setattr(adapters, "call", fake_call)

    async def run():
        with pytest.raises(TransportError) as excinfo:
            await adapters.execute_write(
                _teams_connector(),
                {"access_token": "mock-token"},
                _job(),
                {"team_id": TEAM_ID, "channel_id": CHANNEL_ID, "text": "hi"},
            )
        assert excinfo.value.ambiguous is True

    asyncio.run(run())


def test_execute_write_teams_send_validates_payload(monkeypatch):
    calls = []

    async def fake_call(connector, secret, path, **kwargs):
        calls.append(path)
        return SimpleNamespace(body={"id": "msg-1"}, status=201)

    monkeypatch.setattr(adapters, "call", fake_call)

    async def run():
        with pytest.raises(Exception):
            await adapters.execute_write(
                _teams_connector(), {"access_token": "mock-token"}, _job(),
                {"team_id": "not-a-guid", "channel_id": CHANNEL_ID, "text": "hi"},
            )
        with pytest.raises(Exception):
            await adapters.execute_write(
                _teams_connector(), {"access_token": "mock-token"}, _job(),
                {"team_id": TEAM_ID, "channel_id": CHANNEL_ID, "text": ""},
            )
        assert calls == []

    asyncio.run(run())
