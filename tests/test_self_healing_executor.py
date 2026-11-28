"""Step W slice 3 — remedy executor: allow-list, budget, cooldown, verify, audit (DB tests)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import platform
from app.core.errors import PermissionDeniedError
from app.db.models import (
    DOC_PENDING,
    DOC_PROCESSING,
    AccessGroup,
    AuditLog,
    Document,
    IngestionWorkerHeartbeat,
    User,
)
from app.db.session import test_engine_options as _test_engine_options
from app.db.workflow_engine_models import Workflow, WorkflowRun, WorkflowStep, WorkflowVersion
from app.platform.models import PlatformActionExecution
from app.self_healing.detector import Incident
from app.self_healing.executor import execute_remedy
from app.self_healing.models import SelfHealingIncident
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, USER2_EMAIL, USER_EMAIL

pytestmark = pytest.mark.usefixtures("clean_tables")


def run(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


async def _principal(db, email):
    user = await db.scalar(select(User).where(User.email == email))
    return platform.Principal(user.id, user.email, "user", "web", user.auth_version)


async def setup(sm):
    """Org > Ops department; USER_EMAIL holds the self_healing.remedy role."""
    async with sm() as db:
        admin = await _principal(db, ADMIN_EMAIL)
        org = await platform.ensure_org_scope(db)
        dept = await platform.create_scope(db, kind="department", parent_id=org.id, name="Ops")
        await platform.ensure_role(db, "healer", {"self_healing.remedy"})
        await platform.ensure_role(db, "viewer", set())
        user_id = await db.scalar(select(User.id).where(User.email == USER_EMAIL))
        await platform.grant_membership(db, admin, user_id=user_id, scope_id=dept.id, role_key="healer")
        user2_id = await db.scalar(select(User.id).where(User.email == USER2_EMAIL))
        await platform.grant_membership(db, admin, user_id=user2_id, scope_id=dept.id, role_key="viewer")
        await db.commit()
        return {"dept": dept.id}


async def _stale_step(db, owner_id, *, lease_age_seconds=900):
    """A running workflow step whose lease expired (the lock to clear)."""
    group = AccessGroup(name="Ops", kind="department", source="local")
    db.add(group)
    await db.flush()
    workflow = Workflow(scope_id=group.id, owner_id=owner_id, name="W")
    db.add(workflow)
    await db.flush()
    version = WorkflowVersion(
        scope_id=group.id, workflow_id=workflow.id, number=1,
        spec={}, digest="x" * 64, author_id=owner_id,
    )
    db.add(version)
    await db.flush()
    run_row = WorkflowRun(
        scope_id=group.id, workflow_id=workflow.id, version_id=version.id,
        requester_id=owner_id, idempotency_key="k1", input_digest="y" * 64,
        payload={}, status="running",
    )
    db.add(run_row)
    await db.flush()
    step = WorkflowStep(
        scope_id=group.id, run_id=run_row.id, node_id="n1", status="running",
        lease_until=datetime.now(UTC) - timedelta(seconds=lease_age_seconds),
        claim_token=uuid.uuid4(),
    )
    db.add(step)
    await db.flush()
    return step.id


async def _stuck_document(db, owner_id, *, lock_age_seconds=900):
    db.add(
        Document(
            owner_id=owner_id, visibility="private", filename="b.pdf",
            storage_key="k" * 32, sha256="t" * 64, size_bytes=10,
            status=DOC_PROCESSING,
            locked_until=datetime.now(UTC) - timedelta(seconds=lock_age_seconds),
        )
    )
    await db.flush()
    return await db.scalar(select(Document.id).where(Document.sha256 == "t" * 64))


def _stale_lock_incident():
    return Incident(
        key="stale_lock", kind="stale_lock", severity="medium",
        component="workflow_worker", summary="A workflow lock has been held too long.",
    )


def _worker_stalled_incident():
    return Incident(
        key="worker_stalled", kind="worker_stalled", severity="high",
        component="workflow_worker", summary="Workflow worker heartbeat is stale.",
    )


def test_clear_stale_lock_releases_lease_and_verifies():
    async def go(sm):
        s = await setup(sm)
        async with sm() as db:
            healer = await _principal(db, USER_EMAIL)
            owner_id = healer.user_id
            step_id = await _stale_step(db, owner_id)
            await db.commit()
        async with sm() as db:
            healer = await _principal(db, USER_EMAIL)
            outcome = await execute_remedy(db, healer, _stale_lock_incident(), scope_id=s["dept"])
            assert outcome.action == "remedy" and outcome.remedy == "clear_stale_lock"
            assert outcome.status == "verified" and not outcome.replayed
            assert outcome.execution_id is not None
        async with sm() as db:
            step = await db.get(WorkflowStep, step_id)
            assert step.lease_until is None and step.claim_token is None
            row = await db.scalar(select(SelfHealingIncident).where(SelfHealingIncident.key == "stale_lock"))
            assert row.attempts == 1 and row.status == "remedied"
            assert row.last_attempt_at is not None
            execution = await db.get(PlatformActionExecution, outcome.execution_id)
            assert execution.status == "verified"
            assert execution.action_type == "self_healing.clear_stale_lock"
            decisions = await db.scalars(
                select(AuditLog).where(AuditLog.action == "self_healing.remedy_decision")
            )
            assert len(list(decisions)) == 1

    run(go)


def test_requeue_job_returns_stuck_document_to_queue():
    async def go(sm):
        s = await setup(sm)
        async with sm() as db:
            healer = await _principal(db, USER_EMAIL)
            doc_id = await _stuck_document(db, healer.user_id)
            await db.commit()
        async with sm() as db:
            healer = await _principal(db, USER_EMAIL)
            incident = Incident(
                key="ingestion_backlog", kind="ingestion_backlog", severity="medium",
                component="ingestion_worker", summary="Jobs are backing up.",
            )
            # ingestion_backlog's remedy is requeue_job; the stale document is the job.
            outcome = await execute_remedy(db, healer, incident, scope_id=s["dept"])
            assert outcome.action == "remedy" and outcome.remedy == "requeue_job"
            assert outcome.status == "verified"
        async with sm() as db:
            doc = await db.get(Document, doc_id)
            assert doc.status == DOC_PENDING and doc.locked_until is None

    run(go)


def test_retry_once_runs_operation_once_then_cooldown_escalates():
    async def go(sm):
        s = await setup(sm)
        calls: list[str] = []

        async def operation():
            calls.append("ran")
            return "op-ref-1"

        async def check(ref):
            return ref == "op-ref-1"

        incident = Incident(
            key="transient_failure", kind="transient_failure", severity="low",
            component="api", summary="A single retryable error.",
        )
        async with sm() as db:
            healer = await _principal(db, USER_EMAIL)
            first = await execute_remedy(
                db, healer, incident, scope_id=s["dept"], operation=operation, check=check
            )
            assert first.action == "remedy" and first.status == "verified"
            assert calls == ["ran"]
        async with sm() as db:
            healer = await _principal(db, USER_EMAIL)
            second = await execute_remedy(
                db, healer, incident, scope_id=s["dept"], operation=operation, check=check
            )
            assert second.action == "escalate"  # 5-minute cooldown after the first attempt
            assert "cooldown" in second.reason
            assert calls == ["ran"]  # never dispatched twice

    run(go)


def test_attempt_budget_exhausted_escalates_without_dispatch():
    async def go(sm):
        s = await setup(sm)
        calls: list[str] = []

        async def operation():
            calls.append("ran")
            return "op-ref-1"

        async def check(ref):
            return True

        incident = Incident(
            key="transient_failure", kind="transient_failure", severity="low",
            component="api", summary="A single retryable error.",
        )
        async with sm() as db:
            row = SelfHealingIncident(
                key="transient_failure", kind="transient_failure", severity="low",
                component="api", summary="x", status="open", attempts=3,
                last_attempt_at=datetime.now(UTC) - timedelta(hours=1),
            )
            db.add(row)
            await db.commit()
        async with sm() as db:
            healer = await _principal(db, USER_EMAIL)
            outcome = await execute_remedy(
                db, healer, incident, scope_id=s["dept"], operation=operation, check=check
            )
            assert outcome.action == "escalate"
            assert "budget exhausted" in outcome.reason
            assert calls == []
            assert await db.scalar(
                select(func.count()).select_from(PlatformActionExecution)
            ) == 0

    run(go)


def test_critical_incident_is_never_remedied():
    async def go(sm):
        s = await setup(sm)
        incident = Incident(
            key="api_down", kind="api_down", severity="critical",
            component="api", summary="API is down.",
        )
        async with sm() as db:
            healer = await _principal(db, USER_EMAIL)
            outcome = await execute_remedy(db, healer, incident, scope_id=s["dept"])
            assert outcome.action == "escalate"
            assert outcome.status == "escalated"
            assert await db.scalar(
                select(func.count()).select_from(PlatformActionExecution)
            ) == 0
            row = await db.scalar(select(SelfHealingIncident).where(SelfHealingIncident.key == "api_down"))
            assert row.status == "escalated"

    run(go)


def test_restart_worker_has_no_verified_handler_so_it_escalates():
    async def go(sm):
        s = await setup(sm)
        async with sm() as db:
            healer = await _principal(db, USER_EMAIL)
            outcome = await execute_remedy(db, healer, _worker_stalled_incident(), scope_id=s["dept"])
            assert outcome.action == "escalate"
            assert "no verified handler" in outcome.reason
            assert "restart_worker" in outcome.reason
            assert await db.scalar(
                select(func.count()).select_from(PlatformActionExecution)
            ) == 0

    run(go)


def test_principal_without_remedy_permission_is_denied():
    async def go(sm):
        s = await setup(sm)
        async with sm() as db:
            # USER2 is a member of the scope with an empty role: member, but no
            # self_healing.remedy permission. (The admin principal is NOT used
            # here: the legacy bridge never grants this duty, and whether an
            # admin counts as a member is a separate platform question.)
            viewer = await _principal(db, USER2_EMAIL)
            with pytest.raises(PermissionDeniedError):
                await execute_remedy(db, viewer, _stale_lock_incident(), scope_id=s["dept"])

    run(go)


def test_failed_verification_leaves_incident_open_for_retry():
    async def go(sm):
        s = await setup(sm)

        async def operation():
            return "op-ref-1"

        async def check(ref):
            return False  # the operation did not actually fix anything

        incident = Incident(
            key="transient_failure", kind="transient_failure", severity="low",
            component="api", summary="A single retryable error.",
        )
        async with sm() as db:
            healer = await _principal(db, USER_EMAIL)
            outcome = await execute_remedy(
                db, healer, incident, scope_id=s["dept"], operation=operation, check=check
            )
            assert outcome.action == "remedy" and outcome.status == "failed"
            row = await db.scalar(
                select(SelfHealingIncident).where(SelfHealingIncident.key == "transient_failure")
            )
            assert row.attempts == 1 and row.status == "open"

    run(go)


def test_dispatch_failure_counts_as_attempt_and_stays_open():
    async def go(sm):
        s = await setup(sm)
        async with sm() as db:
            healer = await _principal(db, USER_EMAIL)
            # No stale lock exists anywhere, so the dispatch raises.
            outcome = await execute_remedy(db, healer, _stale_lock_incident(), scope_id=s["dept"])
            assert outcome.action == "remedy" and outcome.status == "failed"
            row = await db.scalar(select(SelfHealingIncident).where(SelfHealingIncident.key == "stale_lock"))
            assert row.attempts == 1 and row.status == "open"

    run(go)
