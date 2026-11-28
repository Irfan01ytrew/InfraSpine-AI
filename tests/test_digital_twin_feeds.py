"""Live sync feeds (slice 3): outbox -> governed graph-proposal tests.

Pure translation tests plus ``apply_feed`` tests against a stubbed
``discovery.synchronize`` (the governed-change path). No database rows
are created by these tests themselves; the repo conftest session
fixture still migrates the dedicated *_test database, as for every
test module. Nothing here writes a GraphRecord, approves a change, or
pays anything — the tests assert exactly that boundary.
"""

import asyncio

import pytest

from app.digital_twin.feeds import (
    SUPPORTED_EVENTS,
    apply_feed,
    feed_idempotency_key,
    make_feed_handler,
    register_twin_feeds,
    translate_event,
)


class _Event:
    def __init__(self, event_id, event_type, payload):
        self.id = event_id
        self.event_type = event_type
        self.payload = payload


# --- idempotency keys --------------------------------------------------------

def test_idempotency_key_deterministic_and_short():
    key = feed_idempotency_key("digital_twin_feed", "finance.batch_started", 42)
    assert key == feed_idempotency_key("digital_twin_feed", "finance.batch_started", 42)
    assert feed_idempotency_key("digital_twin_feed", "finance.batch_started", 43) != key
    assert len(key) <= 120
    long_key = feed_idempotency_key("x" * 100, "finance.batch_started", "y" * 100)
    assert len(long_key) <= 120
    assert long_key.startswith("twin-feed:")


# --- translate_event: supported types ---------------------------------------

def test_batch_started_translates_to_batch_run_and_edge():
    sync = translate_event("finance.batch_started",
                           {"batch_id": "b-1", "run_id": "r-1", "round": 2}, event_id=7)
    assert sync is not None
    assert sync.idempotency_key == feed_idempotency_key(
        "digital_twin_feed", "finance.batch_started", 7)
    assert sync.visibility == "restricted"  # owner-only until plan §7 is decided
    kinds = {(i.key, i.kind) for i in sync.items}
    assert ("fin-batch:b-1", "invoice_batch") in kinds
    assert ("wf-run:r-1", "workflow") in kinds
    edge = next(i for i in sync.items if i.kind == "depends_on")
    assert (edge.source_key, edge.target_key) == ("wf-run:r-1", "fin-batch:b-1")
    batch = next(i for i in sync.items if i.key == "fin-batch:b-1")
    assert batch.attributes["status"] == "verifying"
    assert batch.attributes["round"] == 2
    assert all(i.quote for i in sync.items)  # SyncItem requires a quote


def test_payment_verified_and_failed_statuses():
    verified = translate_event("finance.payment_verified",
                               {"payment_id": "p-1", "batch_id": "b-1",
                                "external_ref": "ext-123"}, event_id=8)
    pay = next(i for i in verified.items if i.key == "fin-pay:p-1")
    assert pay.attributes["status"] == "verified"
    assert pay.attributes["external_ref"] == "ext-123"
    assert any(i.kind == "depends_on" and i.source_key == "fin-pay:p-1"
               and i.target_key == "fin-batch:b-1" for i in verified.items)

    failed = translate_event("finance.payment_failed",
                             {"payment_id": "p-1", "batch_id": "b-1", "code": "bank_timeout"},
                             event_id=9)
    pay = next(i for i in failed.items if i.key == "fin-pay:p-1")
    assert pay.attributes["status"] == "failed"
    assert pay.attributes["failure_code"] == "bank_timeout"


def test_workflow_step_verified_and_blocked():
    ok = translate_event("workflow.step_verified",
                         {"run_id": "r-1", "node_id": "n-5", "handler": "finance.verify"},
                         event_id=10)
    run = next(i for i in ok.items if i.key == "wf-run:r-1")
    assert run.attributes["status"] == "running"
    assert run.attributes["last_verified_node"] == "n-5"

    blocked = translate_event("workflow.blocked",
                              {"run_id": "r-1", "node_id": "n-5", "reason": "person_blocked"},
                              event_id=11)
    run = next(i for i in blocked.items if i.key == "wf-run:r-1")
    assert run.attributes["status"] == "blocked"
    assert run.attributes["blocked_reason"] == "person_blocked"


def test_supported_event_set_matches_plan():
    assert SUPPORTED_EVENTS == {
        "finance.batch_started", "finance.payment_verified",
        "finance.payment_failed", "workflow.step_verified", "workflow.blocked",
    }


# --- translate_event: ignored and invalid -----------------------------------

@pytest.mark.parametrize("event_type", [
    "approval.requested", "approval.decided", "identity.session_revoked",
    "document.acl_changed", "document.deleted", "action.verified",
    "unknown.event",
])
def test_unsupported_events_are_ignored_not_errors(event_type):
    assert translate_event(event_type, {"anything": True}, event_id=1) is None


@pytest.mark.parametrize("event_type,payload", [
    ("finance.batch_started", {"batch_id": "b-1"}),  # missing run_id
    ("finance.batch_started", {"batch_id": "", "run_id": "r-1"}),
    ("finance.payment_verified", {"payment_id": "p-1"}),  # missing batch_id
    ("workflow.blocked", {"run_id": "r-1"}),  # missing node_id
    ("workflow.step_verified", {"run_id": 42, "node_id": "n-1"}),  # non-string id
    ("finance.batch_started", "not-a-dict"),
    ("finance.batch_started", None),
])
def test_malformed_supported_event_raises(event_type, payload):
    with pytest.raises(ValueError):
        translate_event(event_type, payload, event_id=3)


