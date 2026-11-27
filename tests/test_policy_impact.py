"""PostgreSQL acceptance for policy-to-process impact links and assigned exception history."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.test_access_control import new_group, set_access, user_id
from tests.test_knowledge import quote_from, ready_doc
from tests.test_knowledge_conflicts import SOURCE, _create, _grant_reviewers, _publish


def _published_policy(client) -> tuple[str, str]:
    document_id = ready_doc(client, "policy-process-source.pdf", SOURCE, visibility="shared")
    entry = _create(client, document_id, "Customer refunds are permitted within 30 days after invoice date.")
    _publish(client, entry)
    return document_id, entry["id"]


def test_policy_change_impact_exception_assignment_and_append_only_lifecycle(client):
    _grant_reviewers(client)
    login(client, ADMIN_EMAIL)
    document_id, entry_id = _published_policy(client)

    login(client, USER_EMAIL)
    candidates = client.get("/api/governance/policy-candidates")
    assert candidates.status_code == 200, candidates.text
    assert [row["entry_id"] for row in candidates.json()["items"]] == [entry_id]
    created = client.post("/api/governance/processes", json={
        "name": "Refund processing",
        "description": "Customer refund intake and review.",
        "policy_entry_id": entry_id,
    })
    assert created.status_code == 201, created.text
    process = created.json()
    assert process["policies"][0]["published_revision_number"] == 1
    assert process["policies"][0]["published_sources"][0]["filename"] == "policy-process-source.pdf"
    process_id = process["id"]

    # A submitted revision appears as scoped impact beside the last published statement.
    login(client, ADMIN_EMAIL)
    revised = client.post(f"/api/knowledge/{entry_id}/revisions", json={
        "statement": "Customer refunds are permitted within 14 days after invoice date.",
        "category": "policy",
        "visibility": "shared",
        "evidence": [{"document_id": document_id, "quote": quote_from(document_id)}],
    })
    assert revised.status_code == 200, revised.text
    assert client.post(f"/api/knowledge/{entry_id}/submit").status_code == 200
    login(client, USER_EMAIL)
    impact = client.get("/api/governance/processes")
    assert impact.status_code == 200, impact.text
    policy = impact.json()["items"][0]["policies"][0]
    assert policy["published_revision_number"] == 1
    assert policy["published_statement"].endswith("30 days after invoice date.")
    assert policy["pending_revision_number"] == 2
    assert policy["pending_statement"].endswith("14 days after invoice date.")

    assignees = client.get(f"/api/governance/processes/{process_id}/assignees?entry_id={entry_id}")
    assert assignees.status_code == 200, assignees.text
    target = next(row for row in assignees.json()["items"] if row["email"] == USER2_EMAIL)
    assigned = client.post(f"/api/governance/processes/{process_id}/exceptions", json={
        "entry_id": entry_id,
        "assigned_to": target["user_id"],
        "rationale": "Review the intake checklist against the submitted 14-day change.",
        "due_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
    })
    assert assigned.status_code == 201, assigned.text
    exception_id = assigned.json()["id"]
    assert assigned.json()["status"] == "open"
    assert assigned.json()["events"][0]["action"] == "assigned"

    # The Manager who created the assignment must retain scoped visibility into its status/history.
    manager_queue = client.get("/api/governance/policy-exceptions")
    assert manager_queue.status_code == 200, manager_queue.text
    manager_item = next(row for row in manager_queue.json()["items"] if row["id"] == exception_id)
    assert manager_item["process_id"] == process_id
    assert manager_item["assigned_to"]["email"] == USER2_EMAIL

    # A publisher without a review grant may receive and resolve an assignment but cannot reassign it.
    login(client, USER2_EMAIL)
    inbox = client.get("/api/governance/policy-exceptions")
    assert inbox.status_code == 200 and [row["id"] for row in inbox.json()["items"]] == [exception_id]
    assert inbox.json()["items"][0]["can_acknowledge"] is True
    forbidden = client.post(f"/api/governance/policy-exceptions/{exception_id}/actions", json={
        "action": "reassign", "assigned_to": target["user_id"], "rationale": "Attempted without review grant.",
    })
    assert forbidden.status_code == 403
    acknowledged = client.post(f"/api/governance/policy-exceptions/{exception_id}/actions", json={
        "action": "acknowledge", "rationale": "I will update the customer intake checklist.",
    })
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["status"] == "acknowledged"
    resolved = client.post(f"/api/governance/policy-exceptions/{exception_id}/actions", json={
        "action": "resolve", "rationale": "The checklist has been updated and verified.",
    })
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    assert [event["action"] for event in resolved.json()["events"]] == ["assigned", "acknowledged", "resolved"]

    assert run_db(lambda conn: conn.scalar(text(
        "SELECT count(*) FROM knowledge_policy_exception_events WHERE exception_id = :id"
    ), {"id": UUID(exception_id)})) == 3
    with pytest.raises(Exception, match="append-only"):
        run_db(lambda conn: conn.execute(text(
            "UPDATE knowledge_policy_exception_events SET rationale = 'edited' WHERE exception_id = :id"
        ), {"id": UUID(exception_id)}))
    with pytest.raises(Exception, match="matching immutable history event"):
        run_db(lambda conn: conn.execute(text(
            "UPDATE knowledge_policy_exceptions SET status = 'open' WHERE id = :id"
        ), {"id": UUID(exception_id)}))


def test_policy_impact_and_exception_are_hidden_after_source_acl_revocation(client):
    _grant_reviewers(client)
    login(client, ADMIN_EMAIL)
    document_id, entry_id = _published_policy(client)
    login(client, USER_EMAIL)
    process = client.post("/api/governance/processes", json={
        "name": "Source revocation process", "policy_entry_id": entry_id,
    })
    assert process.status_code == 201, process.text
    process_id = process.json()["id"]
    people = client.get(f"/api/governance/processes/{process_id}/assignees?entry_id={entry_id}").json()["items"]
    target = next(row for row in people if row["email"] == USER2_EMAIL)
    assigned = client.post(f"/api/governance/processes/{process_id}/exceptions", json={
        "entry_id": entry_id,
        "assigned_to": target["user_id"],
        "rationale": "Synthetic assignment used to verify live source ACL revocation.",
    })
    assert assigned.status_code == 201, assigned.text
    exception_id = assigned.json()["id"]

    login(client, ADMIN_EMAIL)
    assert set_access(client, document_id, "restricted", users=[ADMIN_EMAIL]).status_code == 200
    login(client, USER_EMAIL)
    assert client.get("/api/governance/policy-candidates").json()["items"] == []
    assert client.get("/api/governance/processes").json()["items"] == []
    assert client.get("/api/governance/policy-exceptions").json()["items"] == []
    denied = client.post(f"/api/governance/policy-exceptions/{exception_id}/actions", json={
        "action": "reassign", "assigned_to": target["user_id"], "rationale": "The source ACL was revoked.",
    })
    assert denied.status_code == 404


def test_exception_assignees_must_have_current_read_access_to_policy_sources(client):
    _grant_reviewers(client)
    login(client, ADMIN_EMAIL)
    document_id, entry_id = _published_policy(client)
    login(client, USER_EMAIL)
    process = client.post("/api/governance/processes", json={"name": "ACL-bound process", "policy_entry_id": entry_id})
    assert process.status_code == 201, process.text

    login(client, ADMIN_EMAIL)
    restricted = set_access(client, document_id, "restricted", users=[ADMIN_EMAIL, USER_EMAIL])
    assert restricted.status_code == 200 and restricted.json()["visibility"] == "restricted"
    login(client, USER2_EMAIL)
    assert client.get(f"/api/rag/documents/{document_id}").status_code == 404
    login(client, USER_EMAIL)
    assignees = client.get(f"/api/governance/processes/{process.json()['id']}/assignees?entry_id={entry_id}")
    assert assignees.status_code == 200, assignees.text
    assert all(item["email"] != USER2_EMAIL for item in assignees.json()["items"])
    outsider = run_db(lambda conn: conn.scalar(text("SELECT id FROM users WHERE email = :email"), {"email": USER2_EMAIL}))
    denied = client.post(f"/api/governance/processes/{process.json()['id']}/exceptions", json={
        "entry_id": entry_id,
        "assigned_to": str(outsider),
        "rationale": "An out-of-source-scope employee must not receive this policy action.",
    })
    assert denied.status_code == 422
    assert run_db(lambda conn: conn.scalar(text("SELECT count(*) FROM knowledge_policy_exceptions"))) == 0


def test_policy_exception_reassignment_revokes_old_assignee_and_records_history(client):
    _grant_reviewers(client)
    login(client, ADMIN_EMAIL)
    _, entry_id = _published_policy(client)
    login(client, USER_EMAIL)
    process = client.post("/api/governance/processes", json={
        "name": "Synthetic reassignment process", "policy_entry_id": entry_id,
    })
    assert process.status_code == 201, process.text
    process_id = process.json()["id"]
    assigned_to = {row["email"]: row["user_id"] for row in client.get(
        f"/api/governance/processes/{process_id}/assignees?entry_id={entry_id}"
    ).json()["items"]}
    assert USER2_EMAIL in assigned_to and ADMIN_EMAIL in assigned_to
    created = client.post(f"/api/governance/processes/{process_id}/exceptions", json={
        "entry_id": entry_id,
        "assigned_to": assigned_to[USER2_EMAIL],
        "rationale": "Assign the source-backed policy update to the synthetic employee.",
    })
    assert created.status_code == 201, created.text
    exception_id = created.json()["id"]

    # A direct API no-op reassignment must not add a duplicate history event.
    same_owner = client.post(f"/api/governance/policy-exceptions/{exception_id}/actions", json={
        "action": "reassign",
        "assigned_to": assigned_to[USER2_EMAIL],
        "rationale": "The current owner is unchanged.",
    })
    assert same_owner.status_code == 409, same_owner.text
    assert run_db(lambda conn: conn.scalar(text(
        "SELECT count(*) FROM knowledge_policy_exception_events WHERE exception_id = :id"
    ), {"id": UUID(exception_id)})) == 1

    # The reviewer can reassign in scope; the old employee loses assignee-only access immediately.
    reassigned = client.post(f"/api/governance/policy-exceptions/{exception_id}/actions", json={
        "action": "reassign",
        "assigned_to": assigned_to[ADMIN_EMAIL],
        "rationale": "Move the open policy action to the synthetic Admin for controlled review.",
    })
    assert reassigned.status_code == 200, reassigned.text
    assert reassigned.json()["assigned_to"]["email"] == ADMIN_EMAIL
    assert [event["action"] for event in reassigned.json()["events"]] == ["assigned", "reassigned"]
    assert reassigned.json()["events"][1]["from_assignee_email"] == USER2_EMAIL

    login(client, USER2_EMAIL)
    assert all(row["id"] != exception_id for row in client.get("/api/governance/policy-exceptions").json()["items"])
    denied = client.post(f"/api/governance/policy-exceptions/{exception_id}/actions", json={
        "action": "acknowledge", "rationale": "The prior assignee cannot act after reassignment.",
    })
    assert denied.status_code == 403

    login(client, ADMIN_EMAIL)
    inbox = client.get("/api/governance/policy-exceptions").json()["items"]
    item = next(row for row in inbox if row["id"] == exception_id)
    assert item["can_acknowledge"] is True
    acknowledged = client.post(f"/api/governance/policy-exceptions/{exception_id}/actions", json={
        "action": "acknowledge", "rationale": "The new assignee accepts the policy action.",
    })
    assert acknowledged.status_code == 200 and acknowledged.json()["status"] == "acknowledged"
    assert [event["action"] for event in acknowledged.json()["events"]] == ["assigned", "reassigned", "acknowledged"]
    assert run_db(lambda conn: conn.scalar(text(
        "SELECT count(*) FROM knowledge_policy_exception_events WHERE exception_id = :id"
    ), {"id": UUID(exception_id)})) == 3


def test_department_scoped_policy_process_and_assignee_picker_exclude_outsiders(client):
    _grant_reviewers(client)
    login(client, ADMIN_EMAIL)
    document_id = ready_doc(client, "department-policy-source.pdf", SOURCE, visibility="shared")
    group_id = new_group(client, "Synthetic Policy Department", kind="department")
    manager = client.patch(f"/api/admin/users/{user_id(USER_EMAIL)}", json={
        "role": "manager", "department_ids": [group_id],
    })
    assert manager.status_code == 200, manager.text
    employee = client.patch(f"/api/admin/users/{user_id(USER2_EMAIL)}", json={
        "role": "employee", "department_ids": [group_id],
    })
    assert employee.status_code == 200, employee.text
    access = set_access(client, document_id, "restricted", groups=[group_id])
    assert access.status_code == 200, access.text

    draft = client.post("/api/knowledge", json={
        "statement": "Customer refunds are permitted within 30 days after invoice date.",
        "category": "policy",
        "visibility": "restricted",
        "scope_group_id": group_id,
        "evidence": [{"document_id": document_id, "quote": quote_from(document_id)}],
    })
    assert draft.status_code == 201, draft.text
    entry_id = draft.json()["id"]
    revision_id = draft.json()["revisions"][0]["id"]
    assert client.post(f"/api/knowledge/{entry_id}/submit").status_code == 200
    login(client, USER_EMAIL)
    reviewed = client.post(f"/api/governance/knowledge/{revision_id}/decisions", json={
        "stage": "review", "rationale": "Reviewed the restricted source within the department.",
    })
    assert reviewed.status_code == 200, reviewed.text
    login(client, USER2_EMAIL)
    published = client.post(f"/api/governance/knowledge/{revision_id}/decisions", json={
        "stage": "publish", "rationale": "Published after the independent department review.",
    })
    assert published.status_code == 200, published.text

    login(client, USER_EMAIL)
    candidate_ids = {item["entry_id"] for item in client.get("/api/governance/policy-candidates").json()["items"]}
    assert entry_id in candidate_ids
    created = client.post("/api/governance/processes", json={
        "name": "Restricted policy implementation", "policy_entry_id": entry_id,
    })
    assert created.status_code == 201, created.text
    process = created.json()
    assert process["scope_group_id"] == group_id
    assignees = client.get(
        f"/api/governance/processes/{process['id']}/assignees?entry_id={entry_id}"
    )
    assert assignees.status_code == 200, assignees.text
    assignee_emails = {item["email"] for item in assignees.json()["items"]}
    assert USER_EMAIL in assignee_emails and USER2_EMAIL in assignee_emails
    assert ADMIN_EMAIL not in assignee_emails

    # Admin remains outside the department source ACL and cannot be assigned this process exception.
    denied = client.post(f"/api/governance/processes/{process['id']}/exceptions", json={
        "entry_id": entry_id,
        "assigned_to": str(user_id(ADMIN_EMAIL)),
        "rationale": "The outsider must not receive this department policy action.",
    })
    assert denied.status_code == 422
    assigned = client.post(f"/api/governance/processes/{process['id']}/exceptions", json={
        "entry_id": entry_id,
        "assigned_to": next(item["user_id"] for item in assignees.json()["items"] if item["email"] == USER2_EMAIL),
        "rationale": "Assign the scoped policy action to a current department member.",
    })
    assert assigned.status_code == 201, assigned.text
    login(client, ADMIN_EMAIL)
    assert all(row["id"] != assigned.json()["id"] for row in client.get("/api/governance/policy-exceptions").json()["items"])
