"""Tests for Step AO slice 6: admin database summary API + migrate-safe CLI gate.

The summary handler is called directly with a real engine (the same
pattern as the health readiness tests). The admin gate itself is
verified by introspection: the router's dependencies must include
``require_admin`` — a missing gate would be a security hole, so the
test fails loudly if it is ever dropped.

Nothing here touches the live Codespace database or the owner's PC
database.
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.deps import require_admin
from app.api.routes import admin_database
from app.db.ao_backup_models import STATUS_VERIFIED, BackupReceipt
from app.db.session import create_engine

TEST_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL is not set")


def test_admin_gate_is_wired_on_every_route():
    for route in admin_database.router.routes:
        dependencies = [dep.dependency for dep in route.dependencies]
        assert require_admin in dependencies, f"{route.path} is missing require_admin"


def test_summary_reports_health_backups_integrity_and_gate():
    async def _body():
        engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)
        try:
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))
            return await admin_database.database_summary(request)
        finally:
            await engine.dispose()

    summary = asyncio.run(_body())
    assert summary["database"]
    assert summary["health"]["reachable"] is True
    assert summary["health"]["overall"] in ("ok", "warning", "critical", "unknown")
    assert "newest_backup" in summary
    assert "latest_drill" in summary
    integrity = summary["integrity"]
    assert integrity["passed"] is True
    assert integrity["foreign_keys"] > 50
    assert integrity["total_orphans"] == 0
    gate = summary["migration_gate"]
    assert isinstance(gate["allowed"], bool)
    assert isinstance(gate["reasons"], list)
    assert "human-approved" in summary["restore_note"]
    # No connection string or secret ever appears in the payload.
    assert "postgresql://" not in json.dumps(summary)


def test_migrate_safe_cli_gate_allows_with_a_fresh_drill_verified_receipt(monkeypatch):
    from sqlalchemy.engine import make_url

    from app.cli import db_migrate_safe

    # The CLI reads DATABASE_URL from settings; point it at the throwaway
    # test database for this test.
    class _Settings:
        def __init__(self, url: str):
            self.database_url = SimpleNamespace(get_secret_value=lambda: url)

    monkeypatch.setattr("app.cli.DatabaseSettings", lambda: _Settings(TEST_URL))
    database_name = make_url(TEST_URL).database
    receipt_path = f"/tmp/ao-gate-test/{uuid.uuid4()}.dump"

    async def _seed_receipt():
        engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)
        try:
            from sqlalchemy.ext.asyncio import AsyncSession

            async with AsyncSession(engine) as session:
                session.add(
                    BackupReceipt(
                        database=database_name,
                        path=receipt_path,
                        bytes=1024,
                        sha256="a" * 64,
                        catalog_entries=10,
                        tables=2,
                        rows_total=5,
                        status=STATUS_VERIFIED,
                        drill_verified=True,
                        created_at=datetime.now(UTC),
                    )
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_seed_receipt())
    # check_only: the gate allows, nothing migrates.
    asyncio.run(db_migrate_safe(True, None, 24.0))
    # A last-migration time in the future refuses the gate (exit 2).
    with pytest.raises(SystemExit) as exc:
        asyncio.run(db_migrate_safe(True, datetime.now(UTC) + timedelta(hours=1), 24.0))
    assert exc.value.code == 2
