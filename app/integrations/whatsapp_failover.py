"""WhatsApp transport path setting + automatic failover (Step F slice 3).

The Agent Studio WhatsApp panel used to offer a QR/Cloud toggle that only
switched which panel was shown. The path setting added here is a runtime
choice: which transport a reply is sent on, and what happens when that
transport cannot deliver.

Semantics (plan ``docs/step-f-whatsapp-plan.md`` §5):

- Per-owner setting: ``cloud``, ``baileys`` or ``auto`` (default). It picks
  the *primary* transport; ``auto`` prefers the channel the message arrived
  on. A primary that is not configured or not connected is skipped, never
  retried.
- Failover: when the primary raises a failover-eligible error — a provider
  error that returned no delivery confirmation — the dispatch is retried
  exactly once on the other path, under the same idempotency key, and the
  attempt is audited (``integration.whatsapp_failover``). If the fallback
  also fails, the primary's error is raised so the caller's existing
  retry/``uncertain`` handling applies unchanged.
- "Never a duplicate" rests on the same idempotency key: the caller wraps
  the send in ``dispatch_once`` (duty ``channel.reply``) and failover runs
  inside that closure. A failover is only attempted when the primary
  confirmed nothing — a response with no provider message id, a 5xx, a
  connection failure, a 429, or a rejected credential. A send that *may*
  have been accepted (an ambiguous response loss) is not retried on the
  other path by this adapter; the worker marks it ``uncertain`` as before.
  The same caveat applies to the Baileys leg: a bridge timeout means the
  bridge may still deliver late, so it is reported ambiguous.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.agent_studio_baileys_models import BaileysConnection
from app.db.agent_studio_whatsapp_models import AgentStudioWhatsAppConnection as Connection, WhatsAppPathSetting
from app.integrations.transport import TransportError
from app.services.audit import AuditContext, record as audit_record

WHATSAPP_PATHS = ("cloud", "baileys", "auto")
DEFAULT_PATH = "auto"

# Cloud TransportError codes where Meta returned no delivery confirmation,
# so trying the other path cannot double-deliver. Anything else (notably
# ``provider_rejected`` — the request itself was refused as malformed) is
# not failover-eligible: the other path would fail the same way.
FAILOVER_ELIGIBLE_CODES = frozenset({
    "provider_unavailable",  # Meta 5xx — no message id returned
    "missing_message_identifier",  # 200 without a message id — nothing confirmed
    "transport_unavailable",  # connection failure before any confirmation
    "rate_limited",  # 429 — provider refused, nothing delivered
    "credentials_or_scope_rejected",  # 401/403 — provider refused, nothing delivered
})


class PathUnavailable(TransportError):
    """A path could not deliver anything (no provider confirmation).

    A subclass of :class:`TransportError` so the workers' existing error
    handling (rate-limit retry, ``uncertain`` on ambiguous errors) applies
    unchanged. ``path`` names the transport that failed.
    """

    def __init__(self, path: str, code: str, *, retryable: bool = False, ambiguous: bool = False, retry_after: int = 0) -> None:
        super().__init__(code, retryable=retryable, ambiguous=ambiguous, retry_after=retry_after)
        self.path = path


def phone_to_jid(phone: str) -> str:
    """Cloud ``from_wa_id`` (digits) to the Baileys JID for the same number."""
    return f"{phone}@s.whatsapp.net"


def jid_to_phone(jid: str) -> str | None:
    """Baileys JID to the Cloud phone number, or None for non-phone JIDs
    (``@lid`` identities have no phone number and Cloud cannot reach them)."""
    if jid.endswith("@s.whatsapp.net"):
        return jid[: -len("@s.whatsapp.net")] or None
    return None


def resolve_primary(setting: str, *, own_channel: str, cloud_available: bool, baileys_available: bool) -> str | None:
    """Pick the primary transport for one send, or None when neither can send.

    An explicit setting wins when its transport is available; ``auto``
    prefers the channel the message arrived on. An unavailable preferred
    path is skipped (not retried) — the other path becomes the primary.
    """
    if setting not in WHATSAPP_PATHS:
        raise ValueError(f"unknown WhatsApp path: {setting!r}")
    if setting == "cloud":
        preferred, other = "cloud", "baileys"
    elif setting == "baileys":
        preferred, other = "baileys", "cloud"
    elif own_channel == "baileys":
        preferred, other = "baileys", "cloud"
    else:
        preferred, other = "cloud", "baileys"
    available = {"cloud": cloud_available, "baileys": baileys_available}
    if available[preferred]:
        return preferred
    if available[other]:
        return other
    return None


async def get_path_setting(db: AsyncSession, owner_id: uuid.UUID) -> str:
    """The owner's path setting; ``auto`` when no row exists (the default)."""
    row = await db.scalar(select(WhatsAppPathSetting.path).where(WhatsAppPathSetting.owner_id == owner_id))
    if row is None:
        return DEFAULT_PATH
    if row not in WHATSAPP_PATHS:
        raise ValueError(f"unknown WhatsApp path in storage: {row!r}")
    return row


