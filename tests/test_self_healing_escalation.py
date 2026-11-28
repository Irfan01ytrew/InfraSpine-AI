"""Step W slice 4 — escalation channel: delivery record, acknowledgement,
out-of-band file fallback, executor wiring, and the admin API (DB + HTTP tests).
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import platform
from app.core.errors import InvalidInputError, NotFoundError
from app.db.models import AuditLog, User
from app.db.session import test_engine_options as _test_engine_options
from app.self_healing.detector import AttemptHistory, Incident
from app.self_healing.escalation import (
    acknowledge_escalation,
    build_escalation,
    escalate_out_of_band,
    list_escalations,
    record_escalation,
)
from app.self_healing.executor import execute_remedy
from app.self_healing.models import SelfHealingEscalation, SelfHealingIncident
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, USER_EMAIL, login

pytestmark = pytest.mark.usefixtures("clean_tables")


def run(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _incident(key="api_down", kind="api_down", severity="critical", component="api"):
    return Incident(
        key=key, kind=kind, severity=severity, component=component,
        summary="API is down.", evidence="api probe status=down age=42s",
    )


async def _principal(db, email):
    user = await db.scalar(select(User).where(User.email == email))
    return platform.Principal(user.id, user.email, "user", "web", user.auth_version)


async def _grant_read(db):
    """Give the admin the explicit self_healing.read grant on the org scope."""
    admin = await _principal(db, ADMIN_EMAIL)
    org = await platform.ensure_org_scope(db)
    await platform.ensure_role(db, "ops_reader", {"self_healing.read"})
    admin_id = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
    await platform.grant_membership(db, admin, user_id=admin_id, scope_id=org.id, role_key="ops_reader")
    await db.commit()


# --------------------------------------------------------------------------- service

def test_record_escalation_creates_row():
    async def go(sm):
        async with sm() as db:
            row = await record_escalation(db, _incident(), AttemptHistory(attempts=2))
            await db.commit()
            assert row.incident_key == "api_down"
            assert row.channel == "admin_ui"
            assert row.attempts == 2
            assert row.acknowledged_at is None
            assert row.severity == "critical"
            stored = await db.scalar(
                select(SelfHealingEscalation).where(SelfHealingEscalation.incident_key == "api_down")
            )
            assert stored is not None and stored.id == row.id
    run(go)


def test_record_escalation_reopen_clears_acknowledgement():
    async def go(sm):
        async with sm() as db:
            principal = await _principal(db, ADMIN_EMAIL)
            await record_escalation(db, _incident())
            await db.commit()
            await acknowledge_escalation(db, principal, "api_down")
            again = await db.scalar(
                select(SelfHealingEscalation).where(SelfHealingEscalation.incident_key == "api_down")
            )
            assert again.acknowledged_at is not None
            await record_escalation(db, _incident(), AttemptHistory(attempts=1))
            await db.commit()
            reopened = await db.scalar(
                select(SelfHealingEscalation).where(SelfHealingEscalation.incident_key == "api_down")
            )
            assert reopened.acknowledged_at is None
            assert reopened.acknowledged_by is None
            assert reopened.attempts == 1
    run(go)


def test_record_escalation_rejects_unknown_channel():
    async def go(sm):
        async with sm() as db:
            with pytest.raises(InvalidInputError):
                await record_escalation(db, _incident(), channel="whatsapp")
    run(go)


def test_list_escalations_filters_acknowledged():
    async def go(sm):
        async with sm() as db:
            principal = await _principal(db, ADMIN_EMAIL)
            await record_escalation(db, _incident("api_down"))
            await record_escalation(db, _incident("db_down", "database_unreachable", "critical", "database"))
            await db.commit()
            await acknowledge_escalation(db, principal, "api_down")
            unacked = await list_escalations(db, acknowledged=False)
            acked = await list_escalations(db, acknowledged=True)
            assert [row.incident_key for row in unacked] == ["db_down"]
            assert [row.incident_key for row in acked] == ["api_down"]
            assert len(await list_escalations(db)) == 2
    run(go)


def test_acknowledge_missing_key_404():
    async def go(sm):
        async with sm() as db:
            principal = await _principal(db, ADMIN_EMAIL)
            with pytest.raises(NotFoundError):
                await acknowledge_escalation(db, principal, "no_such_incident")
    run(go)


def test_acknowledge_writes_audit():
    async def go(sm):
        async with sm() as db:
            principal = await _principal(db, ADMIN_EMAIL)
            await record_escalation(db, _incident())
            await db.commit()
            await acknowledge_escalation(db, principal, "api_down")
            events = list(await db.scalars(
                select(AuditLog).where(AuditLog.action == "self_healing.escalation_acknowledged")
            ))
            assert len(events) == 1
    run(go)


def test_build_escalation_scrubs_secrets():
    report = build_escalation(
        Incident(key="x", kind="unknown", severity="high", component="api",
                 summary="weird", evidence="password= hunter2 and api_key=abc123"),
        AttemptHistory(attempts=1),
    )
    assert "hunter2" not in report["evidence"]
    assert "abc123" not in report["evidence"]
    assert report["channel"] == "admin_ui"
    assert report["needs_owner"] is True


def test_escalate_out_of_band_writes_scrubbed_jsonl(tmp_path):
    path = tmp_path / "escalations.jsonl"
    report = build_escalation(
        Incident(key="db_down", kind="database_unreachable", severity="critical",
                 component="database", summary="Database is unreachable.",
                 evidence="postgres://user:s3cret@host/db"),
    )
    assert escalate_out_of_band(report, path=path) is True
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    stored = json.loads(lines[0])
    assert stored["incident_key"] == "db_down"
    assert "s3cret" not in stored["evidence"]
    assert stored["needs_owner"] is True
    # A second escalation appends, it never overwrites the first line.
    assert escalate_out_of_band(report, path=path) is True
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_escalate_out_of_band_unconfigured_writes_nothing(tmp_path):
    report = build_escalation(_incident())
    assert escalate_out_of_band(report, path=None) is False
    assert escalate_out_of_band(report, path="") is False
    assert list(tmp_path.iterdir()) == []


def test_executor_escalation_creates_row():
    """An escalate-only incident (api_down) leaves a delivery record behind."""
    async def go(sm):
        async with sm() as db:
            admin = await _principal(db, ADMIN_EMAIL)
            org = await platform.ensure_org_scope(db)
            dept = await platform.create_scope(db, kind="department", parent_id=org.id, name="Ops")
            await platform.ensure_role(db, "healer", {"self_healing.remedy"})
            user_id = await db.scalar(select(User.id).where(User.email == USER_EMAIL))
            await platform.grant_membership(db, admin, user_id=user_id, scope_id=dept.id, role_key="healer")
            await db.commit()
        async with sm() as db:
            principal = await _principal(db, USER_EMAIL)
            outcome = await execute_remedy(db, principal, _incident(), scope_id=dept.id)
            assert outcome.action == "escalate"
            row = await db.scalar(
                select(SelfHealingEscalation).where(SelfHealingEscalation.incident_key == "api_down")
            )
            assert row is not None
            assert row.channel == "admin_ui"
            incident = await db.scalar(
                select(SelfHealingIncident).where(SelfHealingIncident.key == "api_down")
            )
            assert incident.status == "escalated"
    run(go)


# --------------------------------------------------------------------------- HTTP API

def test_api_requires_read_permission(client):
    # The admin holds no self_healing.read grant yet: 403, not a data leak.
    login(client, ADMIN_EMAIL)
    response = client.get("/api/self-healing/overview")
    assert response.status_code == 403, response.text


def test_api_unauthenticated(client):
    response = client.get("/api/self-healing/overview")
    assert response.status_code == 401, response.text


def test_api_overview_and_acknowledge(client):
    async def prep(sm):
        async with sm() as db:
            await _grant_read(db)
            await record_escalation(db, _incident())
            await db.commit()
    run(prep)
    login(client, ADMIN_EMAIL)
    overview = client.get("/api/self-healing/overview")
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["unacknowledged_escalations"] == 1
    assert body["incidents"]["total"] == 0  # no incident row was recorded, only the escalation
    escalations = client.get("/api/self-healing/escalations?acknowledged=false")
    assert escalations.status_code == 200, escalations.text
    assert escalations.json()["total"] == 1
    ack = client.post("/api/self-healing/escalations/api_down/acknowledge")
    assert ack.status_code == 200, ack.text
    assert ack.json()["acknowledged_at"] is not None
    assert client.get("/api/self-healing/escalations?acknowledged=false").json()["total"] == 0


def test_api_acknowledge_missing_404(client):
    async def prep(sm):
        async with sm() as db:
            await _grant_read(db)
    run(prep)
    login(client, ADMIN_EMAIL)
    response = client.post("/api/self-healing/escalations/nope/acknowledge")
    assert response.status_code == 404, response.text


def test_api_bad_status_filter(client):
    async def prep(sm):
        async with sm() as db:
            await _grant_read(db)
    run(prep)
    login(client, ADMIN_EMAIL)
    response = client.get("/api/self-healing/incidents?status=bogus")
    assert response.status_code in (400, 422), response.text
