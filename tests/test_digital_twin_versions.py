"""Twin snapshot versions + drift history API (Step K v2 slice 2).

DB-backed tests on a throwaway *_test database only. Proves the
version store (record / idempotency / drift computation / list /
retention pruning / validation) and the read-only API (tenant scope
via ``scope_for``, owner-only money, 404/401/422 behaviour).
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.session import test_engine_options
from app.db.twin_snapshot_models import TwinSnapshotVersion
from app.digital_twin import drift, versions
from tests.conftest import ADMIN_EMAIL, CSRF, TEST_DATABASE_URL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.test_company_graph import checked, member, tenant


def run(fn):
    """Run ``await fn(sessionmaker)`` in a fresh engine/loop."""

    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _snapshot(*, erp_name="ERP", batch_amount=100000, extra_entities=(),
              entities=None, fingerprint=None):
    if entities is None:
        entities = [
            {"id": "e-ayesha", "kind": "employee", "name": "Ayesha", "attributes": {"department": "Finance"}},
            {"id": "e-erp", "kind": "system", "name": erp_name, "attributes": {}},
            {"id": "e-batch", "kind": "invoice_batch", "name": "Batch 1",
             "attributes": {"amount": batch_amount, "currency": "PKR"}},
        ]
        entities.extend(extra_entities)
    snap = {
        "entities": entities,
        "relationships": [
            {"id": "r-1", "source_id": "e-ayesha", "target_id": "e-erp",
             "kind": "owns", "confidence": 1.0, "evidence": []},
        ],
        "as_of": "2026-11-28T00:00:00Z",
        "stale_sources": [],
    }
    if fingerprint is not None:
        snap["fingerprint"] = fingerprint
    return snap


async def _record(sm, tenant_id, snapshot, **kwargs):
    async with sm() as db:
        summary = await versions.record_version(db, tenant_id, snapshot, **kwargs)
        await db.commit()
        return summary


async def _list(sm, tenant_id, **kwargs):
    async with sm() as db:
        return await versions.list_versions(db, tenant_id, **kwargs)


async def _drift(sm, tenant_id, fingerprint):
    async with sm() as db:
        return await versions.get_drift(db, tenant_id, fingerprint)


async def _version(sm, tenant_id, fingerprint):
    async with sm() as db:
        return await versions.get_version(db, tenant_id, fingerprint)


def _row_count():
    """Total twin version rows (sync: runs the count in a fresh loop)."""
    async def count(sm):
        async with sm() as db:
            return (await db.scalar(select(func.count()).select_from(TwinSnapshotVersion))) or 0

    return run(count)


# ---------------------------------------------------------------- store


def test_record_first_version_has_no_drift():
    tid = uuid.uuid4()
    snap = _snapshot()
    summary = run(lambda sm: _record(sm, tid, snap, source_freshness=[{"name": "company_graph", "status": "fresh"}]))
    assert summary["fingerprint"] == drift.snapshot_fingerprint(snap)
    assert summary["drift_score"] is None
    assert summary["trust"] is None
    assert summary["is_first_version"] is True
    assert summary["entity_count"] == 3
    assert summary["relationship_count"] == 1
    assert summary["source_freshness"] == [{"name": "company_graph", "status": "fresh"}]
    assert "snapshot" not in summary and "money" not in summary
    assert _row_count() == 1


def test_record_second_version_computes_drift():
    tid = uuid.uuid4()
    first = run(lambda sm: _record(sm, tid, _snapshot()))
    second = run(lambda sm: _record(
        sm, tid,
        _snapshot(erp_name="ERP v2", batch_amount=120000,
                  extra_entities=[{"id": "e-backup", "kind": "employee", "name": "Backup", "attributes": {}}]),
    ))
    assert second["fingerprint"] != first["fingerprint"]
    assert second["is_first_version"] is False
    # Added 1 entity (2 pts) + changed 2 entities (6 pts) + money
    # share 20000/120000 (3 pts) = 11, trustworthy.
    assert second["drift_score"] == 11
    assert second["trust"] == "trustworthy"
    assert second["entity_count"] == 4

    async def read_drift(sm):
        async with sm() as db:
            result = await versions.get_drift(db, tid, second["fingerprint"])
            return result

    result = run(read_drift)
    assert result["is_first_version"] is False
    report = result["drift"]
    assert report["fingerprint_old"] == first["fingerprint"]
    assert report["fingerprint_new"] == second["fingerprint"]
    assert report["entities"]["added"] == ["e-backup"]
    assert sorted(e["id"] for e in report["entities"]["changed"]) == ["e-batch", "e-erp"]
    assert report["money"]["delta"]["totals"] == {"PKR": 20000.0}
    assert report["money"]["old"]["totals"] == {"PKR": 100000.0}
    assert report["money"]["new"]["totals"] == {"PKR": 120000.0}


def test_record_same_snapshot_is_idempotent():
    tid = uuid.uuid4()
    snap = _snapshot()
    first = run(lambda sm: _record(sm, tid, snap))
    second = run(lambda sm: _record(sm, tid, snap))
    assert first["id"] == second["id"]
    assert first["fingerprint"] == second["fingerprint"]
    assert _row_count() == 1


def test_record_forged_stored_fingerprint_is_ignored():
    tid = uuid.uuid4()
    snap = _snapshot(fingerprint="0" * 64)
    summary = run(lambda sm: _record(sm, tid, snap))
    assert summary["fingerprint"] == drift.snapshot_fingerprint(
        {k: v for k, v in snap.items() if k != "fingerprint"})
    assert summary["fingerprint"] != "0" * 64

    async def read_row(sm):
        async with sm() as db:
            row = await db.scalar(select(TwinSnapshotVersion))
            return row.snapshot

    stored = run(read_row)
    assert "fingerprint" not in stored  # stored copy drops the label


def test_record_invalid_snapshot_writes_nothing():
    tid = uuid.uuid4()
    bad = _snapshot(entities=[{"id": "e-x", "kind": "employee", "name": "X", "attributes": {}},
                              {"id": "e-x", "kind": "employee", "name": "Y", "attributes": {}}])
    with pytest.raises(ValueError, match="Duplicate entity id"):
        run(lambda sm: _record(sm, tid, bad))
    assert _row_count() == 0


def test_record_invalid_source_freshness_writes_nothing():
    tid = uuid.uuid4()
    with pytest.raises(ValueError, match="non-empty string 'name'"):
        run(lambda sm: _record(sm, tid, _snapshot(), source_freshness=[{"status": "fresh"}]))
    assert _row_count() == 0


def test_list_versions_newest_first_and_limit():
    tid = uuid.uuid4()
    summaries = [
        run(lambda sm, n=n: _record(sm, tid, _snapshot(erp_name=f"ERP v{n}")))
        for n in range(3)
    ]
    listed = run(lambda sm: _list(sm, tid))
    assert [s["fingerprint"] for s in listed] == [s["fingerprint"] for s in reversed(summaries)]
    assert [s["seq"] for s in listed] == sorted((s["seq"] for s in listed), reverse=True)
    limited = run(lambda sm: _list(sm, tid, limit=2))
    assert len(limited) == 2
    assert limited[0]["fingerprint"] == summaries[-1]["fingerprint"]


def test_versions_are_isolated_per_tenant():
    snap = _snapshot()
    first_a = run(lambda sm: _record(sm, uuid.uuid4(), snap))
    tid_b = uuid.uuid4()
    first_b = run(lambda sm: _record(sm, tid_b, snap))
    assert first_a["fingerprint"] == first_b["fingerprint"]
    assert first_a["id"] != first_b["id"]
    assert _row_count() == 2
    listed_b = run(lambda sm: _list(sm, tid_b))
    assert len(listed_b) == 1
    assert listed_b[0]["is_first_version"] is True  # not diffed against tenant A


def test_get_drift_first_version_is_null():
    tid = uuid.uuid4()
    summary = run(lambda sm: _record(sm, tid, _snapshot()))
    result = run(lambda sm: _drift(sm, tid, summary["fingerprint"]))
    assert result["drift"] is None
    assert result["is_first_version"] is True
    assert result["version"]["fingerprint"] == summary["fingerprint"]


def test_get_version_unknown_and_malformed_fingerprint():
    tid = uuid.uuid4()
    run(lambda sm: _record(sm, tid, _snapshot()))
    assert run(lambda sm: _version(sm, tid, "a" * 64)) is None
    assert run(lambda sm: _drift(sm, tid, "a" * 64)) is None
    with pytest.raises(ValueError, match="Invalid twin version fingerprint"):
        run(lambda sm: _version(sm, tid, "not-hex"))


def test_prune_keeps_newest_30_on_record():
    tid = uuid.uuid4()
    summaries = [
        run(lambda sm, n=n: _record(sm, tid, _snapshot(erp_name=f"ERP v{n}")))
        for n in range(33)
    ]
    assert _row_count() == 30  # prune runs inside every record
    listed = run(lambda sm: _list(sm, tid, limit=100))
    kept = {s["fingerprint"] for s in listed}
    assert kept == {s["fingerprint"] for s in summaries[-30:]}
    assert summaries[0]["fingerprint"] not in kept


def test_prune_drops_versions_older_than_90_days():
    tid = uuid.uuid4()
    old = run(lambda sm: _record(sm, tid, _snapshot()))
    run_db(lambda conn: conn.execute(
        text("UPDATE twin_snapshot_versions SET created_at = now() - interval '100 days' WHERE id = :id"),
        {"id": old["id"]},
    ))
    run(lambda sm: _record(sm, tid, _snapshot(erp_name="ERP v2")))
    assert _row_count() == 1
    listed = run(lambda sm: _list(sm, tid))
    assert listed[0]["fingerprint"] != old["fingerprint"]


def test_prune_rejects_bad_args():
    tid = uuid.uuid4()
    run(lambda sm: _record(sm, tid, _snapshot()))

    async def bad_keep(sm):
        async with sm() as db:
            await versions.prune_versions(db, tid, keep=0)

    with pytest.raises(ValueError, match="keep must be at least 1"):
        run(bad_keep)

    async def bad_age(sm):
        async with sm() as db:
            await versions.prune_versions(db, tid, max_age_days=-1)

    with pytest.raises(ValueError, match="must not be negative"):
        run(bad_age)


# ------------------------------------------------------------------- API


def _seed_versions(admin_client, count=2):
    """Tenant + `count` recorded versions; returns (tenant_id, summaries)."""
    t = tenant(admin_client, "Twin Versions Atlas")
    tid = UUID(t)
    summaries = []
    for n in range(count):
        snap = _snapshot(
            erp_name=f"ERP v{n}",
            batch_amount=100000 + 20000 * n,
            extra_entities=(
                [{"id": "e-backup", "kind": "employee", "name": "Backup", "attributes": {}}]
                if n == 1 else ()
            ),
        )
        summaries.append(run(lambda sm, s=snap: _record(sm, tid, s)))
    return t, summaries


def test_api_list_versions_owner(admin_client):
    t, summaries = _seed_versions(admin_client)
    body = checked(admin_client.get(f"/api/digital-twin/{t}/versions"))
    assert body["tenant_id"] == t
    assert body["count"] == 2
    assert [v["fingerprint"] for v in body["versions"]] == [s["fingerprint"] for s in reversed(summaries)]
    for version in body["versions"]:
        assert "snapshot" not in version
        assert "money" not in version


def test_api_drift_owner_sees_money(admin_client):
    t, summaries = _seed_versions(admin_client)
    body = checked(admin_client.get(f"/api/digital-twin/{t}/versions/{summaries[1]['fingerprint']}/drift"))
    assert body["is_first_version"] is False
    assert body["drift"]["money"]["delta"]["totals"] == {"PKR": 20000.0}
    assert body["drift"]["drift_score"] == 11


def test_api_drift_first_version_is_null(admin_client):
    t, summaries = _seed_versions(admin_client)
    body = checked(admin_client.get(f"/api/digital-twin/{t}/versions/{summaries[0]['fingerprint']}/drift"))
    assert body["drift"] is None
    assert body["is_first_version"] is True


def test_api_drift_viewer_money_is_redacted(admin_client):
    t, summaries = _seed_versions(admin_client)
    member(admin_client, t, USER_EMAIL, role="viewer")
    login(admin_client, USER_EMAIL)
    body = checked(admin_client.get(f"/api/digital-twin/{t}/versions/{summaries[1]['fingerprint']}/drift"))
    assert body["drift"]["money"] is None
    assert body["drift"]["money_redacted"] is True
    assert body["drift"]["drift_score"] == 11  # score/trust are not amounts
    listed = checked(admin_client.get(f"/api/digital-twin/{t}/versions"))
    assert listed["count"] == 2


def test_api_versions_unknown_tenant_is_404(admin_client):
    response = admin_client.get(f"/api/digital-twin/{uuid.uuid4()}/versions")
    assert response.status_code == 404, response.text


def test_api_versions_non_member_is_404(admin_client):
    t, _ = _seed_versions(admin_client)
    login(admin_client, USER2_EMAIL)  # seeded user, not a tenant member
    response = admin_client.get(f"/api/digital-twin/{t}/versions")
    assert response.status_code == 404, response.text


def test_api_versions_unauthenticated_is_401(client):
    response = client.get(f"/api/digital-twin/{uuid.uuid4()}/versions")
    assert response.status_code == 401, response.text


def test_api_drift_bad_fingerprint_is_422(admin_client):
    t, _ = _seed_versions(admin_client)
    response = admin_client.get(f"/api/digital-twin/{t}/versions/not-hex/drift")
    assert response.status_code == 422, response.text


def test_api_drift_unknown_fingerprint_is_404(admin_client):
    t, _ = _seed_versions(admin_client)
    response = admin_client.get(f"/api/digital-twin/{t}/versions/{'a' * 64}/drift")
    assert response.status_code == 404, response.text


def test_api_drift_viewer_cannot_see_other_tenant_versions(admin_client):
    t, summaries = _seed_versions(admin_client)
    other = tenant(admin_client, "Twin Versions Atlas Two")
    login(admin_client, USER_EMAIL)
    response = admin_client.get(f"/api/digital-twin/{other}/versions/{summaries[1]['fingerprint']}/drift")
    assert response.status_code == 404, response.text