async def set_path_setting(db: AsyncSession, owner_id: uuid.UUID, path: str) -> str:
    """Store the owner's path setting (validated). Audited only on change."""
    if path not in WHATSAPP_PATHS:
        raise ValueError(f"unknown WhatsApp path: {path!r}")
    row = await db.get(WhatsAppPathSetting, owner_id)
    previous = row.path if row is not None else DEFAULT_PATH
    if row is None:
        db.add(WhatsAppPathSetting(owner_id=owner_id, path=path))
    else:
        row.path = path
        row.updated_at = datetime.now(UTC)
    if previous != path:
        audit_record(db, AuditContext(owner_id, None, None, None), "integration.whatsapp_path_changed",
                     target=str(owner_id), details={"previous": previous, "path": path})
    return path


async def cloud_connection_for(db: AsyncSession, owner_id: uuid.UUID) -> Connection | None:
    """The owner's enabled Cloud connection, or None when there is none."""
    return await db.scalar(select(Connection).where(Connection.owner_id == owner_id, Connection.enabled.is_(True)))


async def live_baileys_session(db: AsyncSession, sessions, owner_id: uuid.UUID):
    """The live Baileys bridge session for the owner's connection, or None.

    ``sessions`` is the ``app.state.baileys_sessions`` registry (connection
    id -> ``(BridgeSession, task)`` tuple), maintained by the Baileys
    supervisor. A session counts as live only while the bridge process is
    up and the ``online`` flag is set — a merely configured connection is
    not enough to receive a failover send.
    """
    if not sessions:
        return None
    row = await db.scalar(select(BaileysConnection).where(BaileysConnection.owner_id == owner_id, BaileysConnection.enabled.is_(True)))
    if row is None:
        return None
    session = sessions.get(row.id) or sessions.get(str(row.id))
    if session is None:
        return None
    # The supervisor stores (bridge, task) tuples in this registry; unpack
    # the bridge. A bare session is also accepted (tests register one).
    if isinstance(session, tuple):
        session = session[0]
    if not session.online.is_set():
        return None
    process = getattr(session, "process", None)
    if process is not None and process.poll() is not None:
        return None
    return session


class CloudTransport:
    """Meta Cloud API send for one inbound (or a stand-in with ``from_wa_id``).

    Only the inbound sender can ever be the recipient: a Cloud send always
    goes back to ``inbound.from_wa_id``. A different ``to`` is a programming
    error and is refused before any network call.
    """

    name = "cloud"

    def __init__(self, connection: Connection, inbound, cipher) -> None:
        self.connection, self.inbound, self.cipher = connection, inbound, cipher

    async def send(self, *, to: str, text: str, key: str, interactive=None, template=None) -> str:
        if to != self.inbound.from_wa_id:
            raise ValueError("Cloud send recipient must be the inbound sender")
        from app.integrations.whatsapp import send_interactive, send_template, send_text

        try:
            if template is not None:
                # Step F slice 6: a template send — name, language and
                # parameters; the Baileys leg gets the rendered text instead.
                name, language, parameters = template
                return await send_template(self.connection, self.inbound, self.cipher,
                                           name, language, parameters)
            if interactive is not None:
                body, buttons = interactive
                return await send_interactive(self.connection, self.inbound, self.cipher, body, buttons)
            return await send_text(self.connection, self.inbound, self.cipher, text)
        except TransportError as exc:
            if exc.code in FAILOVER_ELIGIBLE_CODES:
                raise PathUnavailable("cloud", exc.code, retryable=exc.retryable,
                                      ambiguous=exc.ambiguous, retry_after=exc.retry_after) from exc
            raise


