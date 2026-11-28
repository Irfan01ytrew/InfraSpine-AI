"""Remaining live-sync feeds (Step K v2 slice 7).

Pure translators from platform outbox events to twin feed changes.
A feed change is a PROPOSAL for the company graph — it never writes
the graph directly. ``apply_feed`` (``feed_records``) stores it as a
pending ``GraphChange`` through the governed-change path, where a
human approves or rejects it.

Two tiers, stated honestly:

- LIVE on this branch: ``identity.session_revoked`` is published by
  ``app.platform.access`` when a user's platform access is revoked
  (payload ``{"user_id", "reason", ...counts}``, scope_id = org id).
  The twin proposes the graph member be deactivated.
- READY, no publisher yet on this branch: ``access.member_added`` /
  ``access.member_removed``, ``automation.*`` (workflow + run
  lifecycle), ``agent_studio.*`` / ``agent.profile_*`` (Agent Studio
  agents + runs) and ``cloud_connector.*`` exist today only as audit
  event names — no code on this branch publishes them to the outbox
  (verified by scanning every ``publish(`` call site). The
  translators and handlers below are the consumption side, tested
  end to end; they activate the moment publishers start emitting
  these kinds (Step S feed publishers / future slices). Until then a
  handler with no matching event simply never fires.

Subject keys are the contract: each feed kind's subject is read
from the key named in ``FEED_EVENTS`` (``user_id`` for members,
``agent_id`` for agents, ``workflow_id`` for workflows, ``run_id``
for runs, ``connector_id`` for connectors), falling back to
``subject_id`` then ``id`` for publishers that use generic keys.
A known kind whose payload carries no subject id is malformed and
raises ``ValueError`` — it is retried, then dead-lettered by the
outbox dispatcher, never silently dropped. An unknown kind returns
``None``: it is not the twin's business.

No secrets ever ride a feed change: payload keys whose name
mentions a credential (``secret``, ``token``, ``password``,
``credential``, ``api_key``, ``private``) are dropped from the
attributes, and attribute values are bounded to 200 characters.
"""

from __future__ import annotations

import hashlib
import json

# event_type -> (family, subject_kind, action, subject_key)
# action "upsert" = the subject exists (or its state changed);
# action "remove" = the subject is gone / deactivated.
FEED_EVENTS: dict[str, tuple[str, str, str, str]] = {
    # Membership (live: identity.session_revoked; ready: access.*)
    "identity.session_revoked": ("membership", "member", "remove", "user_id"),
    "access.member_added": ("membership", "member", "upsert", "user_id"),
    "access.member_removed": ("membership", "member", "remove", "user_id"),
    # Workflow runs (ready — no publisher on this branch yet)
    "automation.workflow_created": ("workflow", "workflow", "upsert", "workflow_id"),
    "automation.workflow_disabled": ("workflow", "workflow", "remove", "workflow_id"),
    "automation.workflow_version_created": ("workflow", "workflow", "upsert", "workflow_id"),
    "automation.run_requested": ("workflow", "workflow_run", "upsert", "run_id"),
    "automation.run_approved": ("workflow", "workflow_run", "upsert", "run_id"),
    "automation.run_rejected": ("workflow", "workflow_run", "upsert", "run_id"),
    "automation.run_cancelled": ("workflow", "workflow_run", "remove", "run_id"),
    "automation.run_retried": ("workflow", "workflow_run", "upsert", "run_id"),
    "automation.event_run_created": ("workflow", "workflow_run", "upsert", "run_id"),
    "automation.scheduled_run_created": ("workflow", "workflow_run", "upsert", "run_id"),
    # Agent Studio (ready — no publisher on this branch yet)
    "agent_studio.agent_created": ("agent_studio", "agent", "upsert", "agent_id"),
    "agent_studio.agent_updated": ("agent_studio", "agent", "upsert", "agent_id"),
    "agent_studio.agent_enabled": ("agent_studio", "agent", "upsert", "agent_id"),
    "agent_studio.agent_disabled": ("agent_studio", "agent", "upsert", "agent_id"),
    "agent_studio.agent_paused": ("agent_studio", "agent", "upsert", "agent_id"),
    "agent_studio.agent_resumed": ("agent_studio", "agent", "upsert", "agent_id"),
    "agent_studio.agent_deleted": ("agent_studio", "agent", "remove", "agent_id"),
    "agent_studio.run_queued": ("agent_studio", "agent_run", "upsert", "run_id"),
    "agent_studio.run_cancelled": ("agent_studio", "agent_run", "remove", "run_id"),
    "agent.profile_created": ("agent_studio", "agent", "upsert", "agent_id"),
    "agent.profile_versioned": ("agent_studio", "agent", "upsert", "agent_id"),
    "agent.profile_disabled": ("agent_studio", "agent", "remove", "agent_id"),
    "agent.profile_run_started": ("agent_studio", "agent_run", "upsert", "run_id"),
    "agent.profile_run_queued": ("agent_studio", "agent_run", "upsert", "run_id"),
    "agent.profile_run_requeued": ("agent_studio", "agent_run", "upsert", "run_id"),
    "agent.profile_run_retry_started": ("agent_studio", "agent_run", "upsert", "run_id"),
    "agent.profile_run_completed": ("agent_studio", "agent_run", "upsert", "run_id"),
    "agent.profile_run_failed": ("agent_studio", "agent_run", "upsert", "run_id"),
    "agent.profile_run_interrupted": ("agent_studio", "agent_run", "upsert", "run_id"),
    "agent.profile_run_attempt_failed": ("agent_studio", "agent_run", "upsert", "run_id"),
    "agent.profile_run_content_purged": ("agent_studio", "agent_run", "remove", "run_id"),
    # Connectors (ready — no publisher on this branch yet)
    "cloud_connector.created": ("connectors", "connector", "upsert", "connector_id"),
    "cloud_connector.deleted": ("connectors", "connector", "remove", "connector_id"),
    "cloud_connector.permissions_reconciled": ("connectors", "connector", "upsert", "connector_id"),
}

