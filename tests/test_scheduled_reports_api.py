"""Step AA slice 2: scheduled reports API — authz (403 for members without the
permission), CRUD for schedules, delivery scoping, per-user preferences."""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import platform
from app.api.routes.scheduled_reports import ensure_report_roles
from app.db.models import User
from app.db.report_schedule_models import ReportDelivery
from app.db.session import test_engine_options as _test_engine_options
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, USER2_EMAIL, USER_EMAIL, login

pytestmark = pytest.mark.usefixtures("clean_tables")


def run_async(fn):
    """Run an async function with a fresh sessionmaker (mirrors test_platform_core)."""

    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


async def _setup(sm, *, ensure_roles=True):
    async with sm() as db:
        if ensure_roles:
            await ensure_report_roles(db)
        admin_user = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
        admin = platform.principal_for(admin_user)
        org = await platform.ensure_org_scope(db)
        for email, role in ((ADMIN_EMAIL, "reports.admin"), (USER_EMAIL, "reports.viewer")):
            user_id = await db.scalar(select(User.id).where(User.email == email))
            await platform.grant_membership(db, admin, user_id=user_id, scope_id=org.id, role_key=role)
        await db.commit()
        ids = {email: str(await db.scalar(select(User.id).where(User.email == email)))
               for email in (ADMIN_EMAIL, USER_EMAIL, USER2_EMAIL)}
        return ids


def _setup_ids(*, ensure_roles=True):
    return run_async(lambda sm: _setup(sm, ensure_roles=ensure_roles))


def _payload(recipient_id, **overrides):
    body = {
        "template": "daily_cash_summary",
        "cadence": "daily",
        "time_of_day": "08:00:00",
        "timezone": "Asia/Karachi",
        "weekday": 0,
        "channels": ["in_app"],
        "recipients": [recipient_id],
        "quiet_start": "22:00:00",
        "quiet_end": "07:00:00",
        "overdue_days": 7,
        "enabled": True,
    }
    body.update(overrides)
    return body


def _session(client, email):
    client.cookies.clear()
    login(client, email)
    return dict(client.cookies)


def _as(client, cookies):
    client.cookies.clear()
    client.cookies.update(cookies)
    return client


