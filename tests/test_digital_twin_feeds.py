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


"""Twin feeds through the governed-change path + trust/conflicts API
(Step K v2 slice 7).

DB-backed tests on a throwaway *_test database only. Proves the
feed application (pending GraphChange proposals, dedupe, skips,
no-privilege rule), the outbox handlers (tenant resolution, skip
without a tenant), and the read-only trust/conflicts API
(tenant scope via ``scope_for``, owner-only money, 404/401/422
behaviour).
"""

import asyncio
import uuid
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.company_graph.models import GraphChange
from app.db.session import test_engine_options
from app.digital_twin import feed_changes as feeds, feed_records, versions
from app.platform.handlers import _on_twin_feed_event
from app.platform.models import PlatformOutbox
from tests.conftest import TEST_DATABASE_URL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.test_company_graph import checked, member, tenant, user_id


def run(fn):
    """Run ``await fn(sessionmaker)`` in a fresh engine/loop."""

    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _snapshot():
    return {
        "entities": [
            {"id": "e-ayesha", "kind": "employee", "name": "Ayesha", "attributes": {}},
            {"id": "e-erp", "kind": "system", "name": "ERP", "attributes": {}},
        ],
        "relationships": [],
        "as_of": "2026-11-28T00:00:00Z",
        "stale_sources": ["finance"],
    }


async def _record_version(sm, tenant_id, freshness):
    async with sm() as db:
        summary = await versions.record_version(db, tenant_id, _snapshot(), source_freshness=freshness)
        await db.commit()
        return summary


async def _pending_changes(sm, tenant_id):
    async with sm() as db:
        rows = await db.scalars(select(GraphChange).where(
            GraphChange.tenant_id == tenant_id, GraphChange.status == "pending"))
        return list(rows)


def _workflow_change():
    return feeds.translate_event("automation.workflow_created", {
        "workflow_id": "wf-monthly-close", "name": "Monthly close", "status": "active"})


def test_apply_feed_workflow_upsert_proposes_pending_record_change(admin_client):
    t = tenant(admin_client, "Twin Feeds Atlas")
    tid = UUID(t)
    result = run(lambda sm: _apply(sm, tid, _workflow_change()))
    assert result["skipped"] is None
    assert result["change_type"] == "record"
    assert result["base_version"] == 0
    changes = run(lambda sm: _pending_changes(sm, tid))
    assert len(changes) == 1
    payload = changes[0].payload
    assert payload["external_key"] == "twin-feed:workflow:wf-monthly-close"
    assert payload["kind"] == "workflow"
    assert payload["name"] == "Monthly close"
    assert payload["visibility"] == "restricted"
    assert payload["inferred"] is True
    assert payload["retired"] is False
    assert payload["evidence"][0]["source"] == "twin-feed:workflow"


async def _apply(sm, tenant_id, change):
    async with sm() as db:
        result = await feed_records.apply_feed(db, tenant_id, change)
        await db.commit()
        return result


def test_apply_feed_is_idempotent_for_identical_pending_change(admin_client):
    t = tenant(admin_client, "Twin Feeds Atlas Two")
    tid = UUID(t)
    first = run(lambda sm: _apply(sm, tid, _workflow_change()))
    second = run(lambda sm: _apply(sm, tid, _workflow_change()))
    assert first["skipped"] is None
    assert second["skipped"] == "duplicate_pending"
    assert second["change_id"] == first["change_id"]
    assert len(run(lambda sm: _pending_changes(sm, tid))) == 1


def test_apply_feed_remove_without_a_record_is_skipped(admin_client):
    t = tenant(admin_client, "Twin Feeds Atlas Three")
    tid = UUID(t)
    change = feeds.translate_event("automation.workflow_disabled", {"workflow_id": "wf-gone"})
    assert run(lambda sm: _apply(sm, tid, change))["skipped"] == "not_present"
    assert run(lambda sm: _pending_changes(sm, tid)) == []