class BaileysTransport:
    """Baileys bridge send through a live :class:`BridgeSession`.

    ``delivery_id`` is the bridge correlation id. When it is not given it
    is derived deterministically from the idempotency ``key``, so a replay
    of the same dispatch reuses the same bridge id instead of sending twice.
    """

    name = "baileys"

    def __init__(self, session, *, delivery_id: str | None = None, timeout: int = 30) -> None:
        self.session, self.timeout = session, timeout
        self.delivery_id = delivery_id

    async def send(self, *, to: str, text: str, key: str, interactive=None, template=None) -> str:
        # Baileys has no interactive buttons here: the card text (which
        # carries the APPROVE/REJECT commands) is the message. ``interactive``
        # is accepted and ignored so both transports share one call shape.
        # Step F slice 6: likewise, a template send arrives as its rendered
        # text — a normal WhatsApp account has no 24-hour window, so the
        # same words go out as a free-form message.
        delivery_id = self.delivery_id or uuid.uuid5(uuid.NAMESPACE_URL, f"whatsapp-failover:{key}").hex.upper()
        try:
            ok = await self.session.send_reply(delivery_id, to, text, timeout=self.timeout)
        except (RuntimeError, OSError) as exc:
            raise PathUnavailable("baileys", "bridge_unavailable", ambiguous=False) from exc
        if not ok:
            # The bridge may still deliver late after a timeout, so this is
            # ambiguous — the caller must not assume the message was lost.
            raise PathUnavailable("baileys", "delivery_failed", ambiguous=True)
        return delivery_id


async def send_with_failover(*, setting: str, own_channel: str, to: dict[str, str], text: str, key: str,
                             cloud, baileys, on_failover=None, interactive=None, template=None) -> tuple[str, str]:
    """Send once, failing over to the other path on an eligible error.

    ``cloud`` / ``baileys`` are transports (or None when that path is not
    available); ``to`` maps each path to its recipient address (Cloud: phone
    digits; Baileys: JID). Returns ``(provider_ref, path_used)``.

    ``interactive`` is ``(body, buttons)`` for an approval card: Cloud sends
    it as an interactive reply-button message, Baileys sends ``text``. When
    it is None the send is plain text on both paths.

    ``template`` is ``(name, language, parameters)`` for a 24-hour-window
    template send (Step F slice 6): Cloud sends the structured template,
    Baileys sends ``text`` (the rendered template text).

    When the primary fails with :class:`PathUnavailable` the fallback is
    tried exactly once and ``on_failover(from_path, to_path, reason, key,
    outcome=..., fallback_code=...)`` is awaited with the outcome. When both
    fail the *primary's* error is raised, so the caller's existing
    retry/``uncertain`` handling applies to the error it already knows.
    """
    primary_name = resolve_primary(setting, own_channel=own_channel,
                                   cloud_available=cloud is not None, baileys_available=baileys is not None)
    if primary_name is None:
        raise PathUnavailable(own_channel, "path_unavailable")
    fallback_name = "baileys" if primary_name == "cloud" else "cloud"
    transports = {"cloud": cloud, "baileys": baileys}
    try:
        return await transports[primary_name].send(to=to[primary_name], text=text, key=key,
                                                   interactive=interactive, template=template), primary_name
    except PathUnavailable as primary_error:
        fallback = transports.get(fallback_name)
        if fallback is None:
            raise
        try:
            ref = await fallback.send(to=to[fallback_name], text=text, key=key,
                                      interactive=interactive, template=template)
        except PathUnavailable as fallback_error:
            if on_failover is not None:
                await on_failover(primary_name, fallback_name, primary_error.code, key,
                                  outcome="failure", fallback_code=fallback_error.code)
            raise primary_error
        if on_failover is not None:
            await on_failover(primary_name, fallback_name, primary_error.code, key, outcome="success")
        return ref, fallback_name


def cloud_stand_in(phone: str) -> SimpleNamespace:
    """A minimal inbound stand-in for a Cloud send to a Baileys recipient.

    :func:`send_text` only reads ``from_wa_id`` off the inbound, so a
    namespace is enough — no Cloud inbound row is fabricated.
    """
    return SimpleNamespace(from_wa_id=phone)