def test_admin_creates_lists_updates_and_deletes_schedule(client):
    ids = _setup_ids()
    admin = _session(client, ADMIN_EMAIL)
    created = _as(client, admin).post("/api/reports/schedules", json=_payload(ids[USER_EMAIL]))
    assert created.status_code == 201, created.text
    data = created.json()
    assert data["template"] == "daily_cash_summary" and data["last_sent_at"] is None
    schedule_id = data["id"]

    listed = _as(client, admin).get("/api/reports/schedules")
    assert listed.status_code == 200 and len(listed.json()["schedules"]) == 1

    got = _as(client, admin).get(f"/api/reports/schedules/{schedule_id}")
    assert got.status_code == 200 and got.json()["id"] == schedule_id

    patched = _as(client, admin).patch(
        f"/api/reports/schedules/{schedule_id}", json={"enabled": False, "time_of_day": "09:30:00"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["enabled"] is False
    assert patched.json()["time_of_day"] == "09:30:00"

    deleted = _as(client, admin).delete(f"/api/reports/schedules/{schedule_id}")
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert _as(client, admin).get(f"/api/reports/schedules/{schedule_id}").status_code == 404


def test_create_rejects_unknown_recipient_and_empty_quiet_hours(client):
    ids = _setup_ids()
    admin = _session(client, ADMIN_EMAIL)
    unknown = _as(client, admin).post(
        "/api/reports/schedules", json=_payload(str(uuid.uuid4())))
    assert unknown.status_code == 422, unknown.text
    bad_quiet = _as(client, admin).post(
        "/api/reports/schedules",
        json=_payload(ids[USER_EMAIL], quiet_start="22:00:00", quiet_end="22:00:00"))
    assert bad_quiet.status_code == 422, bad_quiet.text
    bad_template = _as(client, admin).post(
        "/api/reports/schedules", json=_payload(ids[USER_EMAIL], template="nope"))
    assert bad_template.status_code == 422, bad_template.text


def test_viewer_cannot_manage_schedules_but_admin_can(client):
    ids = _setup_ids()
    admin = _session(client, ADMIN_EMAIL)
    viewer = _session(client, USER_EMAIL)
    assert _as(client, viewer).post(
        "/api/reports/schedules", json=_payload(ids[USER_EMAIL])).status_code == 403
    assert _as(client, viewer).get("/api/reports/schedules").status_code == 403
    assert _as(client, admin).post(
        "/api/reports/schedules", json=_payload(ids[USER_EMAIL])).status_code == 201


def test_member_without_reports_permission_gets_403_on_deliveries(client):
    _setup_ids()
    plain = _session(client, USER2_EMAIL)  # member of the org, no reports role
    assert _as(client, plain).get("/api/reports/deliveries").status_code == 403
    assert _as(client, plain).get("/api/reports/preferences").status_code == 403


def test_deliveries_are_scoped_to_the_recipient_for_viewers(client):
    ids = _setup_ids()
    admin = _session(client, ADMIN_EMAIL)
    viewer = _session(client, USER_EMAIL)
    schedule = _as(client, admin).post(
        "/api/reports/schedules", json=_payload(ids[USER_EMAIL])).json()

    async def _seed(sm):
        async with sm() as db:
            db.add(ReportDelivery(
                schedule_id=uuid.UUID(schedule["id"]),
                period_start=datetime(2026, 11, 28, 8, 0, tzinfo=timezone.utc),
                channel="in_app", recipient_user_id=uuid.UUID(ids[USER_EMAIL]), status="sent"))
            db.add(ReportDelivery(
                schedule_id=uuid.UUID(schedule["id"]),
                period_start=datetime(2026, 11, 28, 8, 0, tzinfo=timezone.utc),
                channel="in_app", recipient_user_id=uuid.UUID(ids[ADMIN_EMAIL]), status="sent"))
            await db.commit()

    run_async(_seed)
    mine = _as(client, viewer).get("/api/reports/deliveries")
    assert mine.status_code == 200, mine.text
    assert [d["recipient_user_id"] for d in mine.json()["deliveries"]] == [ids[USER_EMAIL]]
    all_rows = _as(client, admin).get("/api/reports/deliveries")
    assert all_rows.status_code == 200
    assert len(all_rows.json()["deliveries"]) == 2
    filtered = _as(client, admin).get(
        "/api/reports/deliveries", params={"schedule_id": schedule["id"], "status": "sent"})
    assert len(filtered.json()["deliveries"]) == 2


def test_preferences_are_per_user(client):
    _setup_ids()
    viewer = _session(client, USER_EMAIL)
    admin = _session(client, ADMIN_EMAIL)
    saved = _as(client, viewer).put(
        "/api/reports/preferences/daily_cash_summary",
        json={"template": "daily_cash_summary", "channels": ["whatsapp", "in_app"],
              "cadence": "daily", "enabled": True})
    assert saved.status_code == 200, saved.text
    assert saved.json()["channels"] == ["whatsapp", "in_app"]
    mine = _as(client, viewer).get("/api/reports/preferences")
    assert [p["template"] for p in mine.json()["preferences"]] == ["daily_cash_summary"]
    # An administrator sees only their own preferences, never another user's.
    assert _as(client, admin).get("/api/reports/preferences").json()["preferences"] == []
    mismatch = _as(client, viewer).put(
        "/api/reports/preferences/weekly_invoices",
        json={"template": "daily_cash_summary", "channels": ["email"]})
    assert mismatch.status_code == 422, mismatch.text


def test_report_roles_exist_from_startup_without_explicit_ensure(client):
    """Regression: the roles must exist without anyone calling ensure_report_roles.

    Before the lifespan seeding fix, ``reports.admin``/``reports.viewer`` were
    only created by the tests themselves — in production no code path created
    them (there is no role-creation endpoint and legacy admins are not
    bridged for ``reports.manage``), so every scheduled-reports request
    returned 403, even for the installation administrator. The TestClient
    fixture runs the app lifespan, which now seeds the roles; this test
    grants the memberships without calling ensure_report_roles and expects
    the admin to be able to create a schedule.
    """
    ids = _setup_ids(ensure_roles=False)
    admin = _session(client, ADMIN_EMAIL)
    created = _as(client, admin).post("/api/reports/schedules", json=_payload(ids[USER_EMAIL]))
    assert created.status_code == 201, created.text
