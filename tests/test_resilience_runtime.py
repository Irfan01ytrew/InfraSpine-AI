"""Slice 4: recovery plans as unified-engine workflow runs (DB-backed).

A plan starts as a system workflow run (origin 'resilience'), its
approval-bound steps wait for an exact-action approval decided by someone
other than the requester, the person steps execute once with read-back
verification, and pause blocks execution until resumed. No money moves,
no data is restored and no supplier is switched by the runtime itself:
performing a step is a human attestation recorded in the audit trail.
"""

from sqlalchemy import select, text

from app.db.models import User
from app.main import app as fastapi_app
from app.resilience.plans import build_plan
from app.resilience.runtime import control_plan, plan_progress, start_plan
from app.services.audit import AuditContext
from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login, run_db
from tests.test_workflow_engine import pump, setup_scope


def _analysis():
    return {
        "affected": [
            {"id": "bc:sys-1", "name": "Core server", "kind": "system", "score": 90,
             "factors": ["no_backup_recorded"], "risk": "critical"},
            {"id": "bc:p-1", "name": "Duty manager", "kind": "person", "score": 50,
             "factors": ["sole_approver"], "risk": "medium"},
        ],
        "uncovered": ["bc:sys-1"],
        "ranking": [],
        "recovery_options": [
            {"rank": 1, "kind": "use_backup", "action": "Restore from the latest verified backup",
             "covers": ["bc:sys-1"], "owner": "p-1", "eta_days": 1, "requires_approval": True},
            {"rank": 2, "kind": "escalate", "action": "Escalate to the on-call team",
             "covers": ["bc:p-1"], "owner": "p-1", "eta_days": 1, "requires_approval": False},
            {"rank": 3, "kind": "switch_supplier", "action": "Switch to the backup supplier",
             "covers": ["bc:sys-1"], "owner": "p-1", "eta_days": 2, "requires_approval": True},
        ],
    }


def _plan(plan_id="plan-1"):
    # s1: restore from backup (approval-bound, workflow.manage).
    # s2: escalate to the team (no approval needed).
    return build_plan(_analysis(), [1, 2], plan_id=plan_id)


def _start(client, scope, plan, email=USER_EMAIL):
    async def _go(sessionmaker, cipher):
        async with sessionmaker() as session:
            user = await session.scalar(select(User).where(User.email == email))
            ctx = AuditContext(user.id, user.email, None, None)
            run, approvals = await start_plan(
                session, user, cipher, ctx, scope_id=scope, plan=plan
            )
            await session.commit()
            return str(run.id), approvals

    return client.portal.call(_go, fastapi_app.state.sessionmaker, fastapi_app.state.cipher)


def _steps(client, run_id):
    async def _go(conn):
        rows = (
            await conn.execute(
                text(
                    "SELECT node_id, status FROM wf_steps WHERE run_id = :run "
                    "ORDER BY node_id"
                ),
                {"run": run_id},
            )
        ).all()
        return [(row.node_id, row.status) for row in rows]

    return run_db(_go)


def _run_status(client, run_id):
    async def _go(conn):
        return (await conn.execute(
            text("SELECT status FROM wf_runs WHERE id = :run"), {"run": run_id}
        )).scalar_one()

    return run_db(_go)


def _approve(client, approval_id):
    """Decide an approval as the admin (holds workflow.manage + platform.manage).

    The reviewer path is the approvals inbox: list pending approvals the
    caller may decide, take the action hash from the listing, and present
    it back on decide (a changed action is refused)."""
    login(client, ADMIN_EMAIL)
    inbox = client.get("/api/approvals")
    assert inbox.status_code == 200, inbox.text
    rows = [row for row in inbox.json()["approvals"] if row["id"] == approval_id]
    assert rows, f"Approval {approval_id} is not visible to the admin inbox."
    decision = client.post(
        f"/api/approvals/{approval_id}/decide",
        json={"presented_hash": rows[0]["action_hash"], "decision": "approve", "note": "Drill rehearsal."},
    )
    assert decision.status_code == 200, decision.text
    return decision.json()