def test_apply_feed_member_upsert_is_viewer_never_privilege(admin_client):
    t = tenant(admin_client, "Twin Feeds Atlas Four")
    tid = UUID(t)
    new_user = UUID(user_id(USER_EMAIL))
    change = feeds.translate_event("access.member_added", {
        "user_id": str(new_user), "role": "owner"})
    result = run(lambda sm: _apply(sm, tid, change))
    assert result["skipped"] is None
    assert result["change_type"] == "member"
    changes = run(lambda sm: _pending_changes(sm, tid))
    assert len(changes) == 1
    assert changes[0].payload["role"] == "viewer"  # event asked for owner; twin proposes viewer
    assert changes[0].payload["active"] is True


def test_apply_feed_member_remove_skips_non_member(admin_client):
    t = tenant(admin_client, "Twin Feeds Atlas Five")
    tid = UUID(t)
    change = feeds.translate_event("access.member_removed", {"user_id": str(uuid.uuid4())})
    assert run(lambda sm: _apply(sm, tid, change))["skipped"] == "not_a_member"


def test_apply_feed_member_remove_proposes_deactivation(admin_client):
    t = tenant(admin_client, "Twin Feeds Atlas Six")
    tid = UUID(t)
    member(admin_client, t, USER_EMAIL, role="editor")
    change = feeds.translate_event("identity.session_revoked", {"user_id": user_id(USER_EMAIL)})
    result = run(lambda sm: _apply(sm, tid, change))
    assert result["skipped"] is None
    changes = run(lambda sm: _pending_changes(sm, tid))
    assert len(changes) == 1
    assert changes[0].payload["role"] == "editor"  # existing role preserved
    assert changes[0].payload["active"] is False


def test_apply_feed_without_requester_raises(admin_client):
    t = tenant(admin_client, "Twin Feeds Atlas Seven")
    tid = UUID(t)

    async def strip_owners(sm):
        async with sm() as db:
            await db.execute(text("DELETE FROM company_graph_members WHERE tenant_id=:t"), {"t": tid})
            await db.commit()

    run(strip_owners)
    with pytest.raises(ValueError, match="no requester"):
        run(lambda sm: _apply(sm, tid, _workflow_change()))


def test_handler_proposes_change_for_live_revoked_event(admin_client):
    t = tenant(admin_client, "Twin Feeds Atlas Eight")
    tid = UUID(t)
    member(admin_client, t, USER_EMAIL, role="viewer")
    # NOTE: user_id() runs its own asyncio.run — it must be called
    # outside the async fire() below, never inside a running loop.
    revoked_user_id = user_id(USER_EMAIL)

    async def fire(sm):
        async with sm() as db:
            event = PlatformOutbox(
                event_type="identity.session_revoked", scope_id=tid,
                payload={"user_id": revoked_user_id, "reason": "access_revoked",
                         "tenant_id": str(tid)},
                trace_id=uuid.uuid4())
            db.add(event)
            await db.flush()
            await _on_twin_feed_event(db, event)
            await db.commit()

    run(fire)
    changes = run(lambda sm: _pending_changes(sm, tid))
    assert len(changes) == 1
    assert changes[0].change_type == "member"
    assert changes[0].payload["active"] is False


def test_handler_skips_event_with_no_graph_tenant(admin_client):
    t = tenant(admin_client, "Twin Feeds Atlas Nine")
    tid = UUID(t)

    async def fire(sm):
        async with sm() as db:
            event = PlatformOutbox(
                event_type="identity.session_revoked", scope_id=uuid.uuid4(),
                payload={"user_id": str(uuid.uuid4())},
                trace_id=uuid.uuid4())
            db.add(event)
            await db.flush()
            await _on_twin_feed_event(db, event)  # must not raise
            await db.commit()

    run(fire)
    assert run(lambda sm: _pending_changes(sm, tid)) == []


