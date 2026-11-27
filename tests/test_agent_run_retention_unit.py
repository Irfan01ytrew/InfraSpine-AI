"""Offline tests for explicit, bounded redaction of expired agent-run content."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.schemas.agent_run_retention import AgentRunRetentionPurgeRequest
from app.services import agent_run_retention
from app.services.audit import AuditContext


def test_retention_cutoff_is_utc_and_rejects_unsafe_windows():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    assert agent_run_retention.retention_cutoff(90, now=now) == now - timedelta(days=90)
    assert agent_run_retention.retention_cutoff(90, now=datetime(2026, 10, 29, 12, 0)) == now - timedelta(days=90)
    for days in (0, 29, 3651):
        with pytest.raises(ValueError):
            agent_run_retention.retention_cutoff(days, now=now)


def test_eligible_content_query_is_bounded_and_excludes_live_or_purged_runs():
    cutoff = datetime(2026, 7, 31, tzinfo=UTC)
    statement = agent_run_retention.eligible_run_content_ids(cutoff, limit=25)
    sql = str(statement.compile(dialect=postgresql.dialect())).lower()
    assert "requested_at <" in sql
    assert "status in" in sql
    assert "content_purged_at is null" in sql
    assert "task_ciphertext is not null" in sql
    assert "for update skip locked" in sql
    assert "limit" in sql
    with pytest.raises(ValueError):
        agent_run_retention.eligible_run_content_ids(cutoff, limit=1001)


def test_purge_redacts_only_selected_batch_and_audits_aggregate_metadata(monkeypatch):
    async def scenario():
        run_ids = [uuid.uuid4(), uuid.uuid4()]
        selected = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: run_ids))
        db = SimpleNamespace(execute=AsyncMock(side_effect=[selected, SimpleNamespace()]), commit=AsyncMock())
        record = Mock()
        monkeypatch.setattr(agent_run_retention.audit, "record", record)
        context = AuditContext(uuid.uuid4(), "admin@example.test", "127.0.0.1", "request-test")
        now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

        count, cutoff = await agent_run_retention.purge_expired_run_content(
            db, context, retention_days=90, limit=10, now=now
        )

        assert count == 2
        assert cutoff == now - timedelta(days=90)
        assert db.execute.await_count == 2
        update = db.execute.await_args_list[1].args[0]
        compiled = update.compile(dialect=postgresql.dialect())
        assert "task_ciphertext" in str(compiled)
        assert "result_ciphertext" in str(compiled)
        assert compiled.params["task_ciphertext"] is None
        assert compiled.params["task_nonce"] is None
        assert compiled.params["task_key_id"] is None
        assert compiled.params["result_ciphertext"] is None
        assert compiled.params["content_purged_at"] == now
        record.assert_called_once()
        audit_args, audit_kwargs = record.call_args
        assert audit_args[0] is db and audit_args[1] is context
        assert audit_args[2] == "agent.profile_run_content_purged"
        assert audit_kwargs["details"] == {
            "retention_days": 90,
            "purged_count": 2,
            "batch_limit": 10,
            "cutoff_at": cutoff.isoformat(),
        }
        db.commit.assert_awaited_once()

    asyncio.run(scenario())


def test_purge_request_requires_explicit_confirmation_and_bounded_batch():
    accepted = AgentRunRetentionPurgeRequest(retention_days=90, batch_size=100, confirm=True)
    assert accepted.confirm is True
    with pytest.raises(ValidationError):
        AgentRunRetentionPurgeRequest(retention_days=90, batch_size=100, confirm=False)
    with pytest.raises(ValidationError):
        AgentRunRetentionPurgeRequest(retention_days=90, batch_size=1001, confirm=True)