def _perform(client, scope, run_id, step_id):
    """Perform a person step as the admin (holds workflow.manage)."""
    login(client, ADMIN_EMAIL)
    response = client.post(
        f"/api/workflow-engine/scopes/{scope}/runs/{run_id}/steps/{step_id}/perform"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _step_ids(client, run_id):
    async def _go(conn):
        rows = (
            await conn.execute(
                text("SELECT id, node_id FROM wf_steps WHERE run_id = :run"),
                {"run": run_id},
            )
        ).all()
        return {row.node_id: str(row.id) for row in rows}

    return run_db(_go)


def _fetchone(sql, params=None):
    """One row from the test DB. (run_db takes an async fn: the execute must be awaited.)"""

    async def _go(conn):
        return (await conn.execute(text(sql), params or {})).first()

    return run_db(_go)


def _scalar(sql, params=None):
    """One scalar from the test DB."""

    async def _go(conn):
        return (await conn.execute(text(sql), params or {})).scalar_one()

    return run_db(_go)


def _fetchall(sql, params=None):
    """All rows from the test DB."""

    async def _go(conn):
        return (await conn.execute(text(sql), params or {})).all()

    return run_db(_go)


def test_start_plan_creates_run_approvals_and_audit(client):
    scope, _people = setup_scope(client)
    plan = _plan()
    run_id, approvals = _start(client, scope, plan)

    assert len(approvals) == 1 and "s1" in approvals  # only the backup step needs approval
    run = _fetchone(
        "SELECT origin, origin_ref, status FROM wf_runs WHERE id = :run",
        {"run": run_id},
    )
    assert run.origin == "resilience" and run.origin_ref == "plan-1"

    approval = _fetchone(
        "SELECT status, subject FROM platform_approval_requests WHERE id = :id",
        {"id": approvals["s1"]},
    )
    assert approval.status == "pending"
    assert approval.subject == "resilience-plan:plan-1:s1"

    started = _scalar(
        "SELECT COUNT(*) FROM audit_logs WHERE action = 'resilience.plan_started'"
    )
    assert started == 1

    # Starting the same plan again returns the same run, no duplicates.
    run_id_2, approvals_2 = _start(client, scope, plan)
    assert run_id_2 == run_id and approvals_2 == approvals


def test_plan_executes_end_to_end(client):
    scope, _people = setup_scope(client)
    plan = _plan()
    run_id, approvals = _start(client, scope, plan)
    pump(client)

    # The approval step waits; the backup execution step has not run yet.
    steps = dict(_steps(client, run_id))
    assert steps.get("approve-s1") == "waiting"
    assert steps.get("exec-s1") == "blocked"

    _approve(client, approvals["s1"])
    pump(client)

    # After approval, the backup step is ready for a person to perform.
    steps = dict(_steps(client, run_id))
    assert steps.get("approve-s1") == "completed"
    assert steps.get("exec-s1") == "action"

    ids = _step_ids(client, run_id)
    _perform(client, scope, run_id, ids["exec-s1"])
    pump(client)

    # The escalate step is a person step too; perform it, then the run ends.
    steps = dict(_steps(client, run_id))
    assert steps.get("exec-s1") == "completed"
    _perform(client, scope, run_id, ids["exec-s2"])
    pump(client)

    assert _run_status(client, run_id) == "completed"
    # Progress is 100% once every execute step is complete.
    async def _progress(sessionmaker, cipher):
        async with sessionmaker() as session:
            return await plan_progress(session, scope, "plan-1")

    progress = client.portal.call(
        _progress, fastapi_app.state.sessionmaker, fastapi_app.state.cipher
    )
    assert progress["total_steps"] == 2
    assert progress["counts"]["done"] == 2
    assert progress["complete"] is True
    assert all(not step["waiting_for_approval"] for step in progress["steps"])

    # execute_once wrote exactly one verified ledger row per step. The
    # ledger's duty column holds the permission (workflow.manage); the
    # resilience rows are identified by their idempotency key.
    executions = _scalar(
        "SELECT COUNT(*) FROM platform_action_executions "
        "WHERE idempotency_key LIKE 'resilience-plan:%' AND status = 'verified'"
    )
    assert executions == 2

    performed = _scalar(
        "SELECT COUNT(*) FROM audit_logs WHERE action = 'resilience.plan_step_performed'"
    )
    assert performed == 2


def test_pause_blocks_execution_until_resumed(client):
    scope, _people = setup_scope(client)
    plan = _plan(plan_id="plan-pause")
    run_id, approvals = _start(client, scope, plan)

    async def _pause(sessionmaker, cipher):
        async with sessionmaker() as session:
            user = await session.scalar(select(User).where(User.email == ADMIN_EMAIL))
            ctx = AuditContext(user.id, user.email, None, None)
            await control_plan(session, user, cipher, ctx, scope, "plan-pause", "pause")
            await session.commit()

    client.portal.call(
        _pause, fastapi_app.state.sessionmaker, fastapi_app.state.cipher
    )
    assert _run_status(client, run_id) == "paused"

    # While paused, the approval step cannot run forward.
    _approve(client, approvals["s1"])
    pump(client)
    assert _run_status(client, run_id) == "paused"
    assert dict(_steps(client, run_id)).get("approve-s1") != "completed"

    async def _resume(sessionmaker, cipher):
        async with sessionmaker() as session:
            user = await session.scalar(select(User).where(User.email == ADMIN_EMAIL))
            ctx = AuditContext(user.id, user.email, None, None)
            await control_plan(session, user, cipher, ctx, scope, "plan-pause", "resume")
            await session.commit()

    client.portal.call(
        _resume, fastapi_app.state.sessionmaker, fastapi_app.state.cipher
    )
    pump(client)
    assert dict(_steps(client, run_id)).get("approve-s1") == "completed"


def test_sensitive_step_needs_owner_approval(client):
    scope, _people = setup_scope(client)
    # A supplier switch is sensitive: only the owner (platform.manage) may approve.
    plan = build_plan(_analysis(), [3, 2], plan_id="plan-swap")
    run_id, approvals = _start(client, scope, plan)
    assert "s1" in approvals

    # A plain member cannot see it in the inbox (no platform.manage) and
    # cannot decide it even when presenting the exact hash.
    login(client, USER_EMAIL)
    inbox = client.get("/api/approvals")
    assert inbox.status_code == 200, inbox.text
    assert approvals["s1"] not in {row["id"] for row in inbox.json()["approvals"]}
    action_hash = _fetchone(
        "SELECT action_hash FROM platform_approval_requests WHERE id = :id",
        {"id": approvals["s1"]},
    ).action_hash
    decision = client.post(
        f"/api/approvals/{approvals['s1']}/decide",
        json={"presented_hash": action_hash, "decision": "approve"},
    )
    assert decision.status_code == 403, decision.text

    # The admin (owner) can.
    _approve(client, approvals["s1"])


def test_repeated_pump_never_executes_a_step_twice(client):
    scope, _people = setup_scope(client)
    plan = _plan(plan_id="plan-once")
    run_id, approvals = _start(client, scope, plan)
    _approve(client, approvals["s1"])
    pump(client)
    ids = _step_ids(client, run_id)
    _perform(client, scope, run_id, ids["exec-s1"])
    _perform(client, scope, run_id, ids["exec-s2"])
    pump(client, iterations=10)
    pump(client, iterations=10)

    executions = _fetchall(
        "SELECT idempotency_key, COUNT(*) FROM platform_action_executions "
        "WHERE idempotency_key LIKE 'resilience-plan:plan-once:%' "
        "GROUP BY idempotency_key ORDER BY idempotency_key"
    )
    assert len(executions) == 2
    assert all(count == 1 for _key, count in executions)
