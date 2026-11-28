"""Generic connector registry + mock transport for Step V wider integrations.

One place that resolves a provider name to its connector handler, answers
which operations a provider supports and which scope a write needs, and
normalises inbound events from outside systems.

Design notes (handover §5 rule 4):
- The catalog in ``app.integrations.catalog`` is the allow-list. A provider
  not in the catalog cannot be registered or resolved.
- ``MockTransport`` is the only transport tests may use: it records calls,
  returns canned responses, and fails closed on any path without a canned
  response, so a test can never accidentally reach a real provider.
- ``normalize_inbound_event`` allow-lists fields. External text is untrusted:
  the normalised event is always marked ``trusted: False`` and any field
  claiming a role, permission, approval, payee or amount is dropped.
"""

from __future__ import annotations

from app.core.errors import InvalidInputError
from app.integrations.catalog import CATALOG, WRITE_SCOPES

# Inbound events may only carry these fields. Everything else is dropped.
_INBOUND_ALLOWLIST = ("event_id", "channel", "user", "text", "ts", "type")

# Fields that must never survive normalisation: they would claim authority.
_INBOUND_FORBIDDEN = (
    "role",
    "roles",
    "permission",
    "permissions",
    "approve",
    "approved",
    "approval",
    "payee",
    "amount",
    "admin",
    "owner",
    "tenant_id",
)


class RegistryError(InvalidInputError):
    """Raised for unknown providers, unsupported operations, bad handlers."""


class MockTransportError(InvalidInputError):
    """Raised when a mock transport is asked for a path with no canned reply."""


class ConnectorRegistry:
    """Resolves provider names to connector handlers, backed by the catalog."""

    def __init__(self):
        self._handlers: dict[str, object] = {}

    def providers(self) -> list[str]:
        """Every provider the code-owned catalog allows, in catalog order."""
        return list(CATALOG)

    def supports_operation(self, provider: str, operation: str) -> bool:
        """True when the catalog lists ``operation`` for ``provider``."""
        definition = CATALOG.get(provider)
        if definition is None:
            raise RegistryError(f"Unknown provider: {provider}")
        return operation in definition["operations"]

    def required_scope(self, provider: str, operation: str) -> str | None:
        """Scope a write operation needs, or None for reads / unknown pairs."""
        if provider not in CATALOG:
            raise RegistryError(f"Unknown provider: {provider}")
        return WRITE_SCOPES.get((provider, operation))

    def register(self, provider: str, handler: object) -> None:
        """Bind a handler to a catalog provider. Duplicates are rejected."""
        if provider not in CATALOG:
            raise RegistryError(f"Unknown provider: {provider}")
        if handler is None:
            raise RegistryError("Handler must not be None.")
        if provider in self._handlers:
            raise RegistryError(f"Provider already registered: {provider}")
        self._handlers[provider] = handler

    def resolve(self, provider: str) -> object:
        """Return the registered handler, or raise for unknown/unregistered."""
        if provider not in CATALOG:
            raise RegistryError(f"Unknown provider: {provider}")
        try:
            return self._handlers[provider]
        except KeyError:
            raise RegistryError(f"No handler registered for provider: {provider}") from None

    def registered(self) -> list[str]:
        """Providers that currently have a handler, in registration order."""
        return list(self._handlers)


class MockTransport:
    """In-memory stand-in for a provider HTTP API.

    ``when(path, response)`` programs a canned reply. Every call is recorded
    in ``calls`` in order. A call to a path with no canned reply raises
    ``MockTransportError`` instead of touching any network.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self._canned: dict[str, object] = {}

    def when(self, path: str, response: object) -> None:
        self._canned[path] = response

    def _reply(self, method: str, path: str, payload: dict | None) -> object:
        self.calls.append({"method": method, "path": path, "payload": payload})
        if path not in self._canned:
            raise MockTransportError(f"No canned response for {method} {path}")
        response = self._canned[path]
        return response() if callable(response) else response

    async def get(self, path: str) -> object:
        return self._reply("GET", path, None)

    async def post(self, path: str, payload: dict) -> object:
        return self._reply("POST", path, payload)


def normalize_inbound_event(provider: str, raw: dict) -> dict:
    """Normalise an inbound provider event into an allow-listed dict.

    The result is always ``trusted: False``. Fields outside the allow-list —
    in particular anything claiming a role, permission, approval, payee or
    amount — are dropped, never carried forward.
    """
    if provider not in CATALOG:
        raise RegistryError(f"Unknown provider: {provider}")
    if not isinstance(raw, dict):
        raise RegistryError("Inbound event must be a JSON object.")
    normalised = {"provider": provider, "trusted": False}
    for field in _INBOUND_ALLOWLIST:
        value = raw.get(field)
        if isinstance(value, str) and value:
            normalised[field] = value
    for field in _INBOUND_FORBIDDEN:
        normalised.pop(field, None)
    return normalised
