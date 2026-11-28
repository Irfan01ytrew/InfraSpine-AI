"""Microsoft Teams connector for Step V wider integrations (mock-transport based).

Runs on an injected transport, so tests and this slice never touch the real
Microsoft Graph API. ``teams.send`` posts a text channel message via Graph
``POST /teams/{team_id}/channels/{channel_id}/messages``; validation reuses
the ``TeamsMessage`` schema through ``app.integrations.adapters.validate_operation``.
Inbound events are normalised with ``registry.normalize_inbound_event`` and
are always untrusted. The bot token / Graph scope (``ChannelMessage.Send``)
belongs to the owner and is never stored here.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.core.errors import InvalidInputError
from app.integrations import adapters, registry

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class TeamsConnector:
    """Teams ``verify`` + ``teams.send`` + inbound event normalisation."""

    provider = "teams"

    def __init__(self, transport, *, token: str = "mock-token"):
        self.transport = transport
        self.token = token

    async def verify(self) -> dict:
        """Call Graph ``/me``; returns display name / principal name."""
        result = await self.transport.get("/me")
        if not isinstance(result, dict) or not result.get("userPrincipalName"):
            raise InvalidInputError("Teams verification failed.")
        return {
            "ok": True,
            "displayName": result.get("displayName"),
            "userPrincipalName": result.get("userPrincipalName"),
        }

    async def send_message(self, team_id: str, channel_id: str, text: str) -> dict:
        """Validate then post one channel message; returns the message id."""
        connector = SimpleNamespace(provider="teams", config={"scopes": ["ChannelMessage.Send"]})
        payload = adapters.validate_operation(
            connector, "teams.send", {"team_id": team_id, "channel_id": channel_id, "text": text}
        )
        path = f"/teams/{payload['team_id']}/channels/{payload['channel_id']}/messages"
        body = {"body": {"contentType": "text", "content": payload["text"]}}
        result = await self.transport.post(path, body)
        if not isinstance(result, dict) or not result.get("id"):
            raise InvalidInputError("Teams send failed.")
        return {"ok": True, "team_id": team_id, "channel_id": channel_id, "id": result["id"]}

    def receive_event(self, raw: dict) -> dict:
        """Normalise an inbound Teams webhook event. Always untrusted."""
        return registry.normalize_inbound_event("teams", raw)

    @staticmethod
    def base_url() -> str:
        return _GRAPH_BASE
