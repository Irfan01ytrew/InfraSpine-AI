"""Focused tests for controlled agent actions, human approvals, and idempotent execution."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.db.models import (
    ROLE_ADMIN,
    ROLE_EMPLOYEE,
    ROLE_MANAGER,
    ROLE_USER,
    User,
    VISIBILITY_PRIVATE,
    VISIBILITY_SHARED,
    Document,
)
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, _PASSWORD_HASH, login, run_db


def _user_id(email: str) -> str:
    return str(run_db(lambda conn: conn.scalar(text("SELECT id FROM users WHERE email=:email"), {"email": email})))


def _create_user(email: str, role: str) -> str:
    return str(run_db(lambda conn: conn.scalar(
        User.__table__.insert().values(email=email, role=role, password_hash=_PASSWORD_HASH).returning(User.id)
    )))


def _department(client, name: str) -> str:
    response = client.post("/api/admin/access/groups", json={"name": name, "kind": "department"})
    assert response.status_code == 201, response.text
    return response.json()["id"]



def _fetch_one(sql: str, params: dict):
    """Read one row; `run_db` passes an async connection, so the query must be awaited."""
    async def query(conn):
        return (await conn.execute(text(sql), params)).one()

    return run_db(query)

def test_propose_action_validation_and_idempotency(client, clean_tables):
    login(client, ADMIN_EMAIL)
    
    # 1. Invalid action type
    r_bad_type = client.post(
        "/api/agents/actions/proposals",
        json={
            "action_type": "arbitrary_cmd",
            "parameters": {"cmd": "whoami"},
            "rationale": "Run shell command",
            "idempotency_key": f"key-{uuid.uuid4()}",
        },
    )
    assert r_bad_type.status_code in (400, 422), r_bad_type.text

    # 2. Webhook dispatch missing url
    r_bad_wh = client.post(
        "/api/agents/actions/proposals",
        json={
            "action_type": "webhook_dispatch",
            "parameters": {"payload": {"test": 1}},
            "rationale": "Dispatch webhook",
            "idempotency_key": f"key-{uuid.uuid4()}",
        },
    )
    assert r_bad_wh.status_code in (400, 422), r_bad_wh.text

    # 3. Document publish missing document_id
    r_bad_doc = client.post(
        "/api/agents/actions/proposals",
        json={
            "action_type": "document_publish",
            "parameters": {},
            "rationale": "Publish document",
            "idempotency_key": f"key-{uuid.uuid4()}",
        },
    )
    assert r_bad_doc.status_code in (400, 422), r_bad_doc.text

    # 4. Valid proposal creation
    key = f"idempotent-key-{uuid.uuid4()}"
    r_valid = client.post(
        "/api/agents/actions/proposals",
        json={
            "action_type": "webhook_dispatch",
            "parameters": {"url": "https://api.example.com/events", "payload": {"event": "test"}},
            "rationale": "Valid proposal for webhook",
            "idempotency_key": key,
            "ttl_hours": 12,
        },
    )
    assert r_valid.status_code == 201, r_valid.text
    p1 = r_valid.json()
    assert p1["status"] == "pending_approval"
    assert p1["action_type"] == "webhook_dispatch"
    assert p1["idempotency_key"] == key

    # 5. Idempotent re-submission returns identical proposal
    r_dup = client.post(
        "/api/agents/actions/proposals",
        json={
            "action_type": "webhook_dispatch",
            "parameters": {"url": "https://api.example.com/events", "payload": {"event": "test"}},
            "rationale": "Valid proposal for webhook duplicate",
            "idempotency_key": key,
            "ttl_hours": 12,
        },
    )
    assert r_dup.status_code == 201
    assert r_dup.json()["id"] == p1["id"]


def test_non_self_approval_enforced(client, clean_tables):
    login(client, ADMIN_EMAIL)
    
    # Create second admin directly with proper password hash
    second_email = f"second_admin_{uuid.uuid4().hex[:6]}@example.com"
    second_id = _create_user(second_email, ROLE_ADMIN)

    # Admin 1 proposes an action
    key = f"key-nonself-{uuid.uuid4()}"
    r_prop = client.post(
        "/api/agents/actions/proposals",
        json={
            "action_type": "webhook_dispatch",
            "parameters": {"url": "https://api.example.com/test", "payload": {"a": 1}},
            "rationale": "Propose action to test self approval",
            "idempotency_key": key,
        },
    )
    assert r_prop.status_code == 201, r_prop.text
    prop_id = r_prop.json()["id"]

    # Admin 1 (requester) tries to self-approve -> MUST fail with 403
    r_self_app = client.post(
        f"/api/agents/actions/proposals/{prop_id}/approve",
        json={"rationale": "I self-approve this proposal"},
    )
    assert r_self_app.status_code == 403, r_self_app.text
    assert "Requesters cannot approve their own action proposals" in r_self_app.text

    # Second admin logs in and approves -> MUST succeed
    login(client, second_email)
    r_app = client.post(
        f"/api/agents/actions/proposals/{prop_id}/approve",
        json={"rationale": "Independent authorization granted"},
    )
    assert r_app.status_code == 200, r_app.text
    approved = r_app.json()
    assert approved["status"] == "approved"
    assert approved["approver_user_id"] == second_id


def test_manager_department_isolation_and_rejection(client, clean_tables):
    login(client, ADMIN_EMAIL)
    dept_a = _department(client, f"Department Alpha {uuid.uuid4().hex[:4]}")
    dept_b = _department(client, f"Department Beta {uuid.uuid4().hex[:4]}")

    # Create managers
    mgr_a_email = f"mgr_a_{uuid.uuid4().hex[:6]}@example.com"
    mgr_b_email = f"mgr_b_{uuid.uuid4().hex[:6]}@example.com"
    mgr_a_id = _create_user(mgr_a_email, ROLE_MANAGER)
    mgr_b_id = _create_user(mgr_b_email, ROLE_MANAGER)
    
    # Assign department managers
    run_db(lambda conn: conn.execute(
        text("INSERT INTO department_managers (group_id, manager_user_id) VALUES (:g, :u)"),
        {"g": dept_a, "u": mgr_a_id},
    ))
    run_db(lambda conn: conn.execute(
        text("INSERT INTO department_managers (group_id, manager_user_id) VALUES (:g, :u)"),
        {"g": dept_b, "u": mgr_b_id},
    ))

    # Employee logs in and proposes an action in Dept A
    login(client, USER_EMAIL)
    r_prop = client.post(
        "/api/agents/actions/proposals",
        json={
            "action_type": "notification_broadcast",
            "parameters": {"title": "Dept A Alert", "message": "Operations notice"},
            "rationale": "Emergency alert for Dept A",
            "idempotency_key": f"key-mgr-{uuid.uuid4()}",
            "department_id": dept_a,
        },
    )
    assert r_prop.status_code == 201, r_prop.text
    prop_id = r_prop.json()["id"]

    # Employee cannot approve or reject
    r_emp_app = client.post(f"/api/agents/actions/proposals/{prop_id}/approve", json={"rationale": "Emp approve"})
    assert r_emp_app.status_code == 403

    # Manager of Dept B cannot approve or reject proposal of Dept A
    login(client, mgr_b_email)
    r_mgr_b = client.post(f"/api/agents/actions/proposals/{prop_id}/approve", json={"rationale": "Mgr B approve"})
    assert r_mgr_b.status_code == 403

    # Manager of Dept A can reject the proposal with rationale
    login(client, mgr_a_email)
    r_rej = client.post(f"/api/agents/actions/proposals/{prop_id}/reject", json={"rationale": "Alert needs more detail"})
    assert r_rej.status_code == 200, r_rej.text
    assert r_rej.json()["status"] == "rejected"
    assert r_rej.json()["approval_rationale"] == "Alert needs more detail"

    # A rejected proposal cannot be executed
    r_exec = client.post(f"/api/agents/actions/proposals/{prop_id}/execute")
    assert r_exec.status_code == 409, r_exec.text


def test_department_manager_cannot_publish_document_organization_wide(client, clean_tables):
    login(client, ADMIN_EMAIL)
    department_id = _department(client, f"Publish guard {uuid.uuid4().hex[:6]}")
    manager_email = f"publish_manager_{uuid.uuid4().hex[:8]}@example.com"
    manager_id = _create_user(manager_email, ROLE_MANAGER)
    run_db(lambda conn: conn.execute(
        text("INSERT INTO department_managers (group_id, manager_user_id) VALUES (:group_id, :user_id)"),
        {"group_id": department_id, "user_id": manager_id},
    ))

    admin_id = uuid.UUID(_user_id(ADMIN_EMAIL))

    async def create_private_document(conn):
        result = await conn.execute(
            Document.__table__.insert().values(
                owner_id=admin_id,
                visibility=VISIBILITY_PRIVATE,
                filename="synthetic-private-publish-test.pdf",
                file_format="pdf",
                storage_key=uuid.uuid4().hex * 2,
                sha256="b" * 64,
                size_bytes=1,
                status="ready",
            ).returning(Document.id)
        )
        return result.scalar_one()

    document_id = run_db(create_private_document)
    login(client, USER_EMAIL)
    proposed = client.post(
        "/api/agents/actions/proposals",
        json={
            "action_type": "document_publish",
            "parameters": {"document_id": str(document_id)},
            "rationale": "Request department review of this document",
            "idempotency_key": f"publish-guard-{uuid.uuid4()}",
            "department_id": department_id,
        },
    )
    assert proposed.status_code == 201, proposed.text
    proposal_id = proposed.json()["id"]

    login(client, manager_email)
    approved = client.post(
        f"/api/agents/actions/proposals/{proposal_id}/approve",
        json={"rationale": "Reviewed for departmental use"},
    )
    assert approved.status_code == 200, approved.text
    executed = client.post(f"/api/agents/actions/proposals/{proposal_id}/execute")
    assert executed.status_code == 403, executed.text

    state = _fetch_one(
        "SELECT p.status, p.execution_result, d.visibility "
        "FROM agent_action_proposals p JOIN documents d ON d.id = :document_id WHERE p.id = :proposal_id",
        {"document_id": document_id, "proposal_id": proposal_id},
    )
    assert state.status == "approved" and state.execution_result is None
    assert state.visibility == VISIBILITY_PRIVATE


def test_unimplemented_external_adapters_never_claim_success(client, clean_tables):
    login(client, ADMIN_EMAIL)
    admin_id = _user_id(ADMIN_EMAIL)
    
    # Create second admin for approvals
    second_email = f"approver_{uuid.uuid4().hex[:6]}@example.com"
    _create_user(second_email, ROLE_ADMIN)
    
    # 1. Webhook dispatch execution
    r_wh = client.post(
        "/api/agents/actions/proposals",
        json={
            "action_type": "webhook_dispatch",
            "parameters": {"url": "https://api.example.com/sync", "payload": {"job": "dispatch"}},
            "rationale": "Dispatch webhook test",
            "idempotency_key": f"key-wh-{uuid.uuid4()}",
        },
    )
    wh_id = r_wh.json()["id"]
    
    # Cannot execute before approval
    r_early = client.post(f"/api/agents/actions/proposals/{wh_id}/execute")
    assert r_early.status_code == 409

    # Approve
    login(client, second_email)
    client.post(f"/api/agents/actions/proposals/{wh_id}/approve", json={"rationale": "Approved for dispatch"})

    # No outbound delivery adapter is installed. The proposal stays approved and is not
    # recorded as executed; retrying must not fabricate a successful result either.
    r_exec = client.post(f"/api/agents/actions/proposals/{wh_id}/execute")
    assert r_exec.status_code == 503, r_exec.text
    assert r_exec.json()["error"]["code"] == "agent_action_unavailable"
    r_reexec = client.post(f"/api/agents/actions/proposals/{wh_id}/execute")
    assert r_reexec.status_code == 503
    assert r_reexec.json()["error"]["code"] == "agent_action_unavailable"
    state = _fetch_one("SELECT status, execution_result FROM agent_action_proposals WHERE id = :id", {"id": wh_id})
    assert state.status == "approved"
    assert state.execution_result is None


def test_proposal_expiration(client, clean_tables):
    login(client, ADMIN_EMAIL)
    second_email = f"approver_{uuid.uuid4().hex[:6]}@example.com"
    _create_user(second_email, ROLE_ADMIN)

    r_prop = client.post(
        "/api/agents/actions/proposals",
        json={
            "action_type": "notification_broadcast",
            "parameters": {"title": "Expiry Test", "message": "Short TTL"},
            "rationale": "Testing expiration",
            "idempotency_key": f"key-exp-{uuid.uuid4()}",
            "ttl_hours": 1,
        },
    )
    prop_id = r_prop.json()["id"]

    # Manually expire the proposal in the database
    run_db(lambda conn: conn.execute(
        text("UPDATE agent_action_proposals SET expires_at = :past WHERE id = :id"),
        {"past": datetime.now(UTC) - timedelta(hours=2), "id": prop_id},
    ))

    # Attempting to approve expired proposal returns 409 Conflict
    login(client, second_email)
    r_app = client.post(f"/api/agents/actions/proposals/{prop_id}/approve", json={"rationale": "Late approve"})
    assert r_app.status_code == 409, r_app.text
    assert "expired" in r_app.text