# Payload keys that may carry credentials: never copied into a feed
# change (or into a GraphChange payload built from one).
_SECRET_KEYS = ("secret", "token", "password", "credential", "api_key", "private")
# Payload keys copied verbatim (scalars only) into the change.
_SAFE_ATTRIBUTE_KEYS = ("status", "state", "role", "active", "label", "reason")
_MAX_ATTRIBUTE_CHARS = 200


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(word in lowered for word in _SECRET_KEYS)


def _short(value) -> str:
    text = json.dumps(value, sort_keys=True, default=str) if not isinstance(value, str) else value
    return text[:_MAX_ATTRIBUTE_CHARS]


def translate_event(event_type: str, payload: dict) -> dict | None:
    """Translate one outbox event to a twin feed change, or None.

    Returns ``{"feed", "family", "subject_kind", "subject_ref",
    "action", "name", "attributes", "source_event"}``. Unknown event
    types return None. A known type with a malformed payload raises
    ``ValueError``.
    """
    spec = FEED_EVENTS.get(event_type)
    if spec is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError(f"feed event {event_type!r} payload must be a dict, got {type(payload).__name__}")
    family, subject_kind, action, subject_key = spec
    subject_ref = None
    for key in (subject_key, "subject_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            subject_ref = value
            break
    if subject_ref is None:
        raise ValueError(f"feed event {event_type!r} carries no subject id (looked for {subject_key!r})")
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        name = subject_ref
    attributes: dict[str, object] = {"event": event_type}
    for key in _SAFE_ATTRIBUTE_KEYS:
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)) and not _is_secret_key(key):
            attributes[key] = _short(value) if isinstance(value, str) else value
    return {
        "feed": family,
        "family": family,
        "subject_kind": subject_kind,
        "subject_ref": subject_ref,
        "action": action,
        "name": name,
        "attributes": attributes,
        "source_event": event_type,
    }


def feed_change_fingerprint(change: dict) -> str:
    """SHA-256 identity of a feed change (dedupe / logging).

    Covers the feed, subject and the resulting state — two events
    that would propose the same graph change share a fingerprint.
    """
    canonical = json.dumps(
        {
            "feed": change.get("feed"),
            "subject_kind": change.get("subject_kind"),
            "subject_ref": change.get("subject_ref"),
            "action": change.get("action"),
            "name": change.get("name"),
            "attributes": change.get("attributes", {}),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
