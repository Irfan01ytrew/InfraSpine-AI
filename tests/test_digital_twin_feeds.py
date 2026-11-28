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
from app.digital_twin import feed_records, feeds, versions
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
