"""Live sync feeds (Step S slice 3).

Outbox-driven updaters that keep the twin fresh. Delivery is
at-least-once (``app.platform.outbox``), so every feed is idempotent:

- each event maps to a deterministic ``SyncInput`` whose
  ``idempotency_key`` is derived from the outbox event id
  (:func:`feed_idempotency_key`);
- the update itself goes through ``app.company_graph.discovery.synchronize``
  — the governed-change path — which replays a known key (same digest
  -> stored result, different digest -> ``ConflictError``) and only
  ever creates *pending* ``GraphChange`` proposals. A feed never
  writes ``GraphRecord`` / ``GraphEvent`` directly, never approves a
  change (``decide`` stays a human owner/approver action), never
  grants access, never pays, and never applies an ungoverned change.

Supported outbox events (all published today — see ``app/finance`` /
``app/workflows/action_runtime.py``):

- ``finance.batch_started`` — batch + its workflow run appear in the
  twin (batch status ``verifying``).
- ``finance.payment_verified`` / ``finance.payment_failed`` — the
  payment appears with its new status, linked to its batch.
- ``workflow.step_verified`` / ``workflow.blocked`` — the workflow
  run's freshness (last verified node / blocked reason).

Explicitly out of scope for this slice: ``approval.*`` (decisions
already flow through governance; the twin reads them via the
snapshot, it does not re-apply them), ``identity.session_revoked``
(owned by ``app.platform.handlers`` — access revocation must never
wait on the twin), and ``document.*`` (documents do not change the
twin). An unsupported event is *ignored*, not an error — other
consumers own those types.

Registration is explicit: importing this module registers nothing.
Call :func:`register_twin_feeds` once at startup with the twin's
graph source id, an owner graph scope, and an audit context; it
registers one handler per supported event type with
``app.platform.outbox``.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from app.company_graph.discovery import synchronize
from app.company_graph.schemas import SyncInput, SyncItem
from app.platform.outbox import register as register_handler

# Twin-feed entities are owner-visible only until the owner confirms
# (plan §7) that finance amounts may appear for authorized finance
# viewers. Restricted + no readers => only the graph owner sees them.
FEED_VISIBILITY = "restricted"
FEED_SOURCE_KIND = "digital_twin_feed"

# event_type -> (required payload fields, translator)
SUPPORTED_EVENTS = frozenset({
    "finance.batch_started",
    "finance.payment_verified",
    "finance.payment_failed",
    "workflow.step_verified",
    "workflow.blocked",
})

_REQUIRED_FIELDS = {
    "finance.batch_started": ("batch_id", "run_id"),
    "finance.payment_verified": ("payment_id", "batch_id"),
    "finance.payment_failed": ("payment_id", "batch_id"),
    "workflow.step_verified": ("run_id", "node_id"),
    "workflow.blocked": ("run_id", "node_id"),
}


def feed_idempotency_key(source_name, event_type, event_id):
    """Deterministic idempotency key for one outbox event (<=120 chars).

    Same event -> same key (safe replay); different event -> different
    key. Long inputs are hashed so the key always fits ``SyncInput``.
    """
    raw = f"twin-feed:{source_name}:{event_type}:{event_id}"
    if len(raw) <= 120:
        return raw
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"twin-feed:{digest}"


def _require_str(payload, field, *, event_type):
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"Feed event {event_type!r} needs a non-empty string payload field {field!r}.")
    return value


def _entity(key, kind, name, *, attributes=None, quote):
    return SyncItem(key=key, kind=kind, name=name,
                    attributes=dict(attributes or {}), quote=quote)


def _edge(key, kind, source_key, target_key, *, quote):
    return SyncItem(key=key, kind=kind, name=f"{kind} {source_key} -> {target_key}",
                    quote=quote, source_key=source_key, target_key=target_key)


def translate_event(event_type, payload, *, event_id, observed_at=None):
    """Translate one outbox event to a ``SyncInput`` (pure).

    Returns ``None`` for event types this feed does not own (ignored,
    not an error). Raises ``ValueError`` for a supported event with a
    malformed payload — the outbox then retries / dead-letters it
    instead of silently dropping a money event. Never reads the
    database, never writes anything.
    """
    if event_type not in SUPPORTED_EVENTS:
        return None
    if not isinstance(payload, dict):
        raise ValueError(f"Feed event {event_type!r} payload must be a dict.")
    for field in _REQUIRED_FIELDS[event_type]:
        _require_str(payload, field, event_type=event_type)
    observed_at = observed_at or datetime.now(UTC)
    quote = f"Outbox event {event_type} (id {event_id}): twin freshness update."
    reference = f"platform_outbox:{event_type}:{event_id}"
    items = []
    if event_type == "finance.batch_started":
        batch_id, run_id = payload["batch_id"], payload["run_id"]
        items = [
            _entity(f"fin-batch:{batch_id}", "invoice_batch", f"Invoice batch {batch_id}",
                    attributes={"status": "verifying", "round": payload.get("round"),
                                "source": "twin-feed"}, quote=quote),
            _entity(f"wf-run:{run_id}", "workflow", f"Workflow run {run_id}",
                    attributes={"status": "running", "source": "twin-feed"}, quote=quote),
            _edge(f"wf-depends:{run_id}:{batch_id}", "depends_on",
                  f"wf-run:{run_id}", f"fin-batch:{batch_id}", quote=quote),
        ]
    elif event_type in ("finance.payment_verified", "finance.payment_failed"):
        payment_id, batch_id = payload["payment_id"], payload["batch_id"]
        status = "verified" if event_type == "finance.payment_verified" else "failed"
        attributes = {"status": status, "source": "twin-feed"}
        if event_type == "finance.payment_verified" and payload.get("external_ref"):
            attributes["external_ref"] = str(payload["external_ref"])[:200]
        if event_type == "finance.payment_failed" and payload.get("code"):
            attributes["failure_code"] = str(payload["code"])[:80]
        items = [
            _entity(f"fin-pay:{payment_id}", "payment", f"Payment {payment_id}",
                    attributes=attributes, quote=quote),
            _edge(f"fin-settles:{batch_id}:{payment_id}", "depends_on",
                  f"fin-pay:{payment_id}", f"fin-batch:{batch_id}", quote=quote),
        ]
    elif event_type == "workflow.step_verified":
        run_id = payload["run_id"]
        items = [_entity(f"wf-run:{run_id}", "workflow", f"Workflow run {run_id}",
                         attributes={"status": "running",
                                     "last_verified_node": payload["node_id"],
                                     "handler": str(payload.get("handler") or ""),
                                     "source": "twin-feed"}, quote=quote)]
    elif event_type == "workflow.blocked":
        run_id = payload["run_id"]
        items = [_entity(f"wf-run:{run_id}", "workflow", f"Workflow run {run_id}",
                         attributes={"status": "blocked",
                                     "blocked_reason": str(payload.get("reason") or ""),
                                     "last_verified_node": payload["node_id"],
                                     "source": "twin-feed"}, quote=quote)]
    return SyncInput(
        idempotency_key=feed_idempotency_key(FEED_SOURCE_KIND, event_type, event_id),
        observed_at=observed_at, reference=reference,
        visibility=FEED_VISIBILITY, items=items)


async def apply_feed(db, graph_scope, *, source_id, event, ctx, observed_at=None):
    """Apply one outbox event to the twin via the governed-change path.

    ``event`` is a ``PlatformOutbox`` row (``event_type`` / ``payload``
    / ``id``). Unsupported events return
    ``{"status": "ignored", ...}`` without touching the database.
    Supported events call ``discovery.synchronize`` exactly once with
    the translated ``SyncInput``; that function only creates pending
    ``GraphChange`` proposals and records the ``GraphSync`` replay row —
    it never writes a ``GraphRecord`` and never approves anything.
    Returns the synchronize result plus ``{"status": "proposed"}``.
    """
    event_id = event.id
    sync_input = translate_event(event.event_type, event.payload, event_id=event_id)
    if sync_input is None:
        return {"status": "ignored", "event_type": event.event_type, "event_id": event_id}
    result = await synchronize(db, graph_scope, source_id, sync_input, ctx)
    return {"status": "proposed", "event_type": event.event_type,
            "event_id": event_id, **result}


def make_feed_handler(*, source_id, graph_scope, ctx):
    """Build an outbox handler bound to one twin graph source.

    The returned ``handler(db, event)`` calls :func:`apply_feed` with
    the bound source/scope/ctx. The scope must belong to a graph owner
    (``discovery.source_for`` requires it); approval of the proposals
    a feed creates stays with a human owner/approver.
    """
    async def _handler(db, event):
        await apply_feed(db, graph_scope, source_id=source_id, event=event, ctx=ctx)
    return _handler


def register_twin_feeds(*, source_id, graph_scope, ctx):
    """Register the twin feed handler for every supported event type.

    Call once at startup. Returns the list of event types registered.
    """
    handler = make_feed_handler(source_id=source_id, graph_scope=graph_scope, ctx=ctx)
    for event_type in sorted(SUPPORTED_EVENTS):
        register_handler(event_type)(handler)
    return sorted(SUPPORTED_EVENTS)


__all__ = [
    "FEED_SOURCE_KIND", "SUPPORTED_EVENTS", "apply_feed",
    "feed_idempotency_key", "make_feed_handler", "register_twin_feeds",
    "translate_event",
]