def _seed_trust_versions(admin_client):
    t = tenant(admin_client, "Twin Trust Atlas")
    tid = UUID(t)
    run(lambda sm: _record_version(sm, tid, [
        {"name": "company_graph", "stale": False},
        {"name": "finance", "stale": True},
    ]))
    return t


def test_api_trust_latest_score_and_band(admin_client):
    t = _seed_trust_versions(admin_client)
    body = checked(admin_client.get(f"/api/digital-twin/{t}/trust"))
    assert body["tenant_id"] == t
    assert body["trust"]["score"] == 60  # graph 30 fresh, finance 20 stale
    assert body["trust"]["band"] == "review"
    assert body["trust"]["coverage"] == 0.5
    assert body["trust"]["stale_sources"] == ["finance"]
    assert "fingerprint" in body["trust"]


def test_api_trust_history_per_version(admin_client):
    t = _seed_trust_versions(admin_client)
    body = checked(admin_client.get(f"/api/digital-twin/{t}/trust/history"))
    assert body["count"] == 1
    assert body["history"][0]["score"] == 60
    assert body["history"][0]["band"] == "review"


def test_api_trust_no_versions_is_404(admin_client):
    t = tenant(admin_client, "Twin Trust Atlas Empty")
    response = admin_client.get(f"/api/digital-twin/{t}/trust")
    assert response.status_code == 404, response.text


def test_api_trust_non_member_is_404(admin_client):
    t = _seed_trust_versions(admin_client)
    login(admin_client, USER2_EMAIL)  # seeded user, not a tenant member
    response = admin_client.get(f"/api/digital-twin/{t}/trust")
    assert response.status_code == 404, response.text


def test_api_trust_unauthenticated_is_401(client):
    # NOTE: `client` must be the only client fixture here — admin_client
    # IS the same TestClient object after login, so taking both would
    # authenticate this call too.
    response = client.get(f"/api/digital-twin/{uuid.uuid4()}/trust")
    assert response.status_code == 401, response.text


def test_api_conflicts_detect_owner_sees_both_sides(admin_client):
    t = tenant(admin_client, "Twin Conflicts Atlas")
    views = [
        {"source": "company_graph", "entities": [
            {"id": "b-1", "kind": "invoice_batch", "name": "Batch 1", "attributes": {"amount": 100}}]},
        {"source": "finance", "entities": [
            {"id": "b-1", "kind": "invoice_batch", "name": "Batch 1", "attributes": {"amount": 120}}]},
    ]
    body = checked(admin_client.post(f"/api/digital-twin/{t}/conflicts/detect", json={"source_views": views}))
    assert body["conflicts"]["total"] == 1
    assert body["conflicts"]["conflicts"][0]["values"] == {"company_graph": "100", "finance": "120"}
    assert "money_redacted" not in body["conflicts"]


def test_api_conflicts_detect_viewer_money_is_redacted(admin_client):
    t = tenant(admin_client, "Twin Conflicts Atlas Two")
    member(admin_client, t, USER_EMAIL, role="viewer")
    login(admin_client, USER_EMAIL)
    views = [
        {"source": "company_graph", "entities": [
            {"id": "b-1", "kind": "invoice_batch", "name": "Batch 1", "attributes": {"amount": 100}}]},
        {"source": "finance", "entities": [
            {"id": "b-1", "kind": "invoice_batch", "name": "Batch 1", "attributes": {"amount": 120}}]},
    ]
    body = checked(admin_client.post(f"/api/digital-twin/{t}/conflicts/detect", json={"source_views": views}))
    assert body["conflicts"]["conflicts"][0]["values"] == {
        "company_graph": "<redacted>", "finance": "<redacted>"}
    assert body["conflicts"]["money_redacted"] is True


def test_api_conflicts_detect_malformed_is_422(admin_client):
    t = tenant(admin_client, "Twin Conflicts Atlas Three")
    response = admin_client.post(f"/api/digital-twin/{t}/conflicts/detect",
                                 json={"source_views": [{"source": "finance", "entities": [{"kind": "x"}]}]})
    assert response.status_code == 422, response.text