def test_same_event_replays_to_same_sync_input():
    from datetime import datetime, timezone
    moment = datetime(2026, 11, 28, tzinfo=timezone.utc)
    first = translate_event("finance.batch_started",
                            {"batch_id": "b-1", "run_id": "r-1"}, event_id=5,
                            observed_at=moment)
    second = translate_event("finance.batch_started",
                             {"batch_id": "b-1", "run_id": "r-1"}, event_id=5,
                             observed_at=moment)
    assert first.idempotency_key == second.idempotency_key
    assert first.model_dump_json() == second.model_dump_json()


# --- apply_feed: governed path only ------------------------------------------

class _StubSync:
    def __init__(self):
        self.calls = []

    async def __call__(self, db, scope, source_id, sync_input, ctx):
        self.calls.append({"db": db, "scope": scope, "source_id": source_id,
                           "sync_input": sync_input, "ctx": ctx})
        return {"proposal_ids": ["change-1"], "unchanged_ids": [],
                "conflicts": [], "approval_required": True}


def test_apply_feed_calls_synchronize_once_with_translated_input(monkeypatch):
    return asyncio.run(_test_apply_feed_calls_synchronize_once_with_translated_input(monkeypatch))


async def _test_apply_feed_calls_synchronize_once_with_translated_input(monkeypatch):
    stub = _StubSync()
    monkeypatch.setattr("app.digital_twin.feeds.synchronize", stub)
    event = _Event(21, "finance.batch_started", {"batch_id": "b-1", "run_id": "r-1"})
    result = await apply_feed(object(), object(), source_id="src-1", event=event, ctx="ctx-1")
    assert result["status"] == "proposed"
    assert result["proposal_ids"] == ["change-1"]
    assert result["approval_required"] is True
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["source_id"] == "src-1" and call["ctx"] == "ctx-1"
    assert call["sync_input"].idempotency_key == feed_idempotency_key(
        "digital_twin_feed", "finance.batch_started", 21)
    # The handler never decides/approves: no GraphRecord write, no decide call.
    assert "decide" not in str(call)


def test_apply_feed_ignores_unsupported_events_without_db_touch(monkeypatch):
    return asyncio.run(_test_apply_feed_ignores_unsupported_events_without_db_touch(monkeypatch))


async def _test_apply_feed_ignores_unsupported_events_without_db_touch(monkeypatch):
    stub = _StubSync()
    monkeypatch.setattr("app.digital_twin.feeds.synchronize", stub)
    event = _Event(22, "approval.decided", {"approval_id": "a-1", "status": "approved"})
    result = await apply_feed(object(), object(), source_id="src-1", event=event, ctx="ctx-1")
    assert result == {"status": "ignored", "event_type": "approval.decided", "event_id": 22}
    assert stub.calls == []


def test_apply_feed_invalid_payload_raises_before_synchronize(monkeypatch):
    return asyncio.run(_test_apply_feed_invalid_payload_raises_before_synchronize(monkeypatch))


async def _test_apply_feed_invalid_payload_raises_before_synchronize(monkeypatch):
    stub = _StubSync()
    monkeypatch.setattr("app.digital_twin.feeds.synchronize", stub)
    event = _Event(23, "finance.payment_verified", {"payment_id": "p-1"})
    with pytest.raises(ValueError):
        await apply_feed(object(), object(), source_id="src-1", event=event, ctx="ctx-1")
    assert stub.calls == []


def test_replay_same_event_reuses_idempotency_key(monkeypatch):
    return asyncio.run(_test_replay_same_event_reuses_idempotency_key(monkeypatch))


async def _test_replay_same_event_reuses_idempotency_key(monkeypatch):
    stub = _StubSync()
    monkeypatch.setattr("app.digital_twin.feeds.synchronize", stub)
    event = _Event(24, "workflow.blocked",
                   {"run_id": "r-1", "node_id": "n-1", "reason": "failed"})
    await apply_feed(object(), object(), source_id="src-1", event=event, ctx="ctx-1")
    await apply_feed(object(), object(), source_id="src-1", event=event, ctx="ctx-1")
    keys = [c["sync_input"].idempotency_key for c in stub.calls]
    assert keys[0] == keys[1]  # GraphSync replay dedupes on this key


# --- handler factory / registration ------------------------------------------

def test_make_feed_handler_binds_source_scope_ctx(monkeypatch):
    return asyncio.run(_test_make_feed_handler_binds_source_scope_ctx(monkeypatch))


async def _test_make_feed_handler_binds_source_scope_ctx(monkeypatch):
    stub = _StubSync()
    monkeypatch.setattr("app.digital_twin.feeds.synchronize", stub)
    scope, ctx = object(), object()
    handler = make_feed_handler(source_id="src-9", graph_scope=scope, ctx=ctx)
    await handler(object(), _Event(30, "finance.payment_failed",
                                  {"payment_id": "p-2", "batch_id": "b-2", "code": "x"}))
    assert len(stub.calls) == 1
    assert stub.calls[0]["source_id"] == "src-9"
    assert stub.calls[0]["scope"] is scope and stub.calls[0]["ctx"] is ctx


def test_register_twin_feeds_registers_each_supported_type(monkeypatch):
    registered = []
    monkeypatch.setattr("app.digital_twin.feeds.register_handler",
                        lambda event_type: (lambda fn: registered.append(event_type) or fn))
    assert register_twin_feeds(source_id="src-1", graph_scope=object(), ctx=None) == sorted(SUPPORTED_EVENTS)
    assert sorted(registered) == sorted(SUPPORTED_EVENTS)


def test_importing_feeds_registers_nothing():
    # Importing the module must not mutate the global outbox registry.
    from app.platform.outbox import HANDLERS
    for event_type in SUPPORTED_EVENTS:
        assert not any(getattr(h, "__module__", "").startswith("app.digital_twin")
                       for h in HANDLERS.get(event_type, []))
