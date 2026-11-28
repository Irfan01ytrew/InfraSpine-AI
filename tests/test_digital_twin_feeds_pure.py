"""Feed translators (Step K v2 slice 7). Pure tests, no DB."""

import pytest

from app.digital_twin import feeds


def test_unknown_event_returns_none():
    assert feeds.translate_event("document.deleted", {"document_id": "d-1"}) is None
    assert feeds.translate_event("platform.approval_requested", {}) is None


def test_live_session_revoked_event_translates_to_member_removal():
    change = feeds.translate_event("identity.session_revoked", {
        "user_id": "11111111-1111-1111-1111-111111111111", "reason": "access_revoked"})
    assert change["feed"] == "membership"
    assert change["subject_kind"] == "member"
    assert change["subject_ref"] == "11111111-1111-1111-1111-111111111111"
    assert change["action"] == "remove"
    assert change["attributes"]["reason"] == "access_revoked"
    assert change["source_event"] == "identity.session_revoked"


def test_member_added_and_removed():
    added = feeds.translate_event("access.member_added", {"user_id": "u-1", "role": "editor"})
    assert added["action"] == "upsert"
    assert added["attributes"]["role"] == "editor"
    removed = feeds.translate_event("access.member_removed", {"user_id": "u-1"})
    assert removed["action"] == "remove"


def test_workflow_agent_and_connector_families():
    cases = [
        ("automation.workflow_created", {"workflow_id": "w-1"}, "workflow", "workflow", "upsert"),
        ("automation.run_requested", {"run_id": "r-1"}, "workflow", "workflow_run", "upsert"),
        ("automation.run_cancelled", {"run_id": "r-1"}, "workflow", "workflow_run", "remove"),
        ("agent_studio.agent_created", {"agent_id": "a-1"}, "agent_studio", "agent", "upsert"),
        ("agent_studio.agent_deleted", {"agent_id": "a-1"}, "agent_studio", "agent", "remove"),
        ("agent_studio.run_queued", {"run_id": "r-2"}, "agent_studio", "agent_run", "upsert"),
        ("agent.profile_run_completed", {"run_id": "r-3"}, "agent_studio", "agent_run", "upsert"),
        ("cloud_connector.created", {"connector_id": "c-1"}, "connectors", "connector", "upsert"),
        ("cloud_connector.deleted", {"connector_id": "c-1"}, "connectors", "connector", "remove"),
    ]
    for event_type, payload, feed, subject_kind, action in cases:
        change = feeds.translate_event(event_type, payload)
        assert (change["feed"], change["subject_kind"], change["action"]) == (feed, subject_kind, action), event_type


def test_generic_subject_key_fallback():
    change = feeds.translate_event("automation.workflow_created", {"subject_id": "w-9"})
    assert change["subject_ref"] == "w-9"
    change = feeds.translate_event("automation.workflow_created", {"id": "w-8"})
    assert change["subject_ref"] == "w-8"


def test_missing_subject_is_malformed():
    with pytest.raises(ValueError):
        feeds.translate_event("automation.workflow_created", {"name": "No id"})
    with pytest.raises(ValueError):
        feeds.translate_event("automation.workflow_created", "not-a-dict")
    with pytest.raises(ValueError):
        feeds.translate_event("automation.workflow_created", {"workflow_id": ""})


def test_secrets_never_ride_a_feed_change():
    change = feeds.translate_event("cloud_connector.created", {
        "connector_id": "c-1", "name": "Drive", "api_key": "sk-live-123",
        "status": "connected"})
    assert "api_key" not in change["attributes"]
    assert change["attributes"]["status"] == "connected"


def test_long_attribute_values_bounded():
    change = feeds.translate_event("agent_studio.agent_created", {
        "agent_id": "a-1", "label": "x" * 500})
    assert len(change["attributes"]["label"]) == 200


def test_name_defaults_to_subject_ref():
    change = feeds.translate_event("automation.workflow_created", {"workflow_id": "w-1"})
    assert change["name"] == "w-1"


def test_fingerprint_is_stable_and_state_sensitive():
    a = feeds.translate_event("automation.workflow_created", {"workflow_id": "w-1", "status": "active"})
    b = feeds.translate_event("automation.workflow_created", {"workflow_id": "w-1", "status": "active"})
    c = feeds.translate_event("automation.workflow_created", {"workflow_id": "w-1", "status": "paused"})
    assert feeds.feed_change_fingerprint(a) == feeds.feed_change_fingerprint(b)
    assert feeds.feed_change_fingerprint(a) != feeds.feed_change_fingerprint(c)
    assert len(feeds.feed_change_fingerprint(a)) == 64


def test_every_feed_event_has_a_known_family():
    for event_type, (family, subject_kind, action, key) in feeds.FEED_EVENTS.items():
        assert family in ("membership", "workflow", "agent_studio", "connectors")
        assert action in ("upsert", "remove")
        assert isinstance(key, str) and key
